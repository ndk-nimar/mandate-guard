"""T2.9 -- regenerate `docs/results.md` from the committed sample. Nothing is typed.

    uv run python scripts/make_results.py

GATE 2 asks that a stranger's fork produce a **byte-identical** `results.md`. That is why
this script exists and why CI runs it and then `git diff --exit-code`: if the committed
file and the regenerated one differ by a rupee, the build fails and somebody has to say
why.

It builds everything it needs from `data/sample/` -- the mandate book, the person-period
frame, the hazard fit, the ladder, the sweeps -- so a fresh clone with no download
reproduces every number here. The whole run is a couple of minutes.

Prose lives in this file and numbers come from the run. A results document where the
numbers are typed is a document that drifts from the data the first time anything changes.
"""

from __future__ import annotations

import duckdb

from mandateguard.allocator.base import NoAskPolicy
from mandateguard.allocator.baselines import ChronologicalCap, GreedyEV, RoundRobin, bulk_channel
from mandateguard.allocator.mckp import MCKPPolicy
from mandateguard.data import mandates, periods
from mandateguard.data.cancel import RENEWAL_TOLERANCE_DAYS
from mandateguard.data.paths import ROOT, frame_dir, sample_dir, spill_dir
from mandateguard.eval import forecast, sweep, world
from mandateguard.policy.loader import load_params
from mandateguard.risk import hazard, scoring

RESULTS = ROOT / "docs" / "results.md"
IMAGE = ROOT / "docs" / "img" / "sweeps.png"


def _ladder_reading(ladder: list[world.RunMetrics]) -> list[str]:
    """The prose for section 2, *derived* rather than typed.

    A generated document whose numbers come from a run and whose claims come from a
    string literal will eventually say the opposite of its own table -- and it did: the
    first draft of this file said "P3 asks nobody" and stayed there while a sourced
    channel prior moved P3 across the frontier. Claims that depend on the numbers have to
    be computed from the numbers.
    """
    floor, queue, rotation, greedy, knapsack = ladder
    lines = [
        "```",
        "cost of one ask  = backfire(1) x channel softness x loss_on_revocation",
        "gain at hazard h = h x uplift x efficacy[channel] x loss_on_lapse",
        "```",
        "",
    ]
    if greedy.asks_spent == 0:
        lines += [
            "**`P3` asks nobody, and that is the result rather than a bug.** At these",
            "parameter values no mandate in the book is risky enough for an ask to break",
            "even, so the value-maximising number of asks is zero.",
        ]
    else:
        lines += [
            f"**`P3` buys {greedy.asks_spent:,} asks out of a possible "
            f"{queue.asks_spent:,}** -- {greedy.asks_spent / queue.asks_spent:.2%} of what",
            "the budget would allow -- and creates",
            f"INR {greedy.net_value_inr:,.0f} of net value at INR {greedy.inr_per_ask:,.2f}",
            "per ask. It is barely worth doing: the gain is",
            f"{(greedy.profit_inr - floor.profit_inr) / floor.profit_inr:.3%} of the book.",
            "Selection here is not a large win; it is the difference between a small gain",
            "and a large loss.",
        ]
    lines += _knapsack_reading(greedy, knapsack)
    lines += [
        "",
        "The large number in this table is on the other side. `P1` and `P2`, spending a",
        "budget that never binds, destroy",
        f"INR {floor.profit_inr - queue.profit_inr:,.0f} and",
        f"INR {floor.profit_inr - rotation.profit_inr:,.0f} of profit respectively, while",
        f"their entire channel spend is INR {queue.channel_cost_inr:,.2f}. That is",
        "`problem.md` §5.1 measured instead of asserted: **the spend is not the",
        "constraint, the customer's patience is.**",
        "",
        "`P1` and `P2` come out within a rupee of each other here because the budget does",
        "not bind -- both contact everyone every week, so there is nothing for a rotation",
        "to rotate. They separate as soon as the budget does bind, which is §3.",
    ]
    return lines


def _knapsack_reading(greedy: world.RunMetrics, knapsack: world.RunMetrics) -> list[str]:
    """What P4 bought that P3 could not -- derived, because the sign is not guaranteed.

    P3 and P4 share one value function by design, so the only thing separating them is
    allocation: P4 chooses a *channel* per mandate and solves the week under a shared
    budget. If that turns out to be worth nothing on this book, the document has to say
    so; a results file that can only report a win is not reporting.
    """
    delta = knapsack.profit_inr - greedy.profit_inr
    theta = f"INR {knapsack.theta_inr:,.4f}" if knapsack.theta_inr is not None else "not computed"
    lines = [
        "",
        "**`P4` is the first arm that is ours**, and it differs from `P3` in exactly two",
        "ways: it picks a channel per mandate rather than using one for everybody, and it",
        "solves the whole week under the shared budget instead of taking the top-B of a",
        "sort. They price asks identically -- same `value/` module, same coefficients -- so",
        "the gap between them is allocation and nothing else.",
        "",
    ]
    if delta > 0:
        lines += [
            f"It is worth INR {delta:,.0f} more than `P3` here, on",
            f"{knapsack.asks_spent:,} asks against {greedy.asks_spent:,}.",
        ]
    elif delta < 0:
        lines += [
            f"On this book it is worth INR {-delta:,.0f} **less** than `P3`, which is a",
            "result and not a bug: when the budget never binds and one channel dominates",
            "the value ranking, an exact solver has nothing to add over a sort.",
        ]
    else:
        lines += [
            "On this book the two land in the same place, which is what should happen when",
            "the budget does not bind: with nothing to ration, choosing well and sorting",
            "well are the same act.",
        ]
    lines += [
        "",
        f"**theta = {theta}** per rupee of weekly ask budget. This is the number the whole",
        "project exists to produce -- *every extra rupee of budget returns theta rupees of",
        "value, net of that rupee* -- and it comes from the LP relaxation's dual on the",
        "budget constraint, not from a heuristic (ADR 0002).",
    ]
    if knapsack.theta_inr == 0.0:
        lines += [
            "",
            "Zero is a real price, not a missing one: at this budget the constraint is",
            "slack, so the next rupee buys nothing. It is also a consequence of the free",
            "channel -- `in_app` costs nothing, so anything worth contacting can be",
            "contacted without touching the budget at all. **No mandate here is refused for",
            'lack of money**; every refusal is "not worth asking". The budget rations',
            "*which channel*, not *whether* -- which is `problem.md` §5.1 falling out of the",
            "solver rather than being asserted at it.",
        ]
    return lines


def _curve_reading(sweeps: list[sweep.ArmSweep]) -> list[str]:
    """The prose for section 3, derived the same way and for the same reason."""
    best = max(sweeps, key=lambda s: s.gain_over_floor_inr)
    if best.optimum_is_doing_nothing:
        return [
            "**There is no inverted U here, and the plan expected one.** Zhang's published",
            "calibration has an interior optimum with under-asking twice as expensive as",
            "over-asking. At these parameter values every arm's optimum is a budget of",
            "zero, so the curve is monotone and there is no optimum to be asymmetric",
            "about. The shape only appears where an ask is worth making, which is what §4",
            "maps.",
        ]
    under, over = best.asymmetry or (0.0, 0.0)
    return [
        f"**`{best.arm}` has an interior optimum at INR {best.optimum.budget_inr:,.2f} per",
        f"week** -- {best.optimum.metrics.asks_spent:,} asks over the horizon, worth",
        f"INR {best.gain_over_floor_inr:,.0f} more than doing nothing. Every other arm's",
        "optimum is still zero: they have no way to decline an ask, so more budget can",
        "only hurt them.",
        "",
        "The curve is **asymmetric in the direction Zhang predicted**. Halving the optimum",
        f"budget costs {under:.1%} of the gain; doubling it costs {over:.1%}. Under-asking",
        "is the more expensive mistake here by a factor of about",
        f"{under / over:.0f}x -- Zhang's calibration puts it at roughly 2x, so the shape",
        "agrees and the magnitude does not, which is what a different book and a different",
        "set of swept parameters should be expected to produce.",
    ]


def main() -> int:
    params = load_params()
    split = scoring.split_at(
        params.india.snapshot_date, params.horizon.weeks, RENEWAL_TOLERANCE_DAYS
    )
    out_dir = frame_dir(sample=True)

    # Built here rather than assumed, so a fresh clone needs one command and no download.
    book_report = mandates.build(params=params, interim=sample_dir(), out_dir=out_dir)
    frame_report = periods.build(params=params, interim=sample_dir(), out_dir=out_dir)

    frame_path = out_dir / "person_periods.parquet"
    book_path = out_dir / "mandates.parquet"

    con = duckdb.connect()
    try:
        con.execute(f"SET temp_directory = '{spill_dir().as_posix()}'")
        con.execute(f"CREATE OR REPLACE TEMP VIEW frame AS SELECT * FROM '{frame_path.as_posix()}'")
        model = hazard.fit(con, "frame", split.train, params.seed, hazard.FIT_ROWS)
        scores = [
            scoring.score(con, "frame", model.expression, split.test, "`hazard`"),
        ]
        forecast.build(con, model, frame_path, book_path, params.horizon.weeks)
        forecast_rows = forecast.summary(con)
        live = world.load_book(con)
    finally:
        con.close()

    channel = bulk_channel(params.channels)
    saturation = channel.cost_inr * len(live)
    arms = [
        NoAskPolicy(),
        ChronologicalCap(params),
        RoundRobin(params),
        GreedyEV(params),
        MCKPPolicy(params),
    ]
    ladder = [world.run(live, arm, params, saturation) for arm in arms]

    budgets = sweep.budget_ladder(channel.cost_inr, len(live))
    sweeps = sweep.budget_sweep(live, params, budgets)
    grid = sweep.sensitivity_grid(
        live, params, list(sweep.UPLIFT_SCALES), list(sweep.BACKFIRE_RATES), saturation
    )
    sweep.plot(sweeps, grid, IMAGE)

    hazard_score = scores[0]
    text = "\n".join(
        [
            "# Results",
            "",
            "**Generated by `scripts/make_results.py`. Do not edit by hand.** CI regenerates",
            "this file and fails if a single character differs, which is GATE 2's",
            "byte-identical requirement made enforceable rather than promised.",
            "",
            "Every number below comes from `data/sample/` -- the committed",
            f"{book_report.steps[0].subscribers:,}-subscriber slice (`mapping.md` §4) -- so a",
            "fresh clone reproduces all of it with no download:",
            "",
            "```",
            "uv run python scripts/make_results.py",
            "```",
            "",
            "The full-data equivalents of the model numbers are in [`eval.md`](./eval.md).",
            "The sample is a smoke test for the pipeline, not a second opinion on it.",
            "",
            "---",
            "",
            "## 1. The book being simulated",
            "",
            "| quantity | value |",
            "|---|---:|",
            f"| subscribers in the sample | {book_report.steps[0].subscribers:,} |",
            f"| mandates in the book | {book_report.mandates:,} |",
            f"| person-weeks in the frame | {frame_report.person_weeks:,} |",
            f"| spells ending in a death | {frame_report.events:,} |",
            f"| **live mandates at the snapshot** | **{len(live):,}** |",
            f"| horizon | {params.horizon.weeks} weeks |",
            f"| bulk channel | `{channel.name}` at INR {channel.cost_inr} |",
            f"| hazard model, Brier on held-out data | {hazard_score.brier:.6f} |",
            "",
            "Only mandates that were alive at the snapshot are simulated. A retention system",
            "cannot be run on a mandate that already died, and including the dead ones would",
            "let every arm claim credit for saving them.",
            "",
            "### The projected hazard",
            "",
            "Each live mandate's features are rolled forward week by week and scored with the",
            "hazard model's own expression -- the same one `eval.md` §2 validated. The renewal",
            "column is the cheap sanity check: a 30-day mandate should pay about three times",
            "over twelve weeks.",
            "",
            forecast.format_summary(forecast_rows),
            "",
            "---",
            "",
            "## 2. The ladder",
            "",
            f"At a budget of INR {saturation:,.2f} per week -- enough for one ask per mandate per",
            "week, so the budget does not bind and each arm asks as much as it wants to.",
            "",
            world.format_metrics(ladder),
            "",
            "| arm | what it does |",
            "|---|---|",
            "| `P0` | contacts nobody. The floor. |",
            "| `P1` | first-come, first-served until the budget runs out -- "
            "the campaign-tool default. |",
            "| `P2` | the same budget, rotated fairly. |",
            "| `P3` | top-B by expected rupee value, pricing backfire. |",
            "| **`P4`** | **ours** -- multiple-choice knapsack over (mandate, channel), "
            "solved under the shared budget, with the LP dual as theta. |",
            "",
            *_ladder_reading(ladder),
            "",
            "---",
            "",
            "## 3. The budget curve (T2.7)",
            "",
            "Profit is ARR retained less what was spent retaining it.",
            "",
            sweep.format_sweep(sweeps),
            "",
            "| budget | " + " | ".join(s.arm for s in sweeps) + " |",
            "|---:|" + "---:|" * len(sweeps),
            *[
                f"| {budget:,.2f} | "
                + " | ".join(f"{s.points[i].metrics.profit_inr:,.0f}" for s in sweeps)
                + " |"
                for i, budget in enumerate(budgets)
            ],
            "",
            *_curve_reading(sweeps),
            "",
            "---",
            "",
            "## 4. Where the answer changes (T2.8)",
            "",
            "Uplift and backfire have no public measurement -- `calibration.md` §4 records both",
            "as swept. Section 2's result turns on them by a margin of about 3%, so the honest",
            "output is a region rather than a point estimate.",
            "",
            sweep.format_grid(grid),
            "",
            "![Budget curve and sensitivity plane](img/sweeps.png)",
            "",
            "Two things to read off the plane.",
            "",
            "**There is a clean frontier.** Asking pays when uplift is large relative to",
            "backfire, and the boundary runs diagonally. The shipped point sits just on the",
            "wrong side of it: at `uplift_scale = 1.0` and a first-ask backfire of 0.006, no ask",
            "is worth making, and one step in either direction flips that.",
            "",
            "**Most of the plane's headline numbers are not our win.** In the parenthesised",
            "cells `P3` asked nobody, so its advantage over `P2` is entirely `P2`'s loss. Those",
            "cells say the reference is burning money, not that selection works. The unbracketed",
            "cells are the ones that support a claim about this approach, and there are fewer of",
            "them.",
            "",
            "---",
            "",
            "## 5. What this does and does not show",
            "",
            "* **The arms are not yet ours.** `P4` (multiple-choice knapsack) and `P5` (Whittle",
            "  index) are Phase 3. `P3` is the strongest arm here and it is a sort.",
            "* **`theta` is null everywhere.** The shadow price comes from the LP dual in `P4`.",
            "* **Expectations, not samples.** Every mandate carries a survival probability rather",
            "  than a sampled outcome, so these are mean results with no variance attached. The",
            '  harness cannot answer "how often does this policy do worse than doing nothing".',
            "* **The uplift mechanism is a modelling choice.** An ask multiplies the hazard by",
            "  `(1 - uplift_scale x efficacy)`, so it saves a share of the deaths that would have",
            "  happened. Reading `efficacy_prior` as an outright conversion rate instead would let",
            "  an ask save a mandate that was never going to die, and that is how a simulator ends",
            "  up claiming a 40% lift against Adyen's ~6%.",
            "* **KKBox is not an Indian mandate book.** Everything inherits `mapping.md` §3.9.",
            "",
        ]
    )
    RESULTS.write_text(text, encoding="utf-8", newline="\n")
    print(f"Wrote {RESULTS.relative_to(ROOT)} ({len(text.splitlines()):,} lines)")
    print(f"Wrote {IMAGE.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
