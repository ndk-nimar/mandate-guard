"""How every risk model in this project is split, scored, and compared.

One module rather than one per model, because the comparison is the point. If the
baseline and the hazard model were scored by different code, "the model beats the
baseline" would be a claim about two scripts as much as about two models -- and GATE 1
turns on exactly that comparison.

**A model here is a SQL expression that returns a probability.** That sounds like a
constraint and is actually what makes the comparison honest: the naive baselines are
expressions over one column, T1.7's logistic regression is a sigmoid of a linear
combination whose coefficients sklearn fitted on a subsample, and both are then scored
by the same aggregate over the same 58M rows. Nothing is scored on the frame it was fit
on, and nothing is scored in a different way from anything else.

The split is out of time
------------------------
The test set is the last `horizon.weeks` before the snapshot; training is everything
before that. Two alternatives were rejected:

* **A random split of person-weeks** puts week 30 of a subscriber in training and week 31
  in test. The model then predicts a week it has effectively already seen, and the score
  is optimistic for a reason that has nothing to do with the model.
* **A split by subscriber** is defensible and is what most churn write-ups do, but it
  answers "how well does this generalise to strangers?" This system is not deployed
  against strangers -- it is deployed against a book it already knows, predicting
  forward. Out of time is the question actually being asked.

The cost of out of time is that the test period is not a random sample of the frame: it
over-represents long-tenured survivors, because the subscribers who died in 2015 are not
in it. Its base rate is therefore lower than the frame's, and every score below is
reported next to that base rate rather than in isolation.
"""

from __future__ import annotations

from datetime import date, timedelta

import duckdb
from pydantic import BaseModel

EPSILON = 1e-15
"""Clip applied before taking a logarithm. A model that predicts exactly 0 for an event
that happens scores an infinite log loss, which is a property of the metric rather than
of the model -- the rule baseline predicts hard 0/1 and would otherwise be unscoreable."""


class Split(BaseModel):
    """Where training stops and testing starts, and which weeks are scoreable at all."""

    cutoff: date
    weeks: int
    last_observable: date

    @property
    def observable(self) -> str:
        """Weeks whose outcome could actually be observed before the data ended.

        A death is only recorded once the renewal tolerance has elapsed without a
        renewal (`docs/mapping.md` 5.2), so a week ending inside the last
        `7 + tolerance` days of the log cannot have a *confirmed* death in it. Its rows
        are not wrong -- their features are real -- but their labels are all zero for a
        reason that has nothing to do with the subscribers, and they were measurably
        deflating the test period's death rate: 0.0078 against a training 0.0134,
        which read as a badly miscalibrated model rather than as an artefact of the
        window.

        This is section 2.2's right-censoring rule applied one layer down. Dropping
        events that could not be observed is the same discipline as not counting a
        subscriber who cancelled five days before the horizon as "failed to recover".
        """
        return f"week_start <= DATE '{self.last_observable}'"

    @property
    def train(self) -> str:
        return f"week_start < DATE '{self.cutoff}' AND {self.observable}"

    @property
    def test(self) -> str:
        return f"week_start >= DATE '{self.cutoff}' AND {self.observable}"


def split_at(snapshot: date, weeks: int, tolerance_days: int = 7) -> Split:
    """Hold out the last `weeks` weeks before the snapshot.

    `weeks` is `horizon.weeks` from the config, not a number chosen here: the harness
    rolls the world forward over exactly that many weeks, so the held-out period is the
    period the system will actually be asked to predict.
    """
    return Split(
        cutoff=snapshot - timedelta(days=7 * weeks),
        weeks=weeks,
        last_observable=snapshot - timedelta(days=7 + tolerance_days),
    )


class Score(BaseModel):
    """One model's performance on one slice of the frame."""

    model: str
    rows: int
    events: int
    mean_prediction: float
    brier: float
    log_loss: float

    @property
    def base_rate(self) -> float:
        """The share of person-weeks that were deaths. Every score is read against it."""
        return self.events / self.rows if self.rows else 0.0

    @property
    def calibration_in_the_large(self) -> float:
        """Predicted deaths over actual deaths. 1.0 is calibrated in aggregate.

        Weaker than T1.8's reliability curve -- a model can be perfect here and wrong in
        every bucket -- but it is the first thing to check, because a model that predicts
        twice as many deaths as happened is not going to be fixed by better features.
        """
        return self.mean_prediction / self.base_rate if self.base_rate else 0.0

    def skill_against(self, reference: Score) -> float:
        """Brier skill score: the share of the reference model's error removed.

        0 means "no better than predicting the base rate for everyone", which is the
        honest zero point for a rare event -- raw Brier looks impressive at any base rate
        of 1.3% and says nothing.
        """
        return 1 - self.brier / reference.brier if reference.brier else 0.0

    @property
    def line(self) -> str:
        return (
            f"| {self.model} | {self.rows:,} | {self.brier:.6f} | {self.log_loss:.4f} "
            f"| {self.mean_prediction:.4f} | {self.calibration_in_the_large:.3f} |"
        )


def score(
    con: duckdb.DuckDBPyConnection,
    source: str,
    prediction: str,
    where: str,
    model: str,
) -> Score:
    """Score one prediction expression over one slice, in SQL.

    `prediction` is any SQL expression over the frame's columns returning a probability.
    Scoring 58M rows in DuckDB rather than pulling them into numpy is not only faster --
    it is what lets the full frame be scored at all on a laptop, so no model is ever
    quietly evaluated on a convenience subsample.
    """
    p = f"least(1 - {EPSILON}, greatest({EPSILON}, {prediction}))"
    row = con.execute(
        f"""
        SELECT count(*),
               count(*) FILTER (WHERE event),
               avg({p}),
               avg(({p} - event::INT) * ({p} - event::INT)),
               -avg(CASE WHEN event THEN ln({p}) ELSE ln(1 - {p}) END)
        FROM {source} WHERE {where}
        """
    ).fetchone()
    assert row is not None
    return Score(
        model=model,
        rows=int(row[0]),
        events=int(row[1]),
        mean_prediction=float(row[2] or 0.0),
        brier=float(row[3] or 0.0),
        log_loss=float(row[4] or 0.0),
    )


def format_scores(scores: list[Score], reference: Score) -> str:
    """Markdown, because these numbers are due in docs/eval.md, not on a terminal."""
    lines = [
        f"Test slice: **{reference.rows:,} person-weeks**, {reference.events:,} deaths, "
        f"base rate **{reference.base_rate:.4f}**.",
        "",
        "| model | rows | Brier | log loss | mean p | calibration |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    lines += [s.line for s in scores]
    lines += [
        "",
        f"| model | Brier skill vs `{reference.model}` |",
        "|---|---:|",
    ]
    lines += [f"| {s.model} | {s.skill_against(reference):+.4f} |" for s in scores]
    return "\n".join(lines)
