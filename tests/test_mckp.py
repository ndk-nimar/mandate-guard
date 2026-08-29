"""P4 tests (T3.3).

T3.3's stated gate is "the solver returns a feasible within-budget allocation on a
100-mandate fixture", and that fixture is built here rather than borrowed: 100 mandates
with a spread of hazards and values is the smallest thing that makes the knapsack a real
choice, and small enough that feasibility can be checked exhaustively rather than sampled.

The rest is the two properties that separate P4 from a sort -- it picks a *channel*, and
it prices the budget -- plus the arithmetic that would make theta meaningless if it were
wrong.
"""

from __future__ import annotations

import pytest

from mandateguard.allocator.baselines import GreedyEV
from mandateguard.allocator.mckp import MCKPPolicy
from mandateguard.models import DecisionKind, MandateWeek
from mandateguard.policy.loader import load_params
from tests.test_world import make_params

BOOK_SIZE = 100


def book(size: int = BOOK_SIZE, week: int = 0) -> list[MandateWeek]:
    """`size` mandates whose risk and value both vary, so selection has work to do.

    Hazards run from near-zero to near-certain and LTV from small to large, which is what
    makes some mandates worth an agent call, some worth an email, and most worth nothing.
    A fixture where every mandate is identical would let any arm score full marks.
    """
    return [
        MandateWeek(
            mandate_id=f"m{index:03d}",
            week=week,
            hazard=0.002 + 0.9 * (index / size),
            alive=1.0,
            ltv_remaining_inr=100.0 + 400.0 * ((index * 7) % size) / size,
            reachability_value_inr=15.0,
            recovery_after_lapse=0.41,
            recovery_after_revocation=0.08,
            asks_so_far=index % 3,
        )
        for index in range(size)
    ]


def asked(response) -> list:
    return [d for d in response.decisions if d.kind is DecisionKind.ASKED]


# --------------------------------------------------------------------------------
# T3.3's stated gate.
# --------------------------------------------------------------------------------


def test_the_allocation_is_feasible_and_within_budget_on_a_hundred_mandates():
    """The gate T3.3 names, checked as three separate claims rather than one.

    Feasible means: every mandate gets a decision, no mandate is contacted twice in one
    week, and the spend does not exceed the budget. A solver that returned a great
    objective while breaking any of the three would be returning an answer to a different
    problem.
    """
    params = load_params()
    budget = 5.0
    response = MCKPPolicy(params).allocate(book(), budget, week=0)

    assert len(response.decisions) == BOOK_SIZE
    contacted = [d.mandate_id for d in asked(response)]
    assert len(contacted) == len(set(contacted)), "a mandate was contacted twice in one week"

    costs = {c.name: c.cost_inr for c in params.channels}
    spend = sum(costs[d.channel] for d in asked(response))
    assert spend <= budget + 1e-9
    assert response.budget_spent_inr == pytest.approx(spend)


def test_every_mandate_gets_a_decision_including_the_ones_refused():
    """The contract is total. A not-asked mandate is a record with a reason -- that is
    the refusal ledger, and it is most of what this system produces."""
    response = MCKPPolicy(load_params()).allocate(book(), 5.0, week=0)
    refused = [d for d in response.decisions if d.kind is DecisionKind.NOT_ASKED]
    assert len(refused) + len(asked(response)) == BOOK_SIZE
    assert all(d.reason for d in refused)
    assert all(d.channel is None for d in refused)


def test_a_budget_of_nothing_still_allows_the_free_channel():
    """`docs/problem.md` §5.3: a zero-cost channel does not consume budget, so a merchant
    with no ask budget at all can still send an in-app nudge. What stops it spamming is
    fatigue and backfire, not money -- which is the project's whole thesis about where
    the constraint really lives."""
    response = MCKPPolicy(load_params()).allocate(book(), 0.0, week=0)
    assert response.budget_spent_inr == pytest.approx(0.0)
    assert all(d.channel == "in_app" for d in asked(response))
    assert len(asked(response)) < BOOK_SIZE, "a free channel is still rationed by patience"


# --------------------------------------------------------------------------------
# What makes it a knapsack rather than a sort.
# --------------------------------------------------------------------------------


def test_a_bigger_budget_buys_more_expensive_channels():
    """The multiple-choice half. With money, the high-value mandates move up the ladder;
    a sort over one channel cannot do this at any budget."""
    params = load_params()
    costs = {c.name: c.cost_inr for c in params.channels}
    dearest = lambda response: max(costs[d.channel] for d in asked(response))  # noqa: E731
    poor = MCKPPolicy(params).allocate(book(), 0.5, week=0)
    rich = MCKPPolicy(params).allocate(book(), 500.0, week=0)
    assert dearest(rich) > dearest(poor), (
        f"a 1000x budget bought nothing dearer than {dearest(poor)}"
    )


def test_no_mandate_is_offered_two_channels_at_once():
    """The at-most-one constraint. Without it a mandate could be emailed, texted and
    telephoned in the same week, which is not a plan."""
    response = MCKPPolicy(load_params()).allocate(book(), 500.0, week=0)
    contacted = [d.mandate_id for d in asked(response)]
    assert len(contacted) == len(set(contacted))


def test_the_solver_never_buys_an_ask_that_loses_money():
    """The objective is the *net* price, with channel cost already subtracted. The first
    draft used the gross profit and P4 bought 258 asks for a negative net value, because
    a pair can be gross-positive and net-negative and a gross objective cannot tell."""
    response = MCKPPolicy(load_params()).allocate(book(), 500.0, week=0)
    assert all(d.value_inr > 0 for d in asked(response))


def test_p4_finds_at_least_as_much_value_as_the_greedy_sort():
    """Not a claim that P4 is better in the ladder -- that is a multi-week result. This
    is the weaker, checkable claim: on one week at one budget, an exact solver over the
    same value function cannot do worse than a greedy sort restricted to one channel."""
    params = load_params()
    entries = book()
    knapsack = MCKPPolicy(params).allocate(entries, 5.0, week=0)
    greedy = GreedyEV(params).allocate(entries, 5.0, week=0)
    assert sum(d.value_inr for d in asked(knapsack)) >= sum(d.value_inr for d in asked(greedy))


# --------------------------------------------------------------------------------
# theta.
# --------------------------------------------------------------------------------


def test_theta_is_positive_when_the_budget_binds():
    """The headline number of the whole project: "the next rupee of ask budget returns
    theta rupees". If it is not positive on a budget that binds, there is no number."""
    theta = MCKPPolicy(load_params()).allocate(book(), 0.30, week=0).theta_inr
    assert theta is not None
    assert theta > 0


def test_theta_is_zero_when_the_budget_does_not_bind():
    """Zero is the true price of a rupee nobody needs, and it is a real answer rather
    than a failure. CBC hands back a small negative dual there; publishing "-0.00" as a
    price would be a solver artefact wearing a finding's clothes."""
    theta = MCKPPolicy(load_params()).allocate(book(), 10_000.0, week=0).theta_inr
    assert theta == 0.0


def test_theta_falls_as_the_budget_relaxes():
    """A shadow price has to get cheaper as the constraint loosens.

    Asserted as a trend rather than pairwise, and the reason is LP not laziness: the
    knapsack's value function is concave and *piecewise linear*, so at a kink the
    subdifferential is an interval and CBC may report either one-sided derivative. Two
    adjacent budgets can therefore come back out of order without anything being wrong.
    Over a 250x range there is no such excuse.
    """
    params = load_params()
    entries = book()
    thetas = [
        MCKPPolicy(params).allocate(entries, budget, week=0).theta_inr
        for budget in (0.20, 1.0, 5.0, 50.0)
    ]
    assert all(t is not None for t in thetas)
    assert thetas[0] > thetas[-1] * 10, thetas
    assert thetas[1] > thetas[2] > thetas[3], thetas


def test_theta_predicts_what_another_rupee_of_budget_actually_buys():
    """The assertion that makes theta a price rather than a number. Spike S1 established
    it on a five-mandate toy; this is the same claim on the hundred-mandate fixture.

    A dual that exists but does not predict the objective's response would be useless for
    the "the next ask is worth INR X" claim, which is the whole reason it is computed.
    """
    params = load_params()
    entries = book()
    policy = MCKPPolicy(params)
    budget = 1.0
    theta = policy.allocate(entries, budget, week=0).theta_inr
    assert theta is not None and theta > 0

    delta = 0.05
    base = sum(d.value_inr for d in asked(policy.allocate(entries, budget, week=0)))
    bumped = sum(d.value_inr for d in asked(policy.allocate(entries, budget + delta, week=0)))
    assert bumped - base == pytest.approx(theta * delta, rel=0.5), (
        f"theta={theta} predicted {theta * delta:.4f}, got {bumped - base:.4f}"
    )


def test_theta_can_be_switched_off_for_the_sweeps():
    """The sweeps run this arm dozens of times to answer questions about *parameters*,
    and theta is a headline nobody reads off a heatmap cell. Skipping the relaxation is
    a deliberate saving, not a lost feature."""
    response = MCKPPolicy(load_params(), with_theta=False).allocate(book(), 0.30, week=0)
    assert response.theta_inr is None
    assert asked(response), "skipping the dual must not change the allocation"


# --------------------------------------------------------------------------------
# Reasons and reproducibility.
# --------------------------------------------------------------------------------


def test_a_free_channel_means_nobody_is_ever_refused_for_lack_of_budget():
    """A sharper consequence of `problem.md` §5.3 than it first looks.

    `in_app` costs nothing, so anything worth contacting at all can be contacted for
    free. Every refusal is therefore "not worth asking" and never "we ran out of money":
    the budget rations *which channel*, not *whether*. That is §5.1's thesis falling out
    of the solver rather than being asserted at it.
    """
    response = MCKPPolicy(load_params()).allocate(book(), 0.10, week=0)
    reasons = [d.reason for d in response.decisions if d.kind is DecisionKind.NOT_ASKED]
    assert reasons
    assert all(r.startswith("not asked: via") for r in reasons)
    assert not any("lost the budget" in r for r in reasons)


def test_without_a_free_channel_a_refusal_can_say_it_was_outbid():
    """The other refusal, on a ladder where it is reachable. Conflating the two would
    make the ledger useless -- "outbid" is the sentence theta exists to make actionable,
    because it is the one a bigger budget would change."""
    params = load_params()
    paid_only = params.model_copy(
        update={"channels": [c for c in params.channels if c.cost_inr > 0]}
    )
    response = MCKPPolicy(paid_only).allocate(book(), 0.10, week=0)
    reasons = [d.reason for d in response.decisions if d.kind is DecisionKind.NOT_ASKED]
    assert any("lost the budget to higher-value mandates" in r for r in reasons)


def test_an_asked_decision_explains_itself_in_rupees():
    response = MCKPPolicy(load_params()).allocate(book(), 500.0, week=0)
    reason = asked(response)[0].reason
    assert "is worth INR" in reason
    assert "revocation risk" in reason


def test_the_same_week_solves_the_same_way_twice():
    """ADR 0003. CBC may choose any one of several equally-good optima, so the variables
    are built in a fixed `(mandate_id, channel)` order -- a model whose rows arrive
    differently each run is free to hand back a different answer."""
    params = load_params()
    entries = book()
    first = MCKPPolicy(params).allocate(entries, 2.0, week=0)
    second = MCKPPolicy(params).allocate(entries, 2.0, week=0)
    assert first.model_dump() == second.model_dump()


def test_an_empty_book_is_not_an_error():
    response = MCKPPolicy(make_params()).allocate([], 5.0, week=0)
    assert response.decisions == []
    assert response.theta_inr is None
    assert response.budget_spent_inr == 0.0
