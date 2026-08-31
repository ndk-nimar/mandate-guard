"""T3.5 tests -- the online rule, and what it costs to decide one mandate at a time.

T3.5's stated gate is "the online rule reproduces batch MCKP within a stated tolerance",
and stating the tolerance honestly turned out to be most of the work. The rule does not
reproduce the batch solver uniformly: it is exact where the budget is loose and loses
ground as it tightens, and the two mechanisms behind that are separable and both pinned
here.

The sharper claim, and the one that makes this a real equivalence rather than a
resemblance, is that at the same theta with the spend meter never binding, the online rule
reproduces the **unrepaired Lagrangian selection exactly** -- not approximately.
"""

from __future__ import annotations

import pytest

from mandateguard.allocator import candidates as candidate_set
from mandateguard.allocator import theta_search
from mandateguard.allocator.mckp import MCKPPolicy
from mandateguard.allocator.serving_rule import OnlineServing, ServingRule
from mandateguard.allocator.theta_search import ThetaSearch
from mandateguard.models import DecisionKind
from mandateguard.policy.loader import load_params
from mandateguard.value.price import Pricer
from tests.test_mckp import BOOK_SIZE, book

BINDING_BUDGETS = (0.30, 1.0, 2.0, 5.0, 20.0)


def pairs(params, entries, budget):
    return candidate_set.build(Pricer(params), params, entries, budget)


def asked(response):
    return [d for d in response.decisions if d.kind is DecisionKind.ASKED]


def serve(params, entries, theta, budget=float("inf"), reach_inr=None):
    """Walk the book one mandate at a time, carrying only a running spend total.

    This loop *is* the claim under test: nothing reads ahead, so a mandate's decision
    cannot depend on a mandate that has not arrived yet.

    `reach_inr` pins how far up the channel ladder the rule may look, independently of
    what it has left to spend. They are the same thing in production and have to be
    separable in a test, because a comparison against a batch selection is only about the
    algorithm if both range over the same channel menu.
    """
    rule = ServingRule(params, theta)
    costs = {c.name: c.cost_inr for c in params.channels}
    chosen, spent = {}, 0.0
    for entry in sorted(entries, key=lambda e: e.mandate_id):
        meter = budget - spent
        verdict = rule.decide(entry, meter if reach_inr is None else min(meter, reach_inr))
        if verdict.channel is not None:
            chosen[entry.mandate_id] = verdict
            spent += costs[verdict.channel]
    return chosen, spent


# --------------------------------------------------------------------------------
# The equivalence. This is the strong claim; the tolerance below is the weak one.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("budget", BINDING_BUDGETS)
def test_the_online_rule_reproduces_the_lagrangian_selection_exactly(budget):
    """Same theta, no cap: identical selection, mandate for mandate and channel for
    channel.

    Not "within a tolerance" -- identical. Both are computing `argmax_c (profit - theta *
    cost)` over the same candidate set, so anything less than exact agreement would mean
    the two disagree about a tie-break or about the candidate set, and either would make
    every softer comparison below meaningless.
    """
    params = load_params()
    entries = book()
    priced = pairs(params, entries, budget)
    theta = ThetaSearch(repair=False).search(priced, budget).theta_inr

    # Both sides get the same affordability bound. `candidates.build` drops channels the
    # bound cannot reach, so handing the rule an unlimited meter while the batch set was
    # built at `budget` would compare two different channel menus and call the difference
    # an algorithm. The meter is set generously so it never binds -- what is under test
    # here is the threshold, not the cap.
    batch = theta_search.select(priced, theta)
    online, _ = serve(params, entries, theta, budget, reach_inr=budget)

    assert set(online) == set(batch)
    assert {m: v.channel for m, v in online.items()} == {m: c.channel for m, c in batch.items()}


def test_at_theta_zero_the_rule_is_the_plain_linkedin_test():
    """`mu*P(re-consent) - nu*P(revoke) - cost > 0`, with no budget term at all.

    theta = 0 is the correct price of a slack budget, and at that price the fifth term
    vanishes and the rule collapses to the four-term threshold LinkedIn published. So the
    budget-aware rule contains the budget-free one rather than replacing it.
    """
    params = load_params()
    entries = book()
    online, _ = serve(params, entries, 0.0)
    pricer = Pricer(params)
    for entry in entries:
        best = pricer.best_channel(entry, float("inf"))
        if best is None:
            assert entry.mandate_id not in online
        else:
            assert online[entry.mandate_id].channel == best.channel


# --------------------------------------------------------------------------------
# T3.5's gate, and the two reasons it is not uniform.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("budget", BINDING_BUDGETS)
def test_the_online_rule_reproduces_batch_mckp_within_a_stated_tolerance(budget):
    """T3.5's gate. The tolerance is 10% of within-week value, and it is not tight
    because the shortfall is structural rather than sloppy -- see the next two tests for
    what the missing part is.
    """
    params = load_params()
    entries = book()
    priced = pairs(params, entries, budget)
    theta = ThetaSearch(repair=False).search(priced, budget).theta_inr

    online, spent = serve(params, entries, theta, budget)
    value = sum(v.price.net_inr for v in online.values() if v.price)
    exact = sum(d.value_inr for d in asked(MCKPPolicy(params).allocate(entries, budget, week=0)))

    assert spent <= budget + 1e-9
    assert value <= exact * 1.0001, "an online rule cannot beat an exact solve"
    assert value >= exact * 0.90, (
        f"budget INR {budget}: online found INR {value:.3f} against CBC's INR {exact:.3f}"
    )


def test_what_the_online_rule_gives_up_is_the_repair_which_is_inherently_batch():
    """The dominant half of the gap, named and measured.

    T3.4's bisection lands on a step of a step function and leaves slack; a greedy repair
    then spends it on the best upgrades that still fit. That pass **requires the whole
    book** -- it is ranking every mandate's available upgrade against every other's. An
    online rule cannot run it by construction, so it keeps the raw Lagrangian answer and
    the slack stays unspent.

    So this is not a deficiency of the implementation that a better online rule would fix.
    It is the price of the shape: seeing one mandate at a time costs exactly the part of
    the answer that needs to see them all.
    """
    params = load_params()
    entries = book()
    # A budget where the repair has work to do. At INR 0.30 on this fixture it finds no
    # upgrade at all, so a test written there would pass while proving nothing.
    budget = 2.0
    priced = pairs(params, entries, budget)
    raw = ThetaSearch(repair=False).search(priced, budget)
    repaired = ThetaSearch().search(priced, budget)
    assert repaired.upgrades > 0, "picked a budget where the repair does nothing"

    online, _ = serve(params, entries, raw.theta_inr, budget, reach_inr=budget)
    value = sum(v.price.net_inr for v in online.values() if v.price)

    assert value == pytest.approx(raw.value_inr), "online should match the *unrepaired* answer"
    assert repaired.value_inr > raw.value_inr, "the repair is what online cannot do"


def test_the_spend_meter_runs_down_in_arrival_order_and_that_costs_something():
    """The smaller half of the gap: whoever arrives first spends the money.

    The batch solver picks the best mandates in the book. The online rule cannot -- it
    meets them in arrival order and commits as it goes, so a high-value mandate arriving
    after the meter has run down gets a cheaper channel or nothing. That is exactly the
    failure mode `P1 ChronologicalCap` exists to represent, and it is worth knowing that
    the online rule inherits a trace of it no matter how good the price is.
    """
    params = load_params()
    entries = book()
    budget = 1.0
    theta = ThetaSearch(repair=False).search(pairs(params, entries, budget), budget).theta_inr

    uncapped, free_spend = serve(params, entries, theta, float("inf"))
    metered, spent = serve(params, entries, theta, budget * 0.25)

    assert spent <= budget * 0.25 + 1e-9
    assert free_spend > spent, "a tighter meter must actually bind for this to test anything"
    downgraded = [m for m, v in metered.items() if uncapped[m].channel != v.channel]
    assert downgraded, "the meter bound but nobody was downgraded"


# --------------------------------------------------------------------------------
# The guarantee the rule does not have, and what stands in for it.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("budget", (0.0, 0.05, 0.30, 2.0, 500.0))
def test_the_rule_never_overspends_its_meter(budget):
    """P4 cannot overspend because the budget is a constraint in its model. This rule has
    no model, so the meter is the only thing between it and `BudgetExceeded` -- which the
    harness raises rather than clipping, because an over-spending arm is a different
    experiment rather than a slightly worse one."""
    params = load_params()
    response = OnlineServing(params).allocate(book(), budget, week=0)
    costs = {c.name: c.cost_inr for c in params.channels}
    assert sum(costs[d.channel] for d in asked(response)) <= budget + 1e-9
    assert response.budget_spent_inr <= budget + 1e-9


def test_a_capped_decision_falls_back_to_a_channel_that_fits_and_says_so():
    """Refusing outright when the meter runs down would throw away a free ask the rule
    had already judged worth making. The fallback is recorded, because the cap rate is
    the measurement of how wrong the served price was."""
    params = load_params()
    entries = book()
    rule = ServingRule(params, theta_inr=0.0)
    wants = rule.decide(entries[-1], remaining_inr=float("inf"))
    assert wants.channel is not None and not wants.capped

    capped = rule.decide(entries[-1], remaining_inr=0.0)
    assert capped.capped
    if capped.channel is not None:
        assert capped.channel == "in_app", "the only channel that fits a zero meter"
        assert "capped" in capped.reason()


def test_every_mandate_gets_a_decision_including_the_refused():
    """The contract is total, the same as every other arm. A not-asked mandate is a
    record with a reason -- that is the refusal ledger."""
    params = load_params()
    response = OnlineServing(params).allocate(book(), 1.0, week=0)
    refused = [d for d in response.decisions if d.kind is DecisionKind.NOT_ASKED]
    assert len(response.decisions) == BOOK_SIZE
    assert len(refused) + len(asked(response)) == BOOK_SIZE
    assert all(d.reason for d in refused)
    assert all(d.channel is None for d in refused)


def test_a_refusal_names_the_price_that_caused_it():
    """ "The budget's own price exceeded what this ask was worth" is an explanation. A
    bare `False` is not, and `/explain` (T5.5) has to render one."""
    params = load_params()
    response = OnlineServing(params).allocate(book(), 0.30, week=0)
    reasons = [d.reason for d in response.decisions if d.kind is DecisionKind.NOT_ASKED]
    assert reasons
    assert any("priced at INR" in r for r in reasons)


# --------------------------------------------------------------------------------
# Staleness -- the risk the online shape imports.
# --------------------------------------------------------------------------------


def test_the_served_theta_is_held_rather_than_resolved_every_week():
    """What makes this online at all. Recomputing the dual per request would be P4 with
    extra steps, so the price is calibrated once and then read as a constant."""
    params = load_params()
    arm = OnlineServing(params)
    first = arm.allocate(book(week=0), 1.0, week=0).theta_inr
    second = arm.allocate(book(week=1), 1.0, week=1).theta_inr
    assert first is not None and first > 0
    assert second == first, "the price moved without anybody asking it to"


def test_recalibrating_puts_the_price_back_in_touch_with_the_book():
    """The axis `docs/eval.md` §5 measures. A price held across a moving book drifts from
    it; recalibrating is what a production system does on a schedule, and it is offline
    work rather than request work."""
    params = load_params()
    arm = OnlineServing(params, recalibrate_every=1)
    first = arm.allocate(book(week=0), 1.0, week=0).theta_inr
    second = arm.allocate(book(week=1), 5.0, week=1).theta_inr
    assert first is not None and second is not None
    assert second < first, "a five-times budget should have repriced cheaper"


def test_an_explicitly_supplied_theta_is_served_unchanged():
    """The production shape: a price computed by last night's batch job, handed in."""
    params = load_params()
    response = OnlineServing(params, theta_inr=3.5).allocate(book(), 1.0, week=0)
    assert response.theta_inr == 3.5


def test_the_same_week_serves_the_same_way_twice():
    """ADR 0003. The rule's tie-breaks are the same total order `theta_search.select`
    uses, for the same reason."""
    params = load_params()
    entries = book()
    first = OnlineServing(params).allocate(entries, 2.0, week=0)
    second = OnlineServing(params).allocate(entries, 2.0, week=0)
    assert first.model_dump() == second.model_dump()


def test_an_empty_book_is_not_an_error():
    response = OnlineServing(params=load_params()).allocate([], 5.0, week=0)
    assert response.decisions == []
    assert response.budget_spent_inr == 0.0
