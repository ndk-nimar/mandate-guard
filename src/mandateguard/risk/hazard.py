"""T1.7 -- the discrete-time survival model: a logistic regression on person-weeks.

A discrete-time hazard model *is* a logistic regression on the person-period frame. Each
row asks "did this mandate die in this week, given that it was alive at the start of it",
and the fitted probability is the per-week hazard the allocator multiplies against rupees.
Nothing more exotic is needed, and `docs/stack.md` records why nothing more exotic is
wanted: a Cox model leaves the baseline unspecified when the baseline is the part that
matters, and a gradient-boosted tree wins on AUC and loses on calibration, which is
literally what GATE 1 measures.

Fit small, score whole
----------------------
The training slice is 44M rows, which is more than this laptop can hand to `sklearn` as a
dense matrix. So the model is fitted on a deterministic uniform subsample and then scored,
as a SQL expression, over every one of the 6.35M held-out rows. That split of duties is
what keeps the comparison in `docs/eval.md` honest: the baselines and the model produce
the same kind of object -- an expression -- and go through the same `scoring.score`.

The subsample is uniform over *rows*, not over subscribers. Sampling subscribers and
keeping all their weeks would make the sample's rows more correlated with each other, not
less, and would under-cover the durations that only long spells reach.

What is deliberately not a feature
----------------------------------
* **`age_years`** -- `docs/mapping.md` 3.6 established that it is missing non-randomly and
  that its missingness encodes signup channel. A model given it would learn the channel
  and be reported as having learned something about age.
* **`method`** (the rail) -- assigned from a hash (3.3), so by construction it carries no
  information. Any coefficient it earned would be pure overfit, and the Streamlit surface
  would then display a per-rail effect as a finding.
* **`week_start`** -- calendar time. A model that learns "November 2016 was a bad month"
  cannot carry that into the held-out period, and out-of-time is exactly the split being
  used. Duration is a covariate; the calendar is not.
* **`tenure_days`** -- it is `7 * week_index` on every row of the frame, exactly.
* **`death_kind`** -- a label.

No `class_weight="balanced"`
----------------------------
Re-weighting the classes is the standard reflex at a 1.4% base rate and it would be wrong
here. It multiplies every predicted probability by roughly the imbalance ratio, which
destroys calibration -- and `docs/eval.md` 1.4 already showed that a well-discriminating
but badly-calibrated model *loses* on Brier. The allocator turns these probabilities into
rupees; a probability that is uniformly too big prices every decision too high.
"""

from __future__ import annotations

import duckdb
import numpy as np
from pydantic import BaseModel
from sklearn.linear_model import LogisticRegression

from mandateguard.risk.baseline import bin_conditions

FIT_SALT = "fit"
"""Salt for the training subsample's hash. Its own salt, like every other hash of a key
in this repository -- see `mandates.RAIL_SALT` for the bug that made that a rule."""

FIT_ROWS = 1_000_000
"""Target size of the training subsample.

One million rows against 56 features is far past the point where more rows change the
coefficients; the binding constraint is the roughly 14,000 deaths they contain, and
doubling the rows only doubles that. Chosen to keep the dense matrix under half a
gigabyte, because the alternative -- fitting on all 44M -- does not fit on the machine
this is built on, and a model that cannot be refitted is a model nobody will refit."""

DURATION_BINS: tuple[tuple[int, int | None], ...] = (
    (2, 3),
    (4, 4),
    (5, 5),
    (6, 7),
    (8, 12),
    (13, 25),
    (26, 51),
    (52, None),
)
"""One-hot bins for `week_index`; weeks 0-1 is the omitted reference level.

Dummies rather than a linear term, because the baseline hazard is not remotely linear:
`docs/mapping.md` 5.7 measured 0.0740 at weeks 4-7 against 0.0066 at week 52+. Weeks 4
and 5 get their own bins because that is where the first renewal of a 30-day plan lands,
and a bin that smeared the spike across four weeks would hand the model a baseline it
could not sharpen.

This is the standard discrete-time survival treatment: the baseline hazard is estimated
as a step function rather than assumed to be any particular shape."""

CITY_LEVELS: tuple[int, ...] = (13, 5, 4, 15, 22, 6, 14, 12, 9, 11, 18, 8, 10, 17, 21, 3, 7)
"""Cities with enough volume to earn a dummy; city 1 is the reference and the rest fall
into it. Fixed rather than derived at fit time so that the feature matrix has the same
columns whatever slice is fitted -- a model whose column set depends on its training data
cannot be compared to another one."""

CHANNEL_LEVELS: tuple[int, ...] = (9, 3, 4, 13, 10)
"""`registered_via` levels; 7 (the most common) is the reference. 3.6 noted that this
column's *missingness* encodes signup channel, and a missing value here is exactly a
missing `members` row -- which `member_record_found` already carries, so no separate
indicator is added."""


class Feature(BaseModel):
    """One model input, as a name and the SQL that computes it.

    The pairing is the point. Fitting reads these expressions out of the frame and
    scoring writes them back into a prediction expression, so the feature the model was
    trained on and the feature it is scored on cannot drift apart -- the usual way a
    model that validated well fails in production.
    """

    name: str
    sql: str


def indicator(condition: str) -> str:
    """A 0/1 column from a SQL condition, with null forced to 0.

    `(city = 13)::INT` is NULL when `city` is NULL, and `sklearn` refuses a matrix with a
    NaN in it -- correctly, because a null indicator is not a missing value that needs
    imputing. "This subscriber has no city on record" is not "this subscriber might be in
    city 13": it is a 0 for every city dummy, and `member_known` is the column that says
    the record was absent. Same for gender, and for every expiry bin when the coverage
    end is unknown.
    """
    return f"coalesce(({condition})::INT, 0)"


def log1p(expression: str) -> str:
    """`ln(1 + x)` with nulls and negatives floored at 0.

    The log is what keeps rupees, day counts and debit counts on comparable scales
    without a fitted scaler -- a scaler would be a second object that has to travel with
    the coefficients and be applied identically at scoring time, and the design here is
    that the model is one self-contained SQL string.
    """
    return f"ln(1 + greatest(coalesce({expression}, 0), 0))"


def feature_spec() -> list[Feature]:
    """Every input to the hazard model, in a fixed order."""
    features = [
        Feature(name=f"weeks_{low}_{high or 'plus'}", sql=_between("week_index", low, high))
        for low, high in DURATION_BINS
    ]
    # The expiry clock, binned exactly as `expiry_bins` bins it -- so the model is handed
    # the baseline's information and any improvement has to come from somewhere else.
    # The final bin is the reference; the null case gets its own indicator because "we do
    # not know when coverage ends" is not "coverage ends far away".
    conditions = bin_conditions()
    features += [
        Feature(name=f"expiry_{index}", sql=indicator(condition))
        for index, (_, condition) in enumerate(conditions[:-1])
    ]
    features.append(Feature(name="expiry_unknown", sql=indicator("days_to_coverage_end IS NULL")))
    features += [
        Feature(name="log_days_since_txn", sql=log1p("days_since_last_txn")),
        Feature(name="log_amount", sql=log1p("amount_inr")),
        Feature(name="log_cycle", sql=log1p("debit_frequency_days")),
        Feature(name="log_debits", sql=log1p("debits_so_far")),
        Feature(name="log_cancels", sql=log1p("cancels_so_far")),
        Feature(name="log_paid", sql=log1p("paid_so_far_inr")),
        Feature(name="log_account_age", sql=log1p("account_age_days")),
        Feature(name="discounted", sql=indicator("discount_inr > 0")),
        Feature(name="auto_renew", sql=indicator("auto_renew")),
        Feature(name="frequency_imputed", sql=indicator("frequency_imputed")),
        Feature(name="left_truncated", sql=indicator("left_truncated")),
        Feature(name="member_known", sql=indicator("member_record_found")),
        Feature(name="male", sql=indicator("gender = 'male'")),
        Feature(name="female", sql=indicator("gender = 'female'")),
    ]
    features += [
        Feature(name=f"city_{level}", sql=indicator(f"city = {level}")) for level in CITY_LEVELS
    ]
    features += [
        Feature(name=f"channel_{level}", sql=indicator(f"registered_via = {level}"))
        for level in CHANNEL_LEVELS
    ]
    return features


def _between(column: str, low: int, high: int | None) -> str:
    if high is None:
        return indicator(f"{column} >= {low}")
    return indicator(f"{column} BETWEEN {low} AND {high}")


class FittedHazard(BaseModel):
    """Coefficients plus everything needed to reproduce and to score them."""

    features: list[Feature]
    coefficients: list[float]
    intercept: float
    rows_fitted: int
    events_fitted: int
    seed: int
    iterations: int
    converged: bool

    @property
    def expression(self) -> str:
        """The model as one SQL expression returning a probability in (0, 1).

        Scored by the same `scoring.score` as every baseline, over the whole held-out
        frame rather than a subsample of it.
        """
        terms = " + ".join(
            f"({beta!r}) * ({feature.sql})"
            for beta, feature in zip(self.coefficients, self.features, strict=True)
            if beta != 0.0
        )
        return f"1.0 / (1.0 + exp(-(({self.intercept!r}) + {terms})))"

    def ranked(self, limit: int = 12) -> list[tuple[str, float]]:
        """Coefficients by absolute size. Readable because every input is an indicator
        or a log, so a coefficient is a log-odds shift for a comparable-sized move."""
        pairs = [(f.name, b) for f, b in zip(self.features, self.coefficients, strict=True)]
        return sorted(pairs, key=lambda pair: -abs(pair[1]))[:limit]


def fit(
    con: duckdb.DuckDBPyConnection,
    source: str,
    where: str,
    seed: int,
    rows: int = FIT_ROWS,
) -> FittedHazard:
    """Fit the hazard on a deterministic uniform subsample of the training slice."""
    features = feature_spec()
    total = con.execute(f"SELECT count(*) FROM {source} WHERE {where}").fetchone()
    assert total is not None
    cutoff = max(1, round(rows / int(total[0]) * 1_000_000)) if total[0] else 1

    # The subsample is a property of the row's key, not of an RNG whose state would have
    # to be threaded through every caller to stay reproducible. `seed` is mixed into the
    # salt so that changing `config/params.yaml`'s seed really does change the draw.
    selected = (
        f"hash(mandate_id || '-' || week_index::VARCHAR || '{FIT_SALT}{seed}') % 1000000 < {cutoff}"
    )
    columns = ", ".join(f"{f.sql} AS {f.name}" for f in features)
    # ORDER BY so the matrix handed to sklearn is the same matrix on every run. lbfgs is
    # order-independent in exact arithmetic and not quite in floating point, and ADR 0003
    # is about not depending on things that merely happen to be true.
    frame = con.execute(
        f"SELECT {columns}, event::INT AS event FROM {source} "
        f"WHERE {where} AND {selected} ORDER BY mandate_id, week_index"
    ).df()

    y = frame.pop("event").to_numpy()
    x = frame.to_numpy(dtype=np.float64)
    model = LogisticRegression(max_iter=1000, C=1.0, solver="lbfgs")
    model.fit(x, y)

    return FittedHazard(
        features=features,
        coefficients=[float(b) for b in model.coef_[0]],
        intercept=float(model.intercept_[0]),
        rows_fitted=int(x.shape[0]),
        events_fitted=int(y.sum()),
        seed=seed,
        iterations=int(model.n_iter_[0]),
        converged=int(model.n_iter_[0]) < 1000,
    )


def format_model(model: FittedHazard) -> str:
    """Markdown, because these numbers are due in docs/eval.md, not on a terminal."""
    lines = [
        f"Fitted on **{model.rows_fitted:,} person-weeks** ({model.events_fitted:,} deaths) "
        f"subsampled from the training slice with seed {model.seed}. "
        f"{'Converged' if model.converged else '**Did not converge**'} in "
        f"{model.iterations} iterations.",
        "",
        f"Intercept {model.intercept:.4f}. Largest coefficients by absolute size:",
        "",
        "| feature | coefficient |",
        "|---|---:|",
    ]
    lines += [f"| `{name}` | {beta:+.4f} |" for name, beta in model.ranked()]
    return "\n".join(lines)
