"""P5 tests (T3.8) -- the index, and the one property that made it a result.

The headline property here is not "P5 beats P4". It is that **the answer no longer depends
on the solver's stopping rule**. The first version of this arm returned 31 asks at 8
bisection steps, 108 at 16 and 109 at 24: every one of those was a plausible-looking
number, and the arm would have shipped whichever the constant happened to be set to. A
result that moves with a tolerance is not a result, and `test_the_allocation_does_not_move
_with_the_bisection_depth` is the assertion that it has stopped.
"""

from __future__ import annotations

import numpy as np
import pytest

from mandateguard.allocator import whittle
from mandateguard.allocator.mckp import MCKPPolicy
from mandateguard.allocator.whittle import WhittleIndex, WhittleSolver, horizon_from
from mandateguard.eval import world
from mandateguard.eval.world import BookMandate
from mandateguard.models import DecisionKind, MandateWeek
from mandateguard.policy.loader import load_params
from tests.test_mckp import BOOK_SIZE, book


def timed_book(size: int = 60, weeks: int = 12) -> list[BookMandate]:
    """Mandates whose risk *peaks in different weeks* -- the structure P5 exists for.

    A book with flat hazards cannot distinguish a multi-period arm from a myopic one, so
    it could not fail this module's central claim. Each mandate here spikes in one week,
    and which week it is rotates through the horizon.
    """
    return [
        BookMandate(
            mandate_id=f"m{index:03d}",
            hazards=[0.05 if week == index % weeks else 0.002 for week in range(weeks)],
            ltv_remaining_inr=200.0 + 300.0 * ((index * 7) % size) / size,
            reachability_value_inr=30.0,
            recovery_after_lapse=0.41,
            recovery_after_revocation=0.08,
        )
        for index in range(size)
    ]


def asked(response):
    return [d for d in response.decisions if d.kind is DecisionKind.ASKED]


# --------------------------------------------------------------------------------
# The property that turns this from a number into a result.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("steps", (6, 8, 16))
def test_the_allocation_does_not_move_with_the_bisection_depth(monkeypatch, steps):
    """The failure this arm actually had, pinned.

    A loose bracket -- one global ceiling, the largest net value times the horizon over the
    cheapest channel -- came out near 1,300 while a typical index is under a rupee, so most
    halvings were spent travelling rather than resolving, and the *allocation* changed with
    the step count. It was safe as a bound and wrong as a bracket.

    A bracket wide enough to be obviously correct is not therefore harmless. That is
    `docs/seekha.md` #54 in a nastier form: there a loose bracket cost tidiness, here it
    silently changed what the arm did.
    """
    params = load_params()
    entries = book(40)
    reference = WhittleIndex(params).allocate(entries, 5.0, week=0)

    monkeypatch.setattr(whittle, "BISECTION_STEPS", steps)
    varied = WhittleIndex(params).allocate(entries, 5.0, week=0)

    assert {d.mandate_id for d in asked(varied)} == {d.mandate_id for d in asked(reference)}
    assert varied.budget_spent_inr == pytest.approx(reference.budget_spent_inr)


def test_a_mandate_never_worth_acting_on_scores_exactly_zero():
    """The `lambda = 0` pre-check. If acting does not pay when budget is free, it cannot
    pay at any price, and no search is needed -- which is both faster and the only way the
    bracket seed is meaningful."""
    params = load_params()
    solver = WhittleSolver(params)
    safe = [
        MandateWeek(
            mandate_id="safe",
            week=0,
            hazard=0.0,  # nothing to prevent, so an ask is pure cost
            alive=1.0,
            ltv_remaining_inr=100.0,
            reachability_value_inr=10.0,
            recovery_after_lapse=0.41,
            recovery_after_revocation=0.08,
            asks_so_far=0,
        )
    ]
    horizon = horizon_from(safe, params, solver.weeks)
    scores = solver.index(horizon, 0, np.array([0]), np.array([solver.never]))
    assert scores[0] == 0.0


# --------------------------------------------------------------------------------
# What the multi-period formulation is for.
# --------------------------------------------------------------------------------


URGENT_HAZARD = 0.15
"""A weekly hazard at which an ask actually pays on this fixture.

Not a round number picked for looks. On the shipped channel table an email ask breaks even
near a hazard of 0.10 -- the gain is `h * efficacy * L_lapse` against a backfire cost that
does not involve `h` at all -- so a fixture built at a plausible-looking 0.002 produces an
index of exactly zero for every mandate, correctly, and tests nothing. That is what the
first draft of the test below did.
"""


def _index_of(params, solver, paths: list[list[float]]) -> np.ndarray:
    """Indices for mandates that differ only in their hazard paths."""
    shared = dict(
        week=0,
        alive=1.0,
        ltv_remaining_inr=400.0,
        reachability_value_inr=60.0,
        recovery_after_lapse=0.41,
        recovery_after_revocation=0.08,
        asks_so_far=0,
    )
    entries = [
        MandateWeek(mandate_id=f"m{i}", hazard=path[0], hazard_path=path, **shared)
        for i, path in enumerate(paths)
    ]
    horizon = horizon_from(entries, params, solver.weeks)
    return solver.index(
        horizon,
        0,
        np.zeros(len(paths), dtype=int),
        np.full(len(paths), solver.never),
    )


def test_the_index_rises_with_this_week_s_hazard():
    """The index is a price of urgency, so it has to move with the risk it is pricing.

    This is the assertion that caught the arm's worst bug. `(net, p_die)` were memoised on
    `(id(horizon), week)` -- and `id()` is unique only among *live* objects, so once a
    horizon was collected CPython handed its address to the next one and the solver served
    the previous book's arrays. The index came back **identical to six decimals** at
    hazards from 0.10 to 0.30. Nothing raised, and it was nearly written up as a finding
    about the value function rather than recognised as a stale cache.
    """
    params = load_params()
    solver = WhittleSolver(params)
    tail = [0.001] * (solver.weeks - 1)
    scores = _index_of(params, solver, [[h] + tail for h in (0.10, 0.12, 0.15, 0.20, 0.30)])

    assert all(a < b for a, b in zip(scores, scores[1:], strict=False)), scores
    assert scores[-1] > scores[0] * 3, f"the index barely moved with the hazard: {scores}"


def test_the_index_reads_the_future_as_well_as_the_present():
    """Two mandates identical today, differing only in what comes after.

    Their week-0 prices are equal to the last decimal, so a single-period optimiser cannot
    separate them at all. The index does -- and `docs/eval.md` §8 reports *how much*, which
    is the honest and slightly deflating part: the horizon moves the index by about 0.02%
    of its level, because the current week's price dominates it.

    The **direction** is deliberately not asserted. It is small, it is not robustly signed
    across the shapes tried, and pinning a sign we cannot explain would be inventing a
    result. What is asserted is that the future registers at all -- because if it did not,
    this arm would be P4 with extra arithmetic and should be cut.
    """
    params = load_params()
    solver = WhittleSolver(params)
    weeks = solver.weeks
    urgent = 0.15
    scores = _index_of(
        params,
        solver,
        [
            [urgent] + [0.001] * (weeks - 1),  # no second chance
            [urgent] * weeks,  # risky all the way
            [urgent] + [0.001] * 5 + [urgent] + [0.001] * (weeks - 7),  # a later spike
        ],
    )
    assert scores[0] > 0, "the fixture must make acting worthwhile, or nothing is compared"
    assert len(set(scores.round(9))) == len(scores), (
        f"identical week-0 prices produced identical indices: {scores}"
    )


def test_without_a_hazard_path_the_arm_degrades_rather_than_failing():
    """`hazard_path` is `None` whenever the caller holds no forecast -- the API layer
    (T5.3) is handed one mandate in one week. The arm has to answer, projecting the
    current hazard flat, and say so rather than raise."""
    params = load_params()
    entries = book(20)
    assert all(e.hazard_path is None for e in entries)
    response = WhittleIndex(params).allocate(entries, 5.0, week=0)
    assert len(response.decisions) == 20


def test_the_harness_now_offers_the_hazard_path_to_every_arm():
    """Equal information is what makes the ladder a comparison. The path is on the view
    all six arms receive, not handed to P5 alone -- otherwise "the multi-period arm wins"
    would be a claim about information rather than about formulation."""
    seen: list[MandateWeek] = []

    class Spy(world.Policy):  # type: ignore[name-defined]
        arm = "spy"

        def allocate(self, book, budget_inr, week):
            seen.extend(book)
            from mandateguard.models import AllocationResponse, Decision

            return AllocationResponse(
                decisions=[
                    Decision(
                        mandate_id=m.mandate_id,
                        week=week,
                        kind=DecisionKind.NOT_ASKED,
                        value_inr=0.0,
                        reason="spy",
                    )
                    for m in book
                ],
                theta_inr=None,
                budget_spent_inr=0.0,
            )

    params = load_params()
    entries = timed_book(size=6)
    world.run(entries, Spy(), params, 1.0)
    assert seen and all(m.hazard_path is not None for m in seen)
    first = seen[0]
    assert first.hazard_path is not None
    assert first.hazard_path[0] == pytest.approx(first.hazard)


# --------------------------------------------------------------------------------
# Feasibility, refusals and reproducibility -- the contract every arm shares.
# --------------------------------------------------------------------------------


def test_the_allocation_is_feasible_and_within_budget():
    params = load_params()
    response = WhittleIndex(params).allocate(book(), 5.0, week=0)
    costs = {c.name: c.cost_inr for c in params.channels}

    assert len(response.decisions) == BOOK_SIZE
    contacted = [d.mandate_id for d in asked(response)]
    assert len(contacted) == len(set(contacted)), "a mandate was contacted twice in one week"
    spend = sum(costs[d.channel] for d in asked(response))
    assert spend <= 5.0 + 1e-9
    assert response.budget_spent_inr == pytest.approx(spend)


def test_it_never_buys_an_ask_that_loses_money_this_week():
    """The index says *whether* a mandate is urgent; the pricer says whether the channel
    is worth using. An urgent mandate with no profitable channel is still not asked."""
    response = WhittleIndex(load_params()).allocate(book(), 500.0, week=0)
    assert all(d.value_inr > 0 for d in asked(response))


def test_a_refusal_names_the_index_that_caused_it():
    response = WhittleIndex(load_params()).allocate(book(), 0.10, week=0)
    refused = [d for d in response.decisions if d.kind is DecisionKind.NOT_ASKED]
    assert refused
    assert all("Whittle index" in d.reason for d in refused)
    assert all(d.channel is None for d in refused)


def test_the_same_week_allocates_the_same_way_twice():
    """ADR 0003. The ranking ties break on `mandate_id` for this reason."""
    params = load_params()
    entries = book()
    first = WhittleIndex(params).allocate(entries, 2.0, week=0)
    second = WhittleIndex(params).allocate(entries, 2.0, week=0)
    assert first.model_dump() == second.model_dump()


def test_an_empty_book_is_not_an_error():
    response = WhittleIndex(load_params()).allocate([], 5.0, week=0)
    assert response.decisions == []
    assert response.budget_spent_inr == 0.0


def test_p5_holds_its_own_against_p4_over_the_horizon():
    """Not a claim that P5 must win -- Whittle's relaxation is a heuristic and indexability
    is not verified here. The checkable claim is that a multi-period arm on a book with
    real timing structure does not come out *behind* the myopic one it extends.

    `docs/eval.md` §8 reports the actual margin; this only guards against a regression that
    would make the arm worth cutting.
    """
    params = load_params()
    entries = timed_book()
    budget = 3.0
    planned = world.run(entries, WhittleIndex(params), params, budget)
    myopic = world.run(entries, MCKPPolicy(params, with_theta=False), params, budget)
    assert planned.profit_inr >= myopic.profit_inr * 0.999
