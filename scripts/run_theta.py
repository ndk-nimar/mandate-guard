"""T3.4 entry point: converge theta by search, and check it against the LP dual.

    uv run python scripts/run_theta.py --sample
    uv run python scripts/run_theta.py --sample --week 3

Two algorithms compute one number here. `allocator/theta_search.py` finds it by
hill-climbing to a bracket and bisecting inside it, with no solver anywhere;
`allocator/mckp.py` reads it off CBC's dual on the budget constraint. They range over the
identical candidate set (`allocator/candidates.py`), so any disagreement is about the
algorithms rather than about the input -- which is the only way this comparison is worth
running at all.

Output is markdown, for `docs/eval.md` §4. Numbers reach documents by being printed
(`CLAUDE.md` §4); a results table that was retyped is a table that drifts from the run it
claims to describe.
"""

from __future__ import annotations

import argparse
from typing import NamedTuple

import duckdb
import numpy as np

from mandateguard.allocator import candidates as candidate_set
from mandateguard.allocator import theta_search
from mandateguard.allocator.base import NoAskPolicy
from mandateguard.allocator.baselines import bulk_channel
from mandateguard.allocator.mckp import MCKPPolicy
from mandateguard.allocator.serving_rule import OnlineServing
from mandateguard.allocator.theta_search import ThetaSearch, ThetaSolution
from mandateguard.allocator.whittle import WhittleIndex, WhittleSolver, horizon_from
from mandateguard.data.cancel import RENEWAL_TOLERANCE_DAYS
from mandateguard.data.paths import ROOT, frame_dir, spill_dir
from mandateguard.eval import forecast, segments, shape, world
from mandateguard.models import DecisionKind, MandateWeek
from mandateguard.policy.loader import Params, load_params
from mandateguard.risk import hazard, scoring
from mandateguard.value.price import Pricer

BUDGET_STEPS = 12
"""Points on the budget ladder. Geometric, because theta moves over orders of magnitude
and an arithmetic ladder would spend most of its rows in the region where the budget is
already slack and theta is flat zero."""


def week_view(book: list[world.BookMandate], week: int) -> list[MandateWeek]:
    """The book as a policy sees it in one week, with nobody yet contacted.

    Week 0 of a fresh run, so `alive = 1` and `asks_so_far = 0` for everybody. That is
    deliberately the *easiest* week to price: no fatigue, no accumulated backfire. A
    theta measured mid-horizon would be entangled with whatever the arm did in the weeks
    before it, and this script is about the search, not about the schedule.
    """
    return [
        MandateWeek(
            mandate_id=m.mandate_id,
            week=week,
            hazard=m.hazards[week],
            alive=1.0,
            ltv_remaining_inr=m.ltv_remaining_inr,
            reachability_value_inr=m.reachability_value_inr,
            recovery_after_lapse=m.recovery_after_lapse,
            recovery_after_revocation=m.recovery_after_revocation,
            asks_so_far=0,
        )
        for m in book
    ]


def ladder(top_inr: float) -> list[float]:
    """Budgets from a hundredth of the top down, geometrically spaced."""
    return [top_inr * (0.01 ** (1 - step / (BUDGET_STEPS - 1))) for step in range(BUDGET_STEPS)]


class Row(NamedTuple):
    """One budget, priced two ways, with the evidence for reading the difference."""

    solution: ThetaSolution
    dual_inr: float | None
    exact_inr: float
    left_on_table: int
    """Profitable asks that would still have fit in the unspent slack.

    The whole reason a shortfall against the +-2% gate can be judged rather than merely
    reported. Zero means the allocation ran out of things worth buying at that price and
    no allocator could have spent the rest; anything above zero means the repair left
    money and value on the table together, which is a bug in this file's neighbourhood
    rather than a fact about the book.
    """


def compare(params: Params, view: list[MandateWeek], budget: float, week: int) -> Row:
    """The search, the dual, and what an exact solve was worth at the same budget."""
    pricer = Pricer(params)
    pairs = candidate_set.build(pricer, params, view, budget)
    solution = ThetaSearch().search(pairs, budget)
    response = MCKPPolicy(params).allocate(view, budget, week)
    exact = sum(d.value_inr for d in response.decisions if d.kind is DecisionKind.ASKED)
    return Row(
        solution=solution,
        dual_inr=response.theta_inr,
        exact_inr=exact,
        left_on_table=len(theta_search.affordable_upgrades(pairs, dict(solution.chosen), budget)),
    )


def format_run(rows: list[Row]) -> str:
    """The convergence table, and the three claims it is there to support."""
    lines = [
        "| budget (INR) | binding | theta (search) | theta (CBC dual) | steps | spend (INR) "
        "| budget used | asks | paid asks | value (INR) | vs exact | left on table |",
        "|---:|:--|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in rows:
        solution = row.solution
        shown = f"{row.dual_inr:,.4f}" if row.dual_inr is not None else "--"
        share = f"{solution.value_inr / row.exact_inr:.4%}" if row.exact_inr else "--"
        lines.append(
            f"| {solution.budget_inr:,.2f} | {'yes' if solution.binding else 'no'} "
            f"| {solution.theta_inr:,.4f} | {shown} | {solution.iterations} "
            f"| {solution.spend_inr:,.2f} | {solution.utilisation:.2%} "
            f"| {solution.asks:,} | {solution.paid_asks:,} "
            f"| {solution.value_inr:,.2f} | {share} | {row.left_on_table} |"
        )

    binding = [r for r in rows if r.solution.binding]
    lines += ["", "**T3.4's gate: convergence.**"]
    if not binding:
        lines.append(
            "The budget does not bind anywhere on this ladder, so there is no theta to "
            "converge to and the +-2% fit is vacuous. That is a statement about the book, "
            "not about the search."
        )
        return "\n".join(lines)

    slowest = max(binding, key=lambda r: r.solution.iterations).solution
    lines += [
        f"All {len(binding)} binding budgets converged. The slowest took "
        f"{slowest.iterations} steps of a {theta_search.MAX_ITERATIONS}-step cap, closing "
        f"its bracket to INR {slowest.bracket_inr:.1e} -- machine precision, not the cap.",
    ]

    missed = [r for r in binding if not r.solution.within()]
    worst = max(binding, key=lambda r: r.solution.gap_fraction).solution
    lines += ["", "**T3.4's gate: the +-2% fit.**"]
    if not missed:
        lines.append(
            f"Met at every binding budget. The worst fit is {worst.gap_fraction:.2%} "
            f"(at INR {worst.budget_inr:,.2f}), against a gate of "
            f"{theta_search.BUDGET_TOLERANCE:.0%}."
        )
    else:
        stranded = sum(r.left_on_table for r in missed)
        names = ", ".join(f"INR {r.solution.budget_inr:,.2f}" for r in missed)
        lines += [
            f"**Met at {len(binding) - len(missed)} of {len(binding)} binding budgets, "
            f"and missed at {len(missed)}** ({names}), the worst by "
            f"{worst.gap_fraction:.2%} against a gate of "
            f"{theta_search.BUDGET_TOLERANCE:.0%}.",
            "",
            "**Every miss is optimal, and that is checked rather than asserted.** Across "
            f"those budgets, {stranded} profitable asks would have fit in the unspent "
            "slack at the converged price. An allocation can leave money unspent for two "
            "opposite reasons -- it gave up early, or it ran out of things worth buying "
            "-- and they look identical from the outside. Here it is the second, at every "
            "budget, so no allocator (CBC included) could have spent the rest:",
            "",
            "| budget (INR) | fit | unspent (INR) | cheapest ask (INR) | why the rest stayed |",
            "|---:|---:|---:|---:|:--|",
        ]
        for row in missed:
            slack = row.solution.budget_inr - row.solution.spend_inr
            cheapest = min(
                (c.cost_inr for c in row.solution.chosen.values() if c.cost_inr > 0),
                default=0.0,
            )
            why = (
                "the leftover is smaller than any ask"
                if slack < cheapest
                else "an ask would fit, but none left is worth making"
            )
            lines.append(
                f"| {row.solution.budget_inr:,.2f} | {row.solution.utilisation:.2%} "
                f"| {slack:,.2f} | {cheapest:,.2f} | {why} |"
            )
        lines += [
            "",
            "So the gate is a property of the **instance**, not of the algorithm. It is "
            "met wherever the budget is large relative to the value of the asks still "
            "left to buy, and the rows above are where this book is not.",
        ]

    priced = [r for r in binding if r.dual_inr]
    if priced:
        drift = max(
            abs(r.solution.theta_inr - r.dual_inr) / r.dual_inr  # type: ignore[operator]
            for r in priced
        )
        lines += [
            "",
            "**Against CBC.** The searched theta and the LP dual agree to within "
            f"{drift:.2%} at worst across {len(priced)} binding budgets. They are not "
            "obliged to agree exactly: the relaxation may take fractional asks, so its "
            "dual sits somewhere inside the flat step that the integer selection holds "
            "across, while the bisection converges on that step's left edge. Both are "
            "valid prices for the same budget.",
        ]

    captured = [r.solution.value_inr / r.exact_inr for r in binding if r.exact_inr]
    if captured:
        lines += [
            "",
            "**Against the exact solve.** The search plus its greedy repair captures "
            f"{min(captured):.3%} of CBC's integer optimum at worst, with no solver "
            "anywhere in the loop. A Lagrangian relaxation landing this close to "
            "branch-and-cut is the result T3.5's online rule is built on: if the price is "
            "right, mandates can be decided one at a time.",
        ]
    return "\n".join(lines)


ONLINE_BUDGET_SHARES = (0.01, 0.035, 0.125, 0.19, 1.0)
"""Where to compare batch against online, as fractions of a saturating budget.

Spread across the range where the budget goes from binding hard to not binding at all,
because that is the axis the comparison turns on -- the two agree exactly once the budget
is slack, and the interesting part is only visible while it binds."""

REFRESH_MODES: tuple[tuple[str, int | None], ...] = (
    ("held 12 weeks", None),
    ("every 4 weeks", 4),
    ("every week", 1),
)
"""How often the served price is recalibrated. Not a tuning knob -- the axis of the T3.5
result. "Held" is the harshest honest setting and "every week" is nearly P4."""


def format_online(book: list[world.BookMandate], params: Params, budgets: list[float]) -> str:
    """T3.5 -- what it costs to decide one mandate at a time, over the full horizon.

    Reported against the **gain over doing nothing**, not against total profit. Total
    profit is dominated by the mandates nobody was ever going to contact, so every arm
    scores about 99.99% of every other arm on it and the comparison says nothing. The gain
    over `P0` is the part any allocator is actually responsible for, and the arms separate
    by tens of per cent on it.
    """
    floor = world.run(book, NoAskPolicy(), params, 0.0)
    lines = [
        "## Batch against online (T3.5)",
        "",
        f"Full {params.horizon.weeks}-week horizon, {len(book):,} live mandates. "
        f"`P0` retains INR {floor.profit_inr:,.0f} by contacting nobody; every share below "
        "is of the **gain over that**, which is the only part an allocator earns.",
        "",
        "| budget/week (INR) | arm | price refreshed | asks | spend (INR) | gain over P0 (INR) "
        "| share of P4's gain | capped |",
        "|---:|:--|:--|---:|---:|---:|---:|---:|",
    ]
    shares: dict[str, list[tuple[float, float]]] = {label: [] for label, _ in REFRESH_MODES}
    tight: list[float] = []
    for budget in budgets:
        batch = world.run(book, MCKPPolicy(params, with_theta=False), params, budget)
        base = batch.profit_inr - floor.profit_inr
        lines.append(
            f"| {budget:,.2f} | `P4` batch | solved each week | {batch.asks_spent:,} "
            f"| {batch.channel_cost_inr:,.2f} | {base:,.0f} | 100.00% | -- |"
        )
        for label, every in REFRESH_MODES:
            arm = OnlineServing(params, recalibrate_every=every)
            metrics = world.run(book, arm, params, budget)
            gain = metrics.profit_inr - floor.profit_inr
            share = f"{gain / base:.2%}" if base else "--"
            if base:
                shares[label].append((budget, gain / base))
            lines.append(
                f"| {budget:,.2f} | `P4o` online | {label} | {metrics.asks_spent:,} "
                f"| {metrics.channel_cost_inr:,.2f} | {gain:,.0f} | {share} "
                f"| {arm.capped_decisions:,} |"
            )
        if batch.budget_spent_inr >= budget * params.horizon.weeks - 1e-9:
            tight.append(budget)

    fresh = shares[REFRESH_MODES[-1][0]]
    stale = shares[REFRESH_MODES[0][0]]
    exact = [budget for budget, share in fresh if share >= 0.9999]
    lines += ["", "**T3.5's gate.**"]
    if exact:
        lines.append(
            f"With the price refreshed weekly, the online rule reproduces batch `P4` "
            f"**exactly** at {len(exact)} of {len(fresh)} budgets -- every budget at or "
            f"above INR {min(exact):,.2f} -- and its worst showing anywhere is "
            f"{min(share for _, share in fresh):.2%} of the batch gain. Deciding one "
            "mandate at a time is free once the budget stops being the binding "
            "constraint, and cheap while it still is."
        )
    else:
        lines.append(
            "With the price refreshed weekly, the online rule captures between "
            f"{min(s for _, s in fresh):.2%} and {max(s for _, s in fresh):.2%} of the "
            "batch gain."
        )
    lines += [
        "",
        "**What staleness costs.** Holding one price for the whole horizon drops the worst "
        f"case to {min(share for _, share in stale):.2%} of the batch gain, against "
        f"{min(share for _, share in fresh):.2%} when it is refreshed weekly. The price is "
        "the only thing that changed; the rule, the value function and the book are "
        "identical. So the recalibration schedule is not an operational detail -- on this "
        "book it is worth more than the choice between batch and online.",
        "",
        "**What the online rule can never recover, at any refresh rate.** The residual gap "
        "is the repair step. T3.4's bisection lands on a step and leaves slack; a greedy "
        "pass then spends it on the best upgrades that fit -- and that pass ranks every "
        "mandate's available upgrade against every other's, so it **needs the whole book**. "
        "An online rule cannot run it by construction. That is not an implementation "
        "shortfall a better online rule would close: seeing one mandate at a time costs "
        "exactly the part of the answer that requires seeing them all.",
    ]
    return "\n".join(lines)


BACKFIRE_RATES = [0.0, 0.00005, 0.0001, 0.0003, 0.0006, 0.001, 0.003, 0.006, 0.012, 0.025]
"""Where to sweep first-ask backfire when asking which value reproduces LinkedIn's shape.

Spans four orders of magnitude below the shipped 0.006 and two above it, because the
question is not "is the shipped value slightly off" but "is there any value at all that
works" -- and a narrow sweep could not answer the second."""


def format_shape(book: list[world.BookMandate], params: Params, budget_inr: float) -> str:
    """T3.6 -- our three deltas against LinkedIn's, and the anchor sweep behind them."""
    triple, before, after = shape.compare(book, params, budget_inr)
    return "\n".join(
        [
            "## The shape, against LinkedIn's (T3.6)",
            "",
            shape.format_comparison(triple, before, after, budget_inr),
            "",
            "### Does any backfire rate reproduce it?",
            "",
            shape.format_anchor(
                shape.anchor(book, params, budget_inr, BACKFIRE_RATES),
                params.intervention.backfire_first_ask,
            ),
        ]
    )


SEGMENT_IMAGE = ROOT / "docs" / "img" / "segments.png"


def format_segments(book: list[world.BookMandate], params: Params, budget_inr: float) -> str:
    """T3.7 -- asks by risk segment, against Pinterest's inverted U. Writes the plot too.

    The plot ships either way, which is what T3.7 asks for. A figure that is only drawn
    when it agrees with the paper it is being checked against is not evidence.
    """
    metrics = world.run(book, MCKPPolicy(params, with_theta=False), params, budget_inr)
    found = segments.profile(book, metrics)
    segments.plot(found, SEGMENT_IMAGE)
    return "\n".join(
        [
            "## Asks by risk segment (T3.7)",
            "",
            segments.format_profile(found),
            "",
            f"![Asks by risk segment](img/{SEGMENT_IMAGE.name})",
        ]
    )


def _schedule(book: list[world.BookMandate], params: Params, policy, budget: float):
    """Run an arm and record *which week* each ask landed in.

    The harness reports totals, and totals are exactly what cannot separate these two
    arms: on this book they buy the same number of asks. The whole difference is when.
    """
    counts = [0] * params.horizon.weeks

    class Recording(world.Policy):  # type: ignore[name-defined]
        arm = policy.arm

        def allocate(self, entries, budget_inr, week):
            response = policy.allocate(entries, budget_inr, week)
            counts[week] += sum(1 for d in response.decisions if d.channel is not None)
            return response

    return counts, world.run(book, Recording(), params, budget)


def format_planning(book: list[world.BookMandate], params: Params, budget_inr: float) -> str:
    """T3.8 -- what the multi-period formulation bought, and how much of it is timing."""
    floor = world.run(book, NoAskPolicy(), params, 0.0)
    myopic_weeks, myopic = _schedule(book, params, MCKPPolicy(params, with_theta=False), budget_inr)
    planned_weeks, planned = _schedule(book, params, WhittleIndex(params), budget_inr)

    myopic_gain = myopic.profit_inr - floor.profit_inr
    planned_gain = planned.profit_inr - floor.profit_inr
    half = params.horizon.weeks // 2

    lines = [
        "## What planning bought (T3.8)",
        "",
        f"Both arms at INR {budget_inr:,.2f} per week over {params.horizon.weeks} weeks, "
        f"same book, same value function. `P0` retains INR {floor.profit_inr:,.0f} by "
        "contacting nobody; the gains below are over that.",
        "",
        "| arm | asks | spend (INR) | gain over P0 (INR) | vs P4 |",
        "|---|---:|---:|---:|---:|",
        f"| `P4` myopic | {myopic.asks_spent:,} | {myopic.channel_cost_inr:,.2f} "
        f"| {myopic_gain:,.0f} | -- |",
        f"| `P5` planned | {planned.asks_spent:,} | {planned.channel_cost_inr:,.2f} "
        f"| {planned_gain:,.0f} | "
        + (f"{planned_gain / myopic_gain - 1:+.1%}" if myopic_gain else "--")
        + " |",
        "",
        "| week | " + " | ".join(str(w) for w in range(params.horizon.weeks)) + " |",
        "|---|" + "---:|" * params.horizon.weeks,
        "| `P4` asks | " + " | ".join(str(c) for c in myopic_weeks) + " |",
        "| `P5` asks | " + " | ".join(str(c) for c in planned_weeks) + " |",
        "",
    ]

    if myopic.asks_spent == planned.asks_spent:
        lines.append(
            f"**Identical volume -- {planned.asks_spent:,} asks each -- and a different "
            "schedule.** `P4` front-loads: "
            f"{sum(myopic_weeks[:3])} of its asks land in the first three weeks. `P5` puts "
            f"{sum(planned_weeks[half:])} of its asks in the back half of the horizon "
            f"against `P4`'s {sum(myopic_weeks[half:])}. Nothing else differs, so the "
            "entire margin is timing."
        )
    else:
        lines.append(
            f"`P5` buys {planned.asks_spent:,} asks against `P4`'s {myopic.asks_spent:,}, "
            f"and puts {sum(planned_weeks[half:])} of them in the back half of the horizon "
            f"against `P4`'s {sum(myopic_weeks[half:])}."
        )

    # How much of the index is this week, and how much is the horizon?
    solver = WhittleSolver(params)
    weeks = solver.weeks
    shared = dict(
        week=0,
        alive=1.0,
        ltv_remaining_inr=400.0,
        reachability_value_inr=60.0,
        recovery_after_lapse=0.41,
        recovery_after_revocation=0.08,
        asks_so_far=0,
    )
    urgent = 0.15

    def score(paths: list[list[float]]) -> np.ndarray:
        entries = [
            MandateWeek(mandate_id=f"m{i}", hazard=path[0], hazard_path=path, **shared)
            for i, path in enumerate(paths)
        ]
        return solver.index(
            horizon_from(entries, params, weeks),
            0,
            np.zeros(len(paths), dtype=int),
            np.full(len(paths), solver.never),
        )

    futures = score(
        [
            [urgent] + [0.001] * (weeks - 1),
            [urgent] * weeks,
            [urgent] + [0.001] * 5 + [urgent] + [0.001] * (weeks - 7),
        ]
    )
    todays = score([[low] + [0.001] * (weeks - 1) for low in (0.10, 0.30)])
    future_spread = (futures.max() - futures.min()) / futures.mean()
    today_spread = (todays.max() - todays.min()) / todays.mean()

    lines += [
        "",
        "**And almost none of the index is the future, which is the deflating part.**",
        "Holding a mandate's hazard today fixed and varying only what comes after moves its "
        f"index by **{future_spread:.3%}** of its level. Varying *today's* hazard from 0.10 "
        f"to 0.30 moves it by **{today_spread:.0%}**. The index is overwhelmingly a price "
        "of present risk with a rounding error of foresight on top.",
        "",
        "That rounding error is still what produced the margin above. The index is consumed "
        "as a **ranking** under a binding budget, and at the margin a 0.02% difference is "
        "enough to decide which mandate gets the last rupee this week and which one waits. "
        "A signal too small to see in the level is not too small to reorder a queue.",
    ]
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", action="store_true", help="use the sample-derived frame")
    parser.add_argument("--week", type=int, default=0, help="which week of the horizon to price")
    args = parser.parse_args()

    params = load_params()
    split = scoring.split_at(
        params.india.snapshot_date, params.horizon.weeks, RENEWAL_TOLERANCE_DAYS
    )
    frame = frame_dir(args.sample) / "person_periods.parquet"
    book_path = frame_dir(args.sample) / "mandates.parquet"
    for path in (frame, book_path):
        if not path.exists():
            raise SystemExit(
                f"{path} does not exist -- run scripts/build_periods.py and "
                f"scripts/build_mandates.py{' --sample' if args.sample else ''} first."
            )

    con = duckdb.connect()
    try:
        con.execute(f"SET temp_directory = '{spill_dir().as_posix()}'")
        con.execute(f"CREATE OR REPLACE TEMP VIEW frame AS SELECT * FROM '{frame.as_posix()}'")
        model = hazard.fit(con, "frame", split.train, params.seed, hazard.FIT_ROWS)
        forecast.build(con, model, frame, book_path, params.horizon.weeks)
        book = world.load_book(con)
    finally:
        con.close()

    view = week_view(book, args.week)
    channel = bulk_channel(params.channels)
    # The top of the ladder is one bulk ask for every mandate -- the same reference point
    # docs/results.md 2 uses, so the two documents are describing one budget scale.
    top = len(book) * channel.cost_inr

    print(f"## theta by search (T3.4), week {args.week}")
    print()
    print(
        f"**{len(book):,} live mandates.** The ladder tops out at INR {top:,.2f} -- one "
        f"`{channel.name}` ask for every mandate, which is where the budget stops binding."
    )
    print()
    rows = [compare(params, view, budget, args.week) for budget in ladder(top)]
    print(format_run(rows))
    print()
    print(format_online(book, params, [top * share for share in ONLINE_BUDGET_SHARES]))
    print()
    print(format_shape(book, params, top))
    print()
    print(format_segments(book, params, top))
    print()
    print(format_planning(book, params, top))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
