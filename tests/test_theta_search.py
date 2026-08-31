"""T3.4 tests -- the shadow price computed as an algorithm rather than read off a solver.

T3.4's stated gate is "theta converges and total asks land within +-2% of budget", and
that is asserted here directly. But the gate is the weaker half of what these tests do.
The strong claim is that this hand-rolled bisection lands on the *same number* CBC's LP
relaxation reports as its dual -- two independent algorithms agreeing on one price is
evidence about the price, whereas either one on its own is evidence about the code.

The 100-mandate fixture is shared with `tests/test_mckp.py` on purpose. Comparing the two
solvers on two different books would compare the books.
"""

from __future__ import annotations

import pytest

from mandateguard.allocator import candidates as candidate_set
from mandateguard.allocator import theta_search
from mandateguard.allocator.mckp import MCKPPolicy
from mandateguard.allocator.theta_search import ThetaSearch
from mandateguard.models import DecisionKind
from mandateguard.policy.loader import load_params
from mandateguard.value.price import Pricer
from tests.test_mckp import BOOK_SIZE, book

# Budgets at which the 100-mandate fixture's budget genuinely binds. Chosen by running
# the search, not by taste: above roughly INR 156 the whole book takes its best channel
# and the constraint goes slack, at which point theta is correctly zero and there is
# nothing for a convergence test to converge to.
BINDING_BUDGETS = (0.10, 0.30, 1.0, 2.0, 5.0, 10.0, 20.0, 50.0)


def pairs(params, entries, budget):
    return candidate_set.build(Pricer(params), params, entries, budget)


def paid_only(params):
    """The channel ladder with the free rung removed.

    `in_app` costs nothing, so with it configured a mandate priced out of every paid
    channel still gets contacted, and "asks match the budget" stops being a statement
    about asks at all. Several claims here are about what the *budget* rations, and they
    need a ladder where the budget is the only thing rationing.
    """
    return params.model_copy(update={"channels": [c for c in params.channels if c.cost_inr > 0]})


# --------------------------------------------------------------------------------
# T3.4's stated gate.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("budget", BINDING_BUDGETS)
def test_the_search_converges_and_lands_within_two_percent_of_budget(budget):
    """The gate T3.4 names, at every budget where the constraint actually binds.

    Both halves matter and they are not the same claim. `converged` says the bisection
    closed its bracket rather than running out of iterations; `within()` says the theta
    it closed on actually spends the money. A search can converge cleanly onto a price
    that leaves a fifth of the budget unspent -- that is the step problem, and it is why
    the fit is asserted separately from the convergence.
    """
    params = load_params()
    solution = ThetaSearch().search(pairs(params, book(), budget), budget)

    assert solution.binding, f"budget {budget} did not bind; the gate is vacuous there"
    assert solution.converged
    assert solution.within(), (
        f"budget INR {budget}: spent INR {solution.spend_inr:.2f}, {solution.gap_fraction:.2%} off"
    )


def test_theta_is_positive_when_the_budget_binds_and_zero_when_it_does_not():
    """Zero is a price, not a missing value: a slack constraint is worth nothing at the
    margin because the next rupee buys nothing."""
    params = load_params()
    entries = book()
    tight = ThetaSearch().search(pairs(params, entries, 0.30), 0.30)
    loose = ThetaSearch().search(pairs(params, entries, 10_000.0), 10_000.0)

    assert tight.binding and tight.theta_inr > 0
    assert not loose.binding and loose.theta_inr == 0.0


def test_a_slack_budget_reports_no_gap_rather_than_a_large_one():
    """The misreading T3.4's "+-2%" gate invites, closed off in the type.

    An unspent budget is the answer when nothing more is worth buying, so reporting the
    shortfall as a miss would make a correct result look like a convergence failure --
    and this book is slack at the shipped budget, so that reading would be the *usual*
    one rather than an edge case.
    """
    params = load_params()
    solution = ThetaSearch().search(pairs(params, book(), 10_000.0), 10_000.0)
    assert not solution.binding
    assert solution.spend_inr < 10_000.0
    assert solution.gap_fraction == 0.0
    assert solution.within()


# --------------------------------------------------------------------------------
# The claim that makes the number trustworthy: it agrees with CBC.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("budget", BINDING_BUDGETS)
def test_the_searched_theta_agrees_with_the_lp_dual(budget):
    """Two algorithms, one number. This is the strongest check either one gets.

    Asserted with a tolerance, and the tolerance is LP rather than laziness. The LP
    relaxation may take fractional asks, so its spend moves *continuously* with theta and
    its dual sits somewhere inside the flat step that the integer selection holds across.
    The bisection converges to that step's left edge. Both are valid prices for the same
    budget; they can differ by the width of a step, which on this fixture is a few per
    cent.

    The repair step is off here on purpose. Repair changes the *allocation* to use the
    slack; it must not be allowed to move the *price*, and running it would make a
    disagreement impossible to attribute to either one.
    """
    params = load_params()
    entries = book()
    searched = ThetaSearch(repair=False).search(pairs(params, entries, budget), budget)
    dual = MCKPPolicy(params).allocate(entries, budget, week=0).theta_inr

    assert dual is not None and dual > 0
    assert searched.theta_inr == pytest.approx(dual, rel=0.10), (
        f"budget INR {budget}: search said {searched.theta_inr}, CBC said {dual}"
    )


@pytest.mark.parametrize("budget", BINDING_BUDGETS)
def test_the_searched_allocation_is_worth_about_what_cbc_finds(budget):
    """The other half: the same price should buy about the same basket.

    CBC solves the integer problem exactly, so it is an upper bound and the search cannot
    beat it by more than solver tolerance. What is worth asserting is how *close* a
    Lagrangian relaxation plus a greedy repair gets without a solver in the loop -- and
    the answer on this fixture is within a fraction of a per cent, which is the finding
    T3.5's online-versus-batch comparison is built on.
    """
    params = load_params()
    entries = book()
    searched = ThetaSearch().search(pairs(params, entries, budget), budget)
    response = MCKPPolicy(params).allocate(entries, budget, week=0)
    exact = sum(d.value_inr for d in response.decisions if d.kind is DecisionKind.ASKED)

    assert searched.value_inr <= exact * 1.0001, "the relaxation cannot beat the exact solve"
    assert searched.value_inr >= exact * 0.99, (
        f"budget INR {budget}: search found INR {searched.value_inr:.3f} against CBC's "
        f"INR {exact:.3f}"
    )


# --------------------------------------------------------------------------------
# The property bisection depends on. Without it the whole method is unsound.
# --------------------------------------------------------------------------------


def test_spend_never_rises_as_theta_rises():
    """Bisection is only valid on a monotone function, so the monotonicity is checked
    rather than assumed.

    The proof is in the module docstring -- each mandate's reduced value is an upper
    envelope of lines with slopes `-k[c]`, so a rising theta can only move the argmax to
    a flatter, cheaper line or off the bottom entirely. This samples it densely enough to
    catch a tie-break that pointed the wrong way, which is the one implementation mistake
    that would break the proof without breaking anything visible: taking the *dearer*
    channel at a crossing point puts a step up into a function assumed to only step down.
    """
    params = load_params()
    entries = pairs(params, book(), 5.0)
    previous = None
    for step in range(2001):
        spend = theta_search.spend_at(entries, step * 0.05)
        if previous is not None:
            assert spend <= previous + 1e-12, f"spend rose at theta={step * 0.05}"
        previous = spend


def test_the_selection_never_overspends_at_any_budget():
    """Feasibility, which the search must guarantee rather than usually achieve. The
    harness raises `BudgetExceeded` rather than clipping, so an arm that overspends is
    not a slightly-wrong arm, it is a different experiment."""
    params = load_params()
    entries = book()
    for budget in (0.0, 0.04, 0.10, 1.0, 7.5, 100.0, 10_000.0):
        solution = ThetaSearch().search(pairs(params, entries, budget), budget)
        assert solution.spend_inr <= budget + 1e-9


def test_a_tighter_budget_prices_higher_when_the_menu_is_held_fixed():
    """A shadow price has to rise as the constraint tightens -- *at a fixed channel menu*.

    The qualifier is not a hedge, it is the finding. `candidates.build` drops channels
    the whole budget cannot afford, so widening the budget does not merely buy more of
    the same asks: it puts new, dearer, more effective rungs on the ladder. See
    `test_widening_the_budget_can_raise_theta_by_unlocking_a_channel` for what that does.

    Here the menu is pinned by building every candidate set at the widest budget, which
    isolates the claim that bisection is actually inverting a monotone function.
    """
    params = load_params()
    entries = book()
    menu = pairs(params, entries, 50.0)
    thetas = [
        ThetaSearch(repair=False).search(menu, budget).theta_inr
        for budget in (0.50, 1.0, 2.0, 5.0, 10.0, 50.0)
    ]
    assert thetas[0] > thetas[-1], thetas
    assert all(a >= b for a, b in zip(thetas, thetas[1:], strict=False)), thetas


def test_widening_the_budget_can_raise_theta_by_unlocking_a_channel():
    """theta is **not** monotone in the budget once the menu is allowed to move, and the
    mechanism is economic rather than numerical.

    `candidates.build` skips any channel costing more than the whole budget -- correctly,
    since a INR 0.10 weekly budget cannot buy a INR 0.15 SMS for anybody. So raising the
    budget from INR 0.10 to INR 0.20 does two things at once: it buys more asks, which
    pushes theta *down*, and it makes SMS purchasable at all, which pushes theta *up*
    because the marginal rupee can now buy a better thing than it could before. On this
    fixture the second effect wins, and theta rises from about 38 to about 42.

    That matters beyond this test. It means a theta published at one budget is not
    comparable with a theta published at another unless the affordable menu is the same,
    so the budget curve in `docs/results.md` §3 compares allocations at different menus
    by construction. Pinning it as a test keeps the next reader from filing the zig-zag
    as a convergence bug and "fixing" a real effect out of the model.
    """
    params = load_params()
    entries = book()
    narrow = pairs(params, entries, 0.10)
    wider = pairs(params, entries, 0.20)
    assert {c.channel for c in narrow} == {"in_app", "email"}
    assert {c.channel for c in wider} == {"in_app", "email", "sms"}

    cheap = ThetaSearch(repair=False).search(narrow, 0.10).theta_inr
    dear = ThetaSearch(repair=False).search(wider, 0.20).theta_inr
    assert dear > cheap, (
        f"unlocking SMS did not raise the marginal value of a rupee: {cheap} -> {dear}"
    )


# --------------------------------------------------------------------------------
# The hill-climb, and where the method stops working.
# --------------------------------------------------------------------------------


def test_the_hill_climb_brackets_the_answer_from_the_candidate_set_alone():
    """No magic constant, no unit. The climb starts at the smallest `profit / cost` ratio
    and stops at the largest, and both come out of the candidates -- so the search takes
    the same number of steps whether the book is priced in rupees or in paise."""
    params = load_params()
    entries = pairs(params, book(), 1.0)
    ratios = theta_search._ratios(entries)

    solution = ThetaSearch(repair=False).search(entries, 1.0)
    assert ratios[0] <= solution.theta_inr <= ratios[-1]
    assert theta_search.spend_at(entries, ratios[-1]) == 0.0, (
        "above the largest ratio no paid ask has positive reduced value, which is what "
        "guarantees the hill-climb terminates"
    )


def test_a_coarse_channel_ladder_cannot_hit_the_budget_and_says_so():
    """The honest limit of the +-2% gate: it is a property of the *instance*.

    With only the dear channels configured, a INR 5 budget buys two IVR calls at INR 2
    and the last rupee cannot buy anything at all. The resulting 20% shortfall is not a
    convergence failure and no allocator could do better -- it is indivisibility. The
    gate holds on the shipped seven-rung ladder because `email` at INR 0.05 makes the
    steps fine relative to any budget worth running.
    """
    params = load_params()
    coarse = params.model_copy(
        update={"channels": [c for c in params.channels if c.cost_inr >= 2.0]}
    )
    solution = ThetaSearch().search(candidate_set.build(Pricer(coarse), coarse, book(), 5.0), 5.0)
    assert solution.binding
    assert solution.converged, "the bisection converges; it is the ladder that is coarse"
    assert not solution.within(), "this instance is meant to be the one that cannot fit"
    assert solution.spend_inr == pytest.approx(4.0)


def test_the_repair_step_spends_slack_the_bisection_left_behind():
    """The bisection stops at a step and the step is rarely flush with the budget.

    Checked on the paid-only ladder, where slack is unambiguous -- with `in_app`
    configured a mandate priced out of every paid channel is still contacted, which
    muddies what "left behind" means.
    """
    params = paid_only(load_params())
    entries = candidate_set.build(Pricer(params), params, book(), 20.0)
    raw = ThetaSearch(repair=False).search(entries, 20.0)
    repaired = ThetaSearch().search(entries, 20.0)

    assert repaired.spend_inr >= raw.spend_inr
    assert repaired.value_inr >= raw.value_inr
    assert repaired.upgrades > 0
    assert repaired.spend_inr <= 20.0 + 1e-9


@pytest.mark.parametrize("budget", BINDING_BUDGETS)
def test_whatever_the_budget_leaves_unspent_could_not_have_been_spent(budget):
    """The claim that turns a shortfall from an excuse into a result.

    T3.4's gate wants the spend within 2% of the budget, and on the real book the two
    tightest budgets miss it. Both readings of a miss look identical from the outside --
    the allocator gave up early, or it ran out of things worth buying -- and only this
    tells them apart: after the repair, *nothing* that would both pay and fit is left.

    So an unspent rupee here is not a rupee the search failed to use. It is a rupee that
    no allocator, CBC included, could have turned into value, and that is the strongest
    form the gate can honestly take on an instance with indivisible asks.
    """
    params = load_params()
    entries = pairs(params, book(), budget)
    solution = ThetaSearch().search(entries, budget)
    stranded = theta_search.affordable_upgrades(entries, dict(solution.chosen), budget)
    assert stranded == [], (
        f"budget INR {budget}: {len(stranded)} profitable asks still fit in the "
        f"INR {budget - solution.spend_inr:.4f} left over -- the repair gave up early"
    )


@pytest.mark.parametrize("budget", BINDING_BUDGETS)
def test_the_published_theta_reproduces_its_own_selection(budget):
    """A price that does not buy the basket it was reported with is not a price.

    This failed once and silently. `theta_inr` was rounded to six decimals while `chosen`
    was computed at the full-precision bracket, so re-deriving the allocation from the
    published number -- which is exactly what T3.5's online rule does -- gave a *different*
    and more expensive answer, because rounding down lets candidates back in that the
    bisection had priced out. Nothing raised; the two just quietly disagreed.

    Rounding up fixes it, and the direction matters: a higher price only ever removes
    candidates, so the selection shrinks and the budget stays respected.
    """
    params = load_params()
    entries = pairs(params, book(), budget)
    solution = ThetaSearch(repair=False).search(entries, budget)

    rederived = theta_search.select(entries, solution.theta_inr)
    assert {m: c.channel for m, c in rederived.items()} == {
        m: c.channel for m, c in solution.chosen.items()
    }
    assert sum(c.cost_inr for c in rederived.values()) <= budget + 1e-9


def test_the_repair_never_moves_the_price():
    """Repair changes the allocation, not the price. If it moved theta, the number this
    project publishes would depend on a greedy tidy-up rather than on the dual, and the
    agreement with CBC above would be untestable."""
    params = load_params()
    entries = pairs(params, book(), 2.0)
    assert (
        ThetaSearch().search(entries, 2.0).theta_inr
        == ThetaSearch(repair=False).search(entries, 2.0).theta_inr
    )


# --------------------------------------------------------------------------------
# The free channel, reproducibility, and the empty book.
# --------------------------------------------------------------------------------


def test_a_free_channel_means_the_budget_rations_which_not_whether():
    """`docs/problem.md` §5.3, arrived at from the other direction than T3.3 arrived at it.

    At a budget of ten paise the search still contacts most of the book, because anything
    priced out of a paid channel falls back to `in_app` rather than dropping out. So the
    ask count says almost nothing about the budget and the *paid* ask count says
    everything -- which is why `ThetaSolution` reports both.
    """
    params = load_params()
    solution = ThetaSearch().search(pairs(params, book(), 0.10), 0.10)
    assert solution.asks > BOOK_SIZE // 2
    assert solution.paid_asks <= 3
    assert solution.spend_inr <= 0.10 + 1e-9


def test_the_same_book_prices_the_same_way_twice():
    """ADR 0003. The tie-breaks in `select` and in the repair are total for this reason:
    a selection that reorders itself between runs breaks the byte-identical gate whether
    or not it happens to cost the same."""
    params = load_params()
    entries = book()
    first = ThetaSearch().search(pairs(params, entries, 2.0), 2.0)
    second = ThetaSearch().search(pairs(params, entries, 2.0), 2.0)
    assert first.model_dump() == second.model_dump()


def test_an_empty_book_is_not_an_error():
    solution = ThetaSearch().search([], 5.0)
    assert solution.chosen == {}
    assert solution.theta_inr == 0.0
    assert not solution.binding
    assert solution.converged


def test_from_book_prices_the_live_view_without_hand_built_candidates():
    """The entry point the scripts and T3.5 use. It must range over the same pairs
    `MCKPPolicy` solves, or "the search reproduces the dual" is a claim about two
    candidate sets rather than about two algorithms."""
    params = load_params()
    entries = book()
    direct = theta_search.from_book(params, entries, 1.0)
    assembled = ThetaSearch().search(pairs(params, entries, 1.0), 1.0)
    assert direct.model_dump() == assembled.model_dump()


def test_the_summary_line_is_printable_in_both_states():
    """Numbers reach docs by being printed, never by being retyped (`CLAUDE.md` §4), so
    the printing path is covered like any other."""
    params = load_params()
    entries = book()
    assert "does not bind" in theta_search.from_book(params, entries, 10_000.0).summary()
    binding = theta_search.from_book(params, entries, 0.30).summary()
    assert "theta = INR" in binding and "of budget" in binding
