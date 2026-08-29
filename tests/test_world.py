"""Harness and ladder tests (T2.1-T2.6).

T2.2 asks for the six metrics to be unit-tested on a hand-built three-mandate fixture,
and that is what most of this file is. The point of three mandates over two weeks is that
every number below can be computed on paper: if `mandates_retained` is 2.06, it is 2.06
because 0.25 + 0.81 + 1.0 is, not because the harness said so.

The rest of the file tests the two things the harness is allowed to be strict about --
budgets and the totality of the decision set -- and the one behavioural difference the
ladder exists to isolate: P1 asks the same people every week, P2 rotates, and the backfire
ladder is what turns that into a number.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mandateguard.allocator.base import NoAskPolicy, Policy
from mandateguard.allocator.baselines import ChronologicalCap, GreedyEV, RoundRobin, bulk_channel
from mandateguard.eval import world
from mandateguard.models import AllocationResponse, Decision, DecisionKind, MandateWeek
from mandateguard.policy.loader import Params

BOOK = [
    # Hazards chosen so the survival arithmetic is exact in binary-friendly decimals.
    world.BookMandate(
        mandate_id="a-doomed",
        hazards=[0.5, 0.5],
        ltv_remaining_inr=1000.0,
        reachability_value_inr=0.0,
        recovery_after_lapse=0.4,
        recovery_after_revocation=0.1,
    ),
    world.BookMandate(
        mandate_id="b-wobbly",
        hazards=[0.1, 0.1],
        ltv_remaining_inr=1000.0,
        reachability_value_inr=0.0,
        recovery_after_lapse=0.4,
        recovery_after_revocation=0.1,
    ),
    world.BookMandate(
        mandate_id="c-safe",
        hazards=[0.0, 0.0],
        ltv_remaining_inr=1000.0,
        reachability_value_inr=0.0,
        recovery_after_lapse=0.4,
        recovery_after_revocation=0.1,
    ),
]


def make_params(**overrides) -> Params:
    """A two-week world with one ₹1 channel, so a budget of ₹1 buys exactly one ask."""
    payload = {
        "channels": [
            {"name": "free", "cost_inr": 0.0, "efficacy_prior": 0.02, "intrusive": False},
            {"name": "post", "cost_inr": 1.0, "efficacy_prior": 0.5, "intrusive": True},
        ],
        "value": {
            "mu_good_outcome": 1.0,
            "nu_complaint": 1.0,
            "alpha_reachability": 1.0,
            "gamma_fatigue": 1.0,
            "fatigue_half_life_days": 15,
            "rho_template_reuse": 1.0,
        },
        "recovery": {
            "after_lapse": 0.4,
            "after_revocation": 0.1,
            "swept_ceiling_after_revocation": 0.29,
        },
        "intervention": {
            "uplift_scale": 1.0,
            "backfire_first_ask": 0.1,
            "backfire_twelfth_ask": 0.1,
            "natural_revocation_share": 0.634,
        },
        "horizon": {"weeks": 2, "budget_inr_per_week": 1.0},
        "india": {
            "snapshot_date": "2017-02-28",
            "ntd_to_inr": 1.0,
            "rail_mix": {"upi_autopay": 0.55, "card": 0.25, "enach": 0.15, "ppi": 0.05},
            "upi_autopay_afa_threshold_inr": 15000.0,
            "mandate_validity_days": 730,
            "reachability_fraction_of_ltv": 0.15,
            "plausible_age_years": [13, 90],
            "default_debit_frequency_days": 30,
        },
        "seed": 1,
    }
    payload.update(overrides)
    return Params.model_validate(payload)


@pytest.fixture
def params() -> Params:
    return make_params()


# --------------------------------------------------------------------------------
# The six metrics, on paper.
# --------------------------------------------------------------------------------


def test_the_floor_arm_retains_exactly_the_survival_product(params):
    """P0 touches nothing, so survival is the product of `(1 - h)` week by week:
    0.5*0.5 = 0.25, 0.9*0.9 = 0.81, 1.0*1.0 = 1.0. Anything else means the harness is
    compounding wrongly, and every arm above it inherits the error."""
    result = world.run(BOOK, NoAskPolicy(), params)
    assert result.mandates_retained == pytest.approx(0.25 + 0.81 + 1.0)
    assert result.arr_retained_inr == pytest.approx(2060.0)
    assert result.asks_spent == 0
    assert result.revocations_caused == 0.0
    assert result.net_value_inr == 0.0
    assert result.theta_inr is None


def test_retention_is_a_fraction_and_that_is_not_a_rounding_error(params):
    """The harness carries survival *probabilities*, not sampled outcomes. A reader who
    expects whole mandates back should meet that fact in a test rather than in a
    results table."""
    result = world.run(BOOK, NoAskPolicy(), params)
    assert result.mandates == 3
    assert result.retention_rate == pytest.approx(2.06 / 3)


def test_deaths_split_into_lapses_and_revocations_by_the_measured_mix(params):
    """0.94 expected deaths, 63.4% of them revocations -- the mix measured in
    mapping.md 5.6, not a guess made here."""
    result = world.run(BOOK, NoAskPolicy(), params)
    deaths = 3 - result.mandates_retained
    assert deaths == pytest.approx(0.94)
    assert result.revocations_natural == pytest.approx(0.94 * 0.634)
    assert result.lapses == pytest.approx(0.94 * 0.366)
    assert result.revocations_natural + result.lapses == pytest.approx(deaths)


def test_an_ask_prices_what_it_prevented_and_what_it_caused(params):
    """One ask, on the doomed mandate, in week 0 only. Every term is checkable:

    b            = 0.1                       (backfire, flat in this fixture)
    h_eff        = 0.5 * (1 - 0.5) = 0.25    (uplift halves the hazard)
    prevented    = 1.0 * 0.9 * (0.5 - 0.25) = 0.225
    loss_lapse   = 1000 * (1 - 0.4)  = 600
    loss_revoke  = 1000 * (1 - 0.1)  = 900
    value        = 0.225*600 - 0.1*900 - 1.0 = 135 - 90 - 1 = 44
    """

    class AskTheDoomedOnce(Policy):
        arm = "T"

        def allocate(self, book, budget_inr, week):
            return AllocationResponse(
                decisions=[
                    Decision(
                        mandate_id=m.mandate_id,
                        week=week,
                        kind=(
                            DecisionKind.ASKED
                            if (week == 0 and m.mandate_id == "a-doomed")
                            else DecisionKind.NOT_ASKED
                        ),
                        channel=("post" if (week == 0 and m.mandate_id == "a-doomed") else None),
                        value_inr=0.0,
                        reason="fixture",
                    )
                    for m in book
                ],
                budget_spent_inr=1.0 if week == 0 else 0.0,
            )

    result = world.run(BOOK, AskTheDoomedOnce(), params)
    assert result.asks_spent == 1
    assert result.revocations_caused == pytest.approx(0.1)
    assert result.net_value_inr == pytest.approx(44.0)
    assert result.inr_per_ask == pytest.approx(44.0)
    assert result.channel_cost_inr == pytest.approx(1.0)


def test_an_ask_that_backfires_costs_more_than_the_lapse_it_prevented(params):
    """The invariant behind the whole project: `loss_on_revocation > loss_on_lapse`, so
    a backfire is never a wash. With q=0.4 and r=0.1 the gap is 300 rupees per mandate."""
    entry = MandateWeek(
        mandate_id="x",
        week=0,
        hazard=0.5,
        alive=1.0,
        ltv_remaining_inr=1000.0,
        reachability_value_inr=0.0,
        recovery_after_lapse=0.4,
        recovery_after_revocation=0.1,
        asks_so_far=0,
    )
    assert entry.loss_on_lapse() == pytest.approx(600.0)
    assert entry.loss_on_revocation() == pytest.approx(900.0)
    assert entry.loss_on_revocation() > entry.loss_on_lapse()


# --------------------------------------------------------------------------------
# What the harness refuses to let a policy do.
# --------------------------------------------------------------------------------


def test_an_overspending_arm_is_rejected_not_clipped(params):
    """The ladder compares arms at equal budget. Silently trimming an over-spender would
    report a budget-respecting result for a policy that does not respect budgets."""

    class Spendthrift(Policy):
        arm = "X"

        def allocate(self, book, budget_inr, week):
            return AllocationResponse(
                decisions=[
                    Decision(
                        mandate_id=m.mandate_id,
                        week=week,
                        kind=DecisionKind.ASKED,
                        channel="post",
                        value_inr=0.0,
                        reason="everything, always",
                    )
                    for m in book
                ],
                budget_spent_inr=3.0,
            )

    with pytest.raises(world.BudgetExceeded, match="not a better policy"):
        world.run(BOOK, Spendthrift(), params)


def test_a_partial_decision_set_is_rejected(params):
    """`Policy.allocate` is total by contract. A policy that returns only the asked
    mandates makes the refusal ledger impossible, and the refusal ledger is most of what
    this system produces."""

    class Silent(Policy):
        arm = "X"

        def allocate(self, book, budget_inr, week):
            return AllocationResponse(decisions=[], budget_spent_inr=0.0)

    with pytest.raises(ValueError, match="refusal ledger"):
        world.run(BOOK, Silent(), params)


def test_an_unknown_channel_is_rejected(params):
    class Exotic(Policy):
        arm = "X"

        def allocate(self, book, budget_inr, week):
            return AllocationResponse(
                decisions=[
                    Decision(
                        mandate_id=m.mandate_id,
                        week=week,
                        kind=DecisionKind.ASKED
                        if m.mandate_id == "a-doomed"
                        else DecisionKind.NOT_ASKED,
                        channel="carrier-pigeon" if m.mandate_id == "a-doomed" else None,
                        value_inr=0.0,
                        reason="fixture",
                    )
                    for m in book
                ],
                budget_spent_inr=0.0,
            )

    with pytest.raises(ValueError, match="unknown channel"):
        world.run(BOOK, Exotic(), params)


# --------------------------------------------------------------------------------
# The ladder. P1 versus P2 is the one variable this phase isolates.
# --------------------------------------------------------------------------------


def test_the_bulk_channel_is_the_cheapest_one_that_actually_costs_something(params):
    """A free non-intrusive channel would give an arm an unbounded budget and the ladder
    would be comparing nothing."""
    channel = bulk_channel(params.channels)
    assert channel.name == "post"


def test_a_book_with_no_intrusive_channel_refuses_rather_than_asking_for_free():
    free_only = [{"name": "free", "cost_inr": 0.0, "efficacy_prior": 0.02, "intrusive": False}]
    params = make_params(channels=free_only)
    with pytest.raises(ValueError, match="no arm can spend its budget"):
        ChronologicalCap(params)


def test_the_queue_arm_asks_the_same_mandate_twice_and_the_rotation_does_not(params):
    """The whole reason P2 is in the ladder, in one assertion. One ask per week for two
    weeks: P1 spends both on the first mandate in the queue, P2 spreads them.

    With a flat backfire the *count* of caused revocations barely differs -- what differs
    is who they land on, and with a climbing ladder it is what makes P1 worse.
    """
    queue = ChronologicalCap(params)
    rotation = RoundRobin(params)

    first = queue.allocate(_view(0, asks={}), 1.0, 0)
    assert _asked(first) == ["a-doomed"]
    second = queue.allocate(_view(1, asks={"a-doomed": 1}), 1.0, 1)
    assert _asked(second) == ["a-doomed"]

    first = rotation.allocate(_view(0, asks={}), 1.0, 0)
    assert _asked(first) == ["a-doomed"]
    second = rotation.allocate(_view(1, asks={"a-doomed": 1}), 1.0, 1)
    assert _asked(second) == ["b-wobbly"]


def test_a_climbing_backfire_ladder_punishes_the_queue_more_than_the_rotation():
    """With backfire that grows with contact count, hammering one customer twice costs
    more than contacting two customers once -- which is the mechanism, not just the
    intuition."""
    params = make_params(
        intervention={
            "uplift_scale": 1.0,
            "backfire_first_ask": 0.01,
            "backfire_twelfth_ask": 0.5,
            "natural_revocation_share": 0.634,
        }
    )
    queue = world.run(BOOK, ChronologicalCap(params), params)
    rotation = world.run(BOOK, RoundRobin(params), params)
    assert queue.asks_spent == rotation.asks_spent == 2
    assert queue.revocations_caused > rotation.revocations_caused


def test_greedy_declines_when_an_ask_is_worth_less_than_it_costs(params):
    """P3 buys only positive-value asks. An arm that spent its budget merely because it
    had one would be worse than the sort it is meant to represent -- and the refusal
    reason has to be able to say "not worth it", not only "someone was ahead of you".
    """
    hopeless = make_params(
        channels=[
            {"name": "free", "cost_inr": 0.0, "efficacy_prior": 0.02, "intrusive": False},
            {"name": "post", "cost_inr": 1.0, "efficacy_prior": 0.001, "intrusive": True},
        ],
        intervention={
            "uplift_scale": 1.0,
            "backfire_first_ask": 0.5,
            "backfire_twelfth_ask": 0.5,
            "natural_revocation_share": 0.634,
        },
    )
    result = world.run(BOOK, GreedyEV(hopeless), hopeless)
    assert result.asks_spent == 0
    assert result.mandates_retained == pytest.approx(
        world.run(BOOK, NoAskPolicy(), hopeless).mandates_retained
    )
    reasons = {d.reason for d in GreedyEV(hopeless).allocate(_view(0, asks={}), 1.0, 0).decisions}
    assert reasons == {"expected value below the week's cut-off"}


def test_greedy_spends_on_the_riskiest_mandate_when_the_ask_does_pay(params):
    """The mirror of the previous test. `post` has efficacy 0.5 here, so the doomed
    mandate is worth 44 rupees and the safe one is worth nothing."""
    greedy = GreedyEV(params)
    response = greedy.allocate(_view(0, asks={}), 1.0, 0)
    assert _asked(response) == ["a-doomed"]


# --------------------------------------------------------------------------------
# Reproducibility.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("arm", ["P0", "P1", "P2", "P3"])
def test_every_arm_reproduces_itself(arm, params):
    """No RNG anywhere in the harness, by design (ADR 0003). Two runs of one arm over one
    book must agree exactly, not approximately."""
    policies = {
        "P0": NoAskPolicy(),
        "P1": ChronologicalCap(params),
        "P2": RoundRobin(params),
        "P3": GreedyEV(params),
    }
    first = world.run(BOOK, policies[arm], params)
    second = world.run(BOOK, policies[arm], params)
    assert first.model_dump() == second.model_dump()


def test_the_metrics_table_renders_every_arm(params):
    results = [
        world.run(BOOK, NoAskPolicy(), params),
        world.run(BOOK, ChronologicalCap(params), params),
    ]
    text = world.format_metrics(results)
    assert "| P0 |" in text
    assert "| P1 |" in text
    assert "3 live mandates" in text


def test_the_mandate_week_view_rejects_an_impossible_survival_weight():
    with pytest.raises(ValidationError):
        MandateWeek(
            mandate_id="x",
            week=0,
            hazard=0.1,
            alive=1.5,
            ltv_remaining_inr=1.0,
            reachability_value_inr=0.0,
            recovery_after_lapse=0.4,
            recovery_after_revocation=0.1,
            asks_so_far=0,
        )


# --------------------------------------------------------------------------------


def _view(week: int, asks: dict[str, int]) -> list[MandateWeek]:
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
            asks_so_far=asks.get(m.mandate_id, 0),
        )
        for m in BOOK
    ]


def _asked(response: AllocationResponse) -> list[str]:
    return [d.mandate_id for d in response.decisions if d.kind is DecisionKind.ASKED]
