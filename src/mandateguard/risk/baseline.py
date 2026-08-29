"""T1.6 -- the naive baselines, scored before the real model exists.

The order is deliberate. Scoring the baseline *after* the hazard model is how a project
ends up with a baseline tuned until the model wins; scoring it first means the number the
model has to beat was fixed before anyone knew what the model would produce.

Three baselines, not one, because "beats the baseline" means nothing without saying which:

**`base_rate`** predicts the training death rate for every row. This is the floor, and it
is the reference every skill score is computed against. At a base rate near 1.3% it is
already "accurate" 98.7% of the time, which is exactly why accuracy is not reported
anywhere in this project.

**`expiry_rule`** is the plan's phrasing taken literally -- "risk equals closeness to
expiry" -- as a hard rule: coverage ending within a week means death, otherwise not. It
predicts 0 and 1, so its log loss is dominated by the clip in `scoring.EPSILON` and is
not very meaningful; its Brier is. It is here because it is what a team without a model
would actually ship, and because a rule that beat the fitted models would be a finding.

**`expiry_bins`** is the same idea done as well as it can be done without a model: bin
`days_to_coverage_end` and predict each bin's observed death rate in the training period.
This is the honest opponent. A one-feature lookup table is hard to beat on a hazard that
is mostly driven by the billing clock, and if the logistic regression cannot beat it then
the extra features are not carrying anything and GATE 1 should say so.

Every baseline is fitted on the training slice only and returns a SQL expression, so it
is scored by the same code path as every other model (`scoring.score`).
"""

from __future__ import annotations

import duckdb
from pydantic import BaseModel

EXPIRY_EDGES: tuple[int, ...] = (0, 3, 7, 14, 30, 60, 120)
"""Upper bounds, in days, for the `days_to_coverage_end` bins.

Fixed rather than derived from quantiles of the data. Quantile bins would move whenever
the data moved, so the baseline would not be the same baseline between runs -- and a
baseline that drifts is not a baseline. The edges follow the billing clock this book
actually runs on: already expired (< 0), the last few days, the last week, a fortnight, a
month, two months, and everything longer.
"""

RULE_DAYS = 7
"""`expiry_rule` calls a mandate dead if coverage ends within this many days. Seven,
because the harness decides weekly (`docs/mapping.md` 2.5) -- the rule is "it expires
before I get another turn"."""


class Bin(BaseModel):
    """One bucket of `days_to_coverage_end` and the death rate observed in it."""

    label: str
    condition: str
    rows: int
    events: int

    @property
    def rate(self) -> float:
        return self.events / self.rows if self.rows else 0.0

    @property
    def line(self) -> str:
        return f"| {self.label} | {self.rows:,} | {self.events:,} | {self.rate:.4f} |"


class BinnedBaseline(BaseModel):
    """A fitted lookup table over `days_to_coverage_end`, plus the fallback."""

    bins: list[Bin]
    fallback: float
    """Used for rows with a null `days_to_coverage_end`, and for any bin that was empty
    in training. Empty-bin fallback matters: a bin the training period never saw would
    otherwise predict 0 and score an infinite log loss on its first real death."""

    @property
    def expression(self) -> str:
        arms = " ".join(
            f"WHEN {b.condition} THEN {b.rate if b.rows else self.fallback}" for b in self.bins
        )
        return f"CASE {arms} ELSE {self.fallback} END"


def bin_conditions() -> list[tuple[str, str]]:
    """(label, SQL condition) per bin, in order, covering the whole real line.

    Public because `hazard.py` builds its expiry dummies from the same list. The model is
    then handed exactly the information `expiry_bins` has, so any improvement it shows has
    to come from somewhere other than a finer view of the same column.

    Nulls are deliberately not given a bin -- they fall through to the `ELSE` arm, which
    is the fallback. A null here means the row's coverage end is unknown, which is a
    different statement from "expiry is far away" and must not be filed with it.
    """
    column = "days_to_coverage_end"
    pairs = [("already expired", f"{column} < 0")]
    previous = 0
    for edge in EXPIRY_EDGES[1:]:
        pairs.append((f"{previous}-{edge} days", f"{column} <= {edge}"))
        previous = edge + 1
    pairs.append((f"{EXPIRY_EDGES[-1]}+ days", f"{column} IS NOT NULL"))
    return pairs


def constant(con: duckdb.DuckDBPyConnection, source: str, where: str) -> float:
    """The training death rate per person-week. The floor every model is measured from."""
    row = con.execute(
        f"SELECT count(*) FILTER (WHERE event) / count(*)::DOUBLE FROM {source} WHERE {where}"
    ).fetchone()
    assert row is not None
    return float(row[0] or 0.0)


def rule_expression(days: int = RULE_DAYS) -> str:
    """ "Risk equals closeness to expiry", as a hard rule rather than a probability."""
    return (
        f"CASE WHEN days_to_coverage_end IS NOT NULL AND days_to_coverage_end <= {days} "
        "THEN 1.0 ELSE 0.0 END"
    )


def fit_bins(con: duckdb.DuckDBPyConnection, source: str, where: str) -> BinnedBaseline:
    """Observed death rate per `days_to_coverage_end` bin, over the training slice only.

    The `CASE` is evaluated top to bottom, so each arm only has to state its own upper
    bound -- the lower bound is "everything the previous arms did not take".
    """
    pairs = bin_conditions()
    arms = " ".join(f"WHEN {condition} THEN {index}" for index, (_, condition) in enumerate(pairs))
    rows = con.execute(
        f"""
        SELECT CASE {arms} END AS bucket, count(*), count(*) FILTER (WHERE event)
        FROM {source} WHERE {where} GROUP BY 1
        """
    ).fetchall()
    counts = {int(b): (int(n), int(e)) for b, n, e in rows if b is not None}
    return BinnedBaseline(
        bins=[
            Bin(
                label=label,
                condition=condition,
                rows=counts.get(index, (0, 0))[0],
                events=counts.get(index, (0, 0))[1],
            )
            for index, (label, condition) in enumerate(pairs)
        ],
        fallback=constant(con, source, where),
    )


def format_bins(model: BinnedBaseline) -> str:
    lines = [
        "| `days_to_coverage_end` | person-weeks | deaths | rate |",
        "|---|---:|---:|---:|",
    ]
    lines += [b.line for b in model.bins]
    lines += ["", f"Fallback for a null or unseen bin: {model.fallback:.4f}."]
    return "\n".join(lines)
