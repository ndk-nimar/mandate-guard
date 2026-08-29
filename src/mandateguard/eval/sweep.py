"""T2.7 and T2.8 -- sweep the budget, and sweep the two numbers nobody measured.

Two pieces of infrastructure, one motive. `docs/calibration.md` §4 records that uplift and
backfire have no public measurement, and Phase 2's first result turned on them by a 3%
margin: at the shipped values the riskiest mandate in the book is worth 0.357 and an ask
breaks even at 0.369. A project whose headline flips on an unmeasured constant does not
get to publish a point estimate. It publishes a region.

**T2.7 -- the budget curve.** Profit against budget, per arm. The shape is the claim:
spending nothing leaves money on the table, spending everything burns the book through
backfire, and somewhere between them is an optimum. Zhang's published calibration is the
reference -- optimum at 7 contacts, 10 costs 16% of profit, 4 costs 32% -- so the curve
should come out *asymmetric*, with under-asking the more expensive mistake.

**T2.8 -- the sensitivity grid.** The `(uplift x backfire)` plane, with the region where
selection beats rotation drawn on it rather than asserted. Until Phase 3 exists the
challenger is P3 `GreedyEV`; the reference is P2 `RoundRobin`, which is the arm that makes
the comparison mean anything.

Neither of these tunes a parameter. They map where the answer changes, which is the only
honest thing to do with a number that was never measured.
"""

from __future__ import annotations

from collections.abc import Callable
from math import log
from pathlib import Path

from pydantic import BaseModel

from mandateguard.allocator.base import NoAskPolicy, Policy
from mandateguard.allocator.baselines import ChronologicalCap, GreedyEV, RoundRobin
from mandateguard.data.paths import ensure
from mandateguard.eval.world import BookMandate, RunMetrics, run
from mandateguard.policy.loader import Params

PolicyFactory = Callable[[Params], Policy]
"""How a sweep names an arm: something that turns config into a policy.

Not `type[Policy]`. The base class takes no constructor arguments -- `NoAskPolicy` needs
none -- while every arm that reads a parameter takes `Params`, so a sweep that varies
parameters has to rebuild its arms at every point rather than reuse one instance. Typing
the slot as a factory says that out loud instead of leaving it to a comment."""

ARMS: dict[str, PolicyFactory] = {
    "P1": ChronologicalCap,
    "P2": RoundRobin,
    "P3": GreedyEV,
}
"""The arms a sweep varies. P0 is handled separately -- it takes no constructor argument
and its curve is a flat line by definition, which is exactly what makes it the floor."""


UPLIFT_SCALES: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0, 8.0, 16.0)
"""Uplift scales the sensitivity grid sweeps.

Geometric and wide, because `uplift_scale` is the knob that converts a channel's
"P(re-consent | contacted)" prior into "share of this week's deaths avoided", and nobody
knows the conversion. 1.0 is the shipped reading -- that the two are the same number --
and 16.0 says an ask is sixteen times more effective than that reading implies."""

BACKFIRE_RATES: tuple[float, ...] = (0.0005, 0.001, 0.003, 0.006, 0.012, 0.025)
"""First-ask backfire rates the grid sweeps. 0.006 is shipped (`problem.md` §5.1's
illustrative ladder) and 0.0005 is an order of magnitude gentler. `backfire_twelfth_ask`
moves with each of these at the configured ratio."""


def budget_ladder(channel_cost_inr: float, book_size: int, steps: int = 16) -> list[float]:
    """Budgets from zero to "ask everyone, every week", spaced geometrically.

    Geometric rather than linear because the interesting behaviour is at the bottom: the
    difference between 10 asks and 100 matters, the difference between 9,000 and 9,100
    does not. A linear ladder spends most of its points in the flat region past
    saturation.

    The top of the ladder is one ask per mandate per week. Past that the budget cannot
    buy anything, so a higher point would be a duplicate wearing a different label.
    """
    saturation = channel_cost_inr * book_size
    ladder = [0.0]
    ladder += [round(saturation * (1.4 ** (step - steps)), 4) for step in range(1, steps + 1)]
    return sorted(set(ladder))


class SweepPoint(BaseModel):
    budget_inr: float
    metrics: RunMetrics


class ArmSweep(BaseModel):
    """One arm's profit curve over the budget ladder."""

    arm: str
    points: list[SweepPoint]

    @property
    def optimum(self) -> SweepPoint:
        """The best point on the curve. Ties break toward the *cheaper* budget, so a flat
        top is reported at the point where the money stops being needed rather than where
        it stops being harmful."""
        return min(self.points, key=lambda p: (-p.metrics.profit_inr, p.budget_inr))

    @property
    def floor_profit_inr(self) -> float:
        """Profit at a budget of zero -- doing nothing. Every curve starts here."""
        return next(p.metrics.profit_inr for p in self.points if p.budget_inr == 0.0)

    @property
    def gain_over_floor_inr(self) -> float:
        return self.optimum.metrics.profit_inr - self.floor_profit_inr

    @property
    def optimum_is_doing_nothing(self) -> bool:
        """True when no budget beats spending none.

        Not a degenerate case to be tuned away. At the shipped parameters it is the
        finding: every rupee of ask budget destroys value, and the naive arms spend it
        anyway.
        """
        return self.optimum.budget_inr == 0.0

    def profit_near(self, budget_inr: float) -> SweepPoint:
        """The sampled point closest to a budget, on a log scale.

        The ladder is geometric, so "closest" has to be measured the same way -- nearest
        in rupees would always pick the larger neighbour.
        """
        target = max(budget_inr, 1e-9)
        # `abs(log(ratio))`, not `abs(ratio - 1)`. The second looks like a log metric and
        # is not symmetric: against a target of 4 it scores 1 at 0.75 and 10 at 1.5, so it
        # would call 1 the nearer point when 10 is two and a half times closer in the
        # scale the ladder is actually built on. A test caught it.
        return min(
            (p for p in self.points if p.budget_inr > 0),
            key=lambda p: abs(log(p.budget_inr / target)),
        )

    @property
    def asymmetry(self) -> tuple[float, float] | None:
        """Cost of under-asking and of over-asking, as shares of the optimum's gain.

        Zhang's shape: half the optimum spend costs about twice what double the optimum
        spend costs. Returned as `(under, over)` so the caller can state which mistake is
        the expensive one instead of assuming it.
        """
        if self.optimum_is_doing_nothing or self.gain_over_floor_inr <= 0:
            return None
        best = self.optimum.metrics.profit_inr
        under = self.profit_near(self.optimum.budget_inr / 2).metrics.profit_inr
        over = self.profit_near(self.optimum.budget_inr * 2).metrics.profit_inr
        return (
            (best - under) / self.gain_over_floor_inr,
            (best - over) / self.gain_over_floor_inr,
        )


def budget_sweep(
    book: list[BookMandate],
    params: Params,
    budgets: list[float],
    arms: list[Policy] | None = None,
) -> list[ArmSweep]:
    """Run every arm at every budget. The identical book and hazard path throughout."""
    policies = arms or [NoAskPolicy(), *(cls(params) for cls in ARMS.values())]
    return [
        ArmSweep(
            arm=policy.arm,
            points=[
                SweepPoint(budget_inr=budget, metrics=run(book, policy, params, budget))
                for budget in budgets
            ],
        )
        for policy in policies
    ]


def format_sweep(sweeps: list[ArmSweep]) -> str:
    lines = [
        "| arm | optimum budget | asks there | profit at optimum | gain over doing nothing "
        "| under-ask cost | over-ask cost |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for sweep in sweeps:
        best = sweep.optimum
        asymmetry = sweep.asymmetry
        under, over = (f"{asymmetry[0]:.1%}", f"{asymmetry[1]:.1%}") if asymmetry else ("--", "--")
        lines.append(
            f"| {sweep.arm} | INR {best.budget_inr:,.2f} | {best.metrics.asks_spent:,} "
            f"| INR {best.metrics.profit_inr:,.0f} | INR {sweep.gain_over_floor_inr:,.0f} "
            f"| {under} | {over} |"
        )
    return "\n".join(lines)


class GridCell(BaseModel):
    """One `(uplift, backfire)` point, with what each arm made there."""

    uplift_scale: float
    backfire_first_ask: float
    challenger_profit_inr: float
    reference_profit_inr: float
    floor_profit_inr: float
    challenger_asks: int

    @property
    def advantage_inr(self) -> float:
        """Challenger minus reference. Positive means selection beat rotation."""
        return self.challenger_profit_inr - self.reference_profit_inr

    @property
    def beats_reference(self) -> bool:
        return self.advantage_inr > 0

    @property
    def selection_paid(self) -> bool:
        """Whether the challenger actually bought any asks here.

        This is the line that matters when reading the plane, and it is not the same as
        `beats_reference`. `GreedyEV` only buys positive-value asks, so in the region
        where asking never pays it asks nobody -- and then it beats rotation by a wide
        margin *for not spending*, which is a real result but a completely different one
        from "selecting the right mandates works". The two are marked differently.
        """
        return self.challenger_asks > 0

    @property
    def beats_floor(self) -> bool:
        """Whether the challenger beat doing nothing. False when it declined to ask, since
        declining reproduces the floor exactly rather than beating it."""
        return self.challenger_profit_inr > self.floor_profit_inr


class Grid(BaseModel):
    challenger: str
    reference: str
    budget_inr: float
    cells: list[GridCell]

    @property
    def uplifts(self) -> list[float]:
        return sorted({cell.uplift_scale for cell in self.cells})

    @property
    def backfires(self) -> list[float]:
        return sorted({cell.backfire_first_ask for cell in self.cells})

    def cell(self, uplift: float, backfire: float) -> GridCell:
        return next(
            c for c in self.cells if c.uplift_scale == uplift and c.backfire_first_ask == backfire
        )

    @property
    def share_where_asking_pays(self) -> float:
        """Share of the plane where the challenger judged any ask worth making."""
        return sum(1 for c in self.cells if c.selection_paid) / len(self.cells)


def sensitivity_grid(
    book: list[BookMandate],
    params: Params,
    uplifts: list[float],
    backfires: list[float],
    budget_inr: float,
    challenger: PolicyFactory = GreedyEV,
    reference: PolicyFactory = RoundRobin,
) -> Grid:
    """Re-run the ladder at every point of the `(uplift x backfire)` plane.

    `backfire_twelfth_ask` moves with `backfire_first_ask` at the shipped 10x ratio. The
    grid is over *how irritating an ask is*, not over how fast irritation compounds --
    varying both independently would be a four-dimensional sweep whose extra two axes
    nobody could read off a heatmap.
    """
    ratio = params.intervention.backfire_twelfth_ask / params.intervention.backfire_first_ask
    cells = []
    for uplift in uplifts:
        for backfire in backfires:
            local = params.model_copy(
                update={
                    "intervention": params.intervention.model_copy(
                        update={
                            "uplift_scale": uplift,
                            "backfire_first_ask": backfire,
                            "backfire_twelfth_ask": min(1.0, backfire * ratio),
                        }
                    )
                }
            )
            challenger_run = run(book, challenger(local), local, budget_inr)
            reference_run = run(book, reference(local), local, budget_inr)
            floor_run = run(book, NoAskPolicy(), local, budget_inr)
            cells.append(
                GridCell(
                    uplift_scale=uplift,
                    backfire_first_ask=backfire,
                    challenger_profit_inr=challenger_run.profit_inr,
                    reference_profit_inr=reference_run.profit_inr,
                    floor_profit_inr=floor_run.profit_inr,
                    challenger_asks=challenger_run.asks_spent,
                )
            )
    return Grid(
        challenger=challenger(params).arm,
        reference=reference(params).arm,
        budget_inr=budget_inr,
        cells=cells,
    )


def format_grid(grid: Grid) -> str:
    """The plane as a table.

    Every cell shows the challenger's rupee advantage over the reference. A value in
    **(parentheses)** means the challenger declined to ask at all there -- its whole
    advantage came from *not spending*, not from choosing well. Distinguishing the two is
    the difference between "selection works" and "the reference is burning money", and
    only the first is a claim about our approach.
    """
    lines = [
        f"Challenger `{grid.challenger}` against reference `{grid.reference}`, at a budget "
        f"of INR {grid.budget_inr:,.2f}/week -- enough for one ask per mandate per week, "
        "so the reference contacts everyone.",
        "",
        "Cells give the challenger's advantage in rupees. **(parentheses)** mark cells "
        "where the challenger asked *nobody*: the advantage there is the reference's "
        "loss, not our gain.",
        "",
        "| uplift \\ backfire | " + " | ".join(f"{b:.4f}" for b in grid.backfires) + " |",
        "|---|" + "---:|" * len(grid.backfires),
    ]
    for uplift in grid.uplifts:
        row = [f"| **{uplift:.2f}**"]
        for backfire in grid.backfires:
            cell = grid.cell(uplift, backfire)
            value = f"{cell.advantage_inr:+,.0f}"
            row.append(f" | {value}" if cell.selection_paid else f" | ({value})")
        lines.append("".join(row) + " |")
    lines += [
        "",
        f"The challenger judged at least one ask worth making in "
        f"**{grid.share_where_asking_pays:.0%}** of the plane.",
    ]
    return "\n".join(lines)


def plot(sweeps: list[ArmSweep], grid: Grid, path: Path) -> Path:
    """Two panels: the budget curve per arm, and the plane. Written deterministically.

    Matplotlib stamps its own version into every PNG it writes, and this file is
    committed -- see ADR 0003 and `risk/calibration.py`.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ensure(path.parent)
    figure, (left, right) = plt.subplots(1, 2, figsize=(11.5, 4.6))

    for sweep in sweeps:
        left.plot(
            [max(p.budget_inr, 1e-2) for p in sweep.points],
            [p.metrics.profit_inr for p in sweep.points],
            marker="o",
            markersize=3.5,
            linewidth=1.4,
            label=sweep.arm,
        )
    left.set_xscale("log")
    left.set_xlabel("weekly ask budget (INR, log scale)")
    left.set_ylabel("profit: ARR retained less spend (INR)")
    left.set_title("T2.7 Budget curve")
    left.legend(frameon=False, fontsize=8)
    left.grid(True, which="both", linewidth=0.3, alpha=0.4)

    uplifts, backfires = grid.uplifts, grid.backfires
    matrix = [
        [grid.cell(uplift, backfire).advantage_inr for backfire in backfires] for uplift in uplifts
    ]
    limit = max(abs(value) for row in matrix for value in row) or 1.0
    image = right.imshow(
        matrix, origin="lower", aspect="auto", cmap="RdBu", vmin=-limit, vmax=limit
    )
    right.set_xticks(range(len(backfires)), [f"{b:.3f}" for b in backfires], fontsize=7)
    right.set_yticks(range(len(uplifts)), [f"{u:.2f}" for u in uplifts], fontsize=7)
    right.set_xlabel("backfire on the first ask")
    right.set_ylabel("uplift scale")
    right.set_title(f"T2.8 {grid.challenger} advantage over {grid.reference} (INR)")
    figure.colorbar(image, ax=right, shrink=0.85)

    figure.tight_layout()
    figure.savefig(path, dpi=140, metadata={"Software": None})
    plt.close(figure)
    return path
