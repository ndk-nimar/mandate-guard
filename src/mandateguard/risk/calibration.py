"""T1.8 -- is the predicted probability the actual probability?

Brier and log loss are single numbers that mix two different virtues: *discrimination*
(does the model rank the risky weeks above the safe ones) and *calibration* (when it says
5%, does 5% of them die). For this system the second one is not a nicety. The allocator
multiplies these probabilities by rupees, so a model that is uniformly twice too high
prices every decision twice too high and spends the ask budget on mandates that were
never going to die -- which is precisely the failure `docs/eval.md` 1.4 caught in the
binned baseline, and precisely what the whole project exists to avoid.

`scoring.calibration_in_the_large` already checks the aggregate. This module checks it
bucket by bucket, which is stronger: a model can predict exactly the right number of
deaths overall while being wrong about which weeks they land in.

Buckets are quantiles of the prediction, not equal-width
--------------------------------------------------------
At a base rate near 0.7%, equal-width bins put 99% of the rows in the first bin and
nothing in the rest, so the plot would be a single point and the metric would be the
aggregate one wearing a curve's clothes. Equal-count bins put the same number of
person-weeks in each, so every bucket carries enough deaths to be worth reading -- and
the axis is logarithmic for the same reason, because the predictions span two orders of
magnitude.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from pydantic import BaseModel

from mandateguard.data.paths import ensure

BUCKETS = 20
"""Equal-count buckets of the prediction. Twenty puts ~2,300 deaths in each bucket of the
full held-out slice, which is enough that a bucket's observed rate is a measurement
rather than a coin flip, while still resolving the top of the risk distribution."""


class Bucket(BaseModel):
    """One equal-count slice of the prediction range."""

    index: int
    rows: int
    events: int
    mean_prediction: float

    @property
    def observed(self) -> float:
        return self.events / self.rows if self.rows else 0.0

    @property
    def error(self) -> float:
        return self.mean_prediction - self.observed

    @property
    def line(self) -> str:
        # A bucket with no deaths has no ratio -- not an infinite one. Printing `inf`
        # would read as a catastrophic miscalibration when what actually happened is
        # that a low-risk bucket behaved exactly as a low-risk bucket should.
        ratio = f"{self.mean_prediction / self.observed:.2f}" if self.observed else "--"
        return (
            f"| {self.index} | {self.rows:,} | {self.events:,} | {self.mean_prediction:.5f} "
            f"| {self.observed:.5f} | {ratio} |"
        )


class Reliability(BaseModel):
    """One model's predicted-versus-observed curve."""

    model: str
    buckets: list[Bucket]

    @property
    def rows(self) -> int:
        return sum(b.rows for b in self.buckets)

    @property
    def expected_calibration_error(self) -> float:
        """Mean absolute gap between predicted and observed, weighted by bucket size.

        In the units of the thing itself: an ECE of 0.001 at a base rate of 0.007 means
        the typical bucket is off by about a seventh of the base rate.
        """
        if not self.rows:
            return 0.0
        return sum(abs(b.error) * b.rows for b in self.buckets) / self.rows

    @property
    def max_calibration_error(self) -> float:
        """The worst bucket. Reported next to the ECE because the ECE is size-weighted,
        and the buckets that matter most to the allocator -- the high-risk ones it will
        actually act on -- are not the biggest contributors to it."""
        return max((abs(b.error) for b in self.buckets), default=0.0)


def reliability(
    con: duckdb.DuckDBPyConnection,
    source: str,
    prediction: str,
    where: str,
    model: str,
    buckets: int = BUCKETS,
) -> Reliability:
    """Bucket the predictions by quantile and compare each bucket's mean to its outcome.

    `ntile` over a 6.35M-row sort is a few seconds, which is the price of not having to
    choose bin edges by hand -- and hand-chosen edges on a distribution this skewed would
    be a modelling decision hiding inside a plotting utility.
    """
    rows = con.execute(
        f"""
        SELECT bucket, count(*), count(*) FILTER (WHERE event), avg(p)
        FROM (
            SELECT event, {prediction} AS p,
                   ntile({buckets}) OVER (ORDER BY {prediction}) AS bucket
            FROM {source} WHERE {where}
        ) GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    return Reliability(
        model=model,
        buckets=[
            Bucket(index=int(b), rows=int(n), events=int(e), mean_prediction=float(p or 0.0))
            for b, n, e, p in rows
        ],
    )


def format_reliability(curve: Reliability) -> str:
    lines = [
        f"`{curve.model}` -- ECE **{curve.expected_calibration_error:.5f}**, "
        f"worst bucket **{curve.max_calibration_error:.5f}**.",
        "",
        "| bucket | person-weeks | deaths | predicted | observed | ratio |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [b.line for b in curve.buckets]
    return "\n".join(lines)


def plot(curves: list[Reliability], path: Path) -> Path:
    """Write the reliability diagram, deterministically.

    Two panels. The left one is the diagram itself on log-log axes -- the predictions
    span two orders of magnitude, and on linear axes every bucket but the last would be
    squashed into the origin. The right one is where the person-weeks actually are, which
    is what stops the left panel from being read as though every bucket were equally
    important.

    `metadata={"Software": None}` is not cosmetic. Matplotlib stamps its own version into
    every PNG, so without it this committed file changes whenever matplotlib does, and
    ADR 0003 is about derived files not changing for reasons that are not about the data.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ensure(path.parent)
    figure, (left, right) = plt.subplots(1, 2, figsize=(11, 4.6))

    low = min(b.mean_prediction for c in curves for b in c.buckets if b.mean_prediction > 0)
    high = max(max(b.mean_prediction, b.observed) for c in curves for b in c.buckets)
    left.plot([low, high], [low, high], color="0.6", linewidth=1, linestyle="--", zorder=1)
    for curve in curves:
        left.plot(
            [b.mean_prediction for b in curve.buckets],
            [max(b.observed, low / 2) for b in curve.buckets],
            marker="o",
            markersize=4,
            linewidth=1.4,
            label=curve.model,
            zorder=2,
        )
    left.set_xscale("log")
    left.set_yscale("log")
    left.set_xlabel("predicted weekly hazard")
    left.set_ylabel("observed death rate")
    left.set_title("Reliability, equal-count buckets")
    left.legend(frameon=False, fontsize=8)
    left.grid(True, which="both", linewidth=0.3, alpha=0.4)

    for curve in curves:
        right.plot(
            [b.index for b in curve.buckets],
            [b.events for b in curve.buckets],
            marker="o",
            markersize=4,
            linewidth=1.4,
            label=curve.model,
        )
    right.set_yscale("log")
    right.set_xlabel("bucket (equal person-weeks, ascending risk)")
    right.set_ylabel("deaths in bucket")
    right.set_title("Where the deaths are")
    right.grid(True, which="both", linewidth=0.3, alpha=0.4)

    figure.tight_layout()
    figure.savefig(path, dpi=140, metadata={"Software": None})
    plt.close(figure)
    return path
