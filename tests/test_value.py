"""Value-layer tests (T3.2).

T3.2 names two assertions specifically, and they are the two the whole pricing argument
rests on:

* a test that fails if `q <= r`
* a test that the loss on revocation exceeds the loss on lapse for the same mandate

Everything else here is the four terms checked one at a time on numbers small enough to
verify by hand, plus the compositions that would silently double-count if anyone reordered
them.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from mandateguard.models import Channel, MandateWeek
from mandateguard.policy.loader import Params, load_params
from mandateguard.value import channel_priors, fatigue, ltv, price, prices, reachability
from tests.test_world import make_params

CHEAP = Channel(name="cheap", cost_inr=1.0, efficacy_prior=0.1, intrusive=True)
DEAR = Channel(name="dear", cost_inr=10.0, efficacy_prior=0.5, intrusive=True)


def entry(**overrides) -> MandateWeek:
    payload = {
        "mandate_id": "m",
        "week": 0,
        "hazard": 0.5,
        "alive": 1.0,
        "ltv_remaining_inr": 1000.0,
        "reachability_value_inr": 100.0,
        "recovery_after_lapse": 0.4,
        "recovery_after_revocation": 0.1,
        "asks_so_far": 0,
        "weeks_since_last_ask": None,
    }
    payload.update(overrides)
    return MandateWeek(**payload)


# --------------------------------------------------------------------------------
# The two endings (Pinterest, KDD 2018, extended). The assertions T3.2 asks for.
# --------------------------------------------------------------------------------


def test_revocation_costs_more_than_lapse_for_the_same_mandate():
    """The assertion T3.2 names. `L(1-q) = 600` against `L(1-r) + alpha*R = 900 + 100`.

    If these were ever equal the model could not express that a failed ask converts a
    soft ending into a hard one -- which is the entire reason contacting a
    probably-doomed mandate is not free.
    """
    lapse = ltv.loss_on_lapse_inr(1000.0, 0.4)
    revocation = ltv.loss_on_revocation_inr(1000.0, 0.1, 100.0, alpha=1.0)
    assert lapse == pytest.approx(600.0)
    assert revocation == pytest.approx(1000.0)
    assert revocation > lapse


def test_a_mandate_with_q_below_r_cannot_be_constructed():
    """The other assertion T3.2 names. Enforced in three places -- here, in
    `models.Mandate`, and in the config loader -- because a YAML file is edited far more
    often than a `Mandate` is constructed."""
    with pytest.raises(ValidationError):
        entry(recovery_after_lapse=0.1, recovery_after_revocation=0.4)


def test_config_with_q_below_r_is_rejected_at_load():
    with pytest.raises(ValidationError, match="must exceed"):
        Params.model_validate(
            {
                **make_params().model_dump(mode="json"),
                "recovery": {
                    "after_lapse": 0.1,
                    "after_revocation": 0.2,
                    "swept_ceiling_after_revocation": 0.29,
                },
            }
        )


def test_reachability_is_a_separate_term_not_folded_into_ltv():
    """Twitter/X 2022's point. With `R = 0` the two losses differ only by `q` and `r`;
    the gap `alpha * R` is a different asset -- the rail to the customer -- and it has to
    be visible as its own number or nobody can sweep it."""
    without = ltv.loss_on_revocation_inr(1000.0, 0.1, 0.0, alpha=1.0)
    with_rail = ltv.loss_on_revocation_inr(1000.0, 0.1, 100.0, alpha=1.0)
    assert with_rail - without == pytest.approx(reachability.reachability_loss_inr(100.0, 1.0))


def test_a_rail_that_survives_revocation_can_say_so():
    """`P(still reachable)` is 1 on UPI AutoPay because a revocation takes the rail with
    it. The argument exists so a rail where that is untrue has somewhere to say so."""
    assert reachability.reachability_loss_inr(100.0, 1.0, still_reachable=0.5) == pytest.approx(
        50.0
    )


# --------------------------------------------------------------------------------
# Fatigue (Duolingo, KDD 2020).
# --------------------------------------------------------------------------------


def test_fatigue_halves_every_half_life():
    """`gamma * 0.5 ** (days / half_life)`. At 15 days it is half of gamma; at 30, a
    quarter. Checked against the arithmetic, not against whatever the code returned."""
    assert fatigue.fatigue_inr(0, gamma=25.0, half_life_days=15) == pytest.approx(25.0)
    assert fatigue.fatigue_inr(2, gamma=25.0, half_life_days=14) == pytest.approx(12.5)
    assert fatigue.fatigue_inr(4, gamma=25.0, half_life_days=14) == pytest.approx(6.25)


def test_a_customer_never_contacted_costs_no_fatigue():
    """The boundary case most of the book is in. An exponential only reaches zero in the
    limit, so "never" has to be handled rather than approximated."""
    assert fatigue.fatigue_inr(None, gamma=25.0, half_life_days=15) == 0.0


def test_reusing_a_template_costs_a_flat_extra_charge():
    """The one place the LLM layer touches the optimiser's arithmetic. Without it, T4.3's
    notice composer is a nicer sentence with no consequence for the allocation."""
    fresh = fatigue.fatigue_inr(2, 25.0, 14, template_reused=False, rho_template_reuse=5.0)
    reused = fatigue.fatigue_inr(2, 25.0, 14, template_reused=True, rho_template_reuse=5.0)
    assert reused - fresh == pytest.approx(5.0)


# --------------------------------------------------------------------------------
# Channel priors (Chrome, USENIX Security 2021).
# --------------------------------------------------------------------------------


def test_softer_channels_carry_less_backfire_and_the_dearest_is_the_reference():
    ladder = channel_priors.build_ladder([CHEAP, DEAR], backfire_avoided_per_step=0.25)
    assert ladder.backfire_multiplier(DEAR) == pytest.approx(1.0)
    assert ladder.backfire_multiplier(CHEAP) == pytest.approx(0.75)


def test_the_ladder_compounds_one_step_at_a_time():
    """Seven rungs from in-app to agent call, each one Chrome-sized. The shipped ladder
    puts an email's backfire at about a quarter of an agent call's."""
    ladder = channel_priors.build_ladder(load_params().channels, 0.24)
    assert ladder.multipliers["agent"] == pytest.approx(1.0)
    assert ladder.multipliers["letter"] == pytest.approx(0.76)
    assert ladder.multipliers["email"] == pytest.approx(0.76**5)
    assert ladder.multipliers["in_app"] < ladder.multipliers["email"]


def test_an_unconfigured_channel_gets_the_most_intrusive_assumption():
    """Silently discounting a channel nobody configured is the one direction of error
    this project cannot afford."""
    ladder = channel_priors.build_ladder([CHEAP], backfire_avoided_per_step=0.24)
    assert ladder.backfire_multiplier(DEAR) == pytest.approx(1.0)


def test_the_uplift_side_of_chromes_result_is_not_applied_twice():
    """Chrome measured both a 2-5% grant loss and a 17-31% refusal avoidance. The grant
    loss is already in `efficacy_prior` (0.02 in-app to 0.28 agent), so only the avoided
    harm is applied here. Applying both would count the same effect twice."""
    pricer = price.Pricer(make_params())
    channel = next(c for c in make_params().channels if c.intrusive)
    # The effective hazard uses `efficacy_prior` unmodified by the ladder.
    assert pricer.effective_hazard(entry(), channel) == pytest.approx(
        0.5 * (1 - channel.efficacy_prior)
    )


# --------------------------------------------------------------------------------
# Prices (LinkedIn, KDD 2016).
# --------------------------------------------------------------------------------


def test_the_two_prices_stay_separate():
    """LinkedIn's point. Netting them into one "value per send" throws away the ratio,
    and the ratio is the only thing that tells an optimiser when to stop."""
    two = prices.Prices(mu_good_outcome=2.0, nu_complaint=5.0)
    assert two.good_outcome_inr(100.0) == pytest.approx(200.0)
    assert two.complaint_inr(100.0) == pytest.approx(500.0)
    assert two.complaint_is_priced_at_least_as_dearly


# --------------------------------------------------------------------------------
# The composition.
# --------------------------------------------------------------------------------


def test_the_four_terms_net_out_to_the_headline():
    params = make_params()
    pricer = price.Pricer(params)
    channel = next(c for c in params.channels if c.intrusive)
    quote = pricer.price(entry(), channel)
    assert quote.net_inr == pytest.approx(
        quote.gain_inr - quote.backfire_inr - quote.fatigue_inr - quote.channel_cost_inr
    )


def test_an_ask_on_a_mandate_that_is_probably_gone_is_worth_almost_nothing():
    """Both sides scale by `alive`: an ask on a mandate that already died neither saves
    nor annoys anybody, in expectation."""
    params = make_params()
    pricer = price.Pricer(params)
    channel = next(c for c in params.channels if c.intrusive)
    live = pricer.price(entry(alive=1.0), channel)
    ghost = pricer.price(entry(alive=0.01), channel)
    assert ghost.gain_inr == pytest.approx(live.gain_inr * 0.01)
    assert ghost.backfire_inr == pytest.approx(live.backfire_inr * 0.01)


def test_a_swept_uplift_cannot_drive_the_hazard_below_zero():
    """The sweep reaches `uplift_scale = 16`, well past `1 / efficacy_prior` for every
    channel. Without the floor the simulation would start *creating* mandates."""
    params = make_params()
    extreme = params.model_copy(
        update={"intervention": params.intervention.model_copy(update={"uplift_scale": 16.0})}
    )
    pricer = price.Pricer(extreme)
    channel = next(c for c in extreme.channels if c.intrusive)
    assert pricer.effective_hazard(entry(hazard=0.5), channel) == 0.0


def test_the_reason_string_names_every_term_in_rupees():
    """`Decision.reason` has to be an explanation, not a number. "The backfire cost
    exceeded the value of the lapses it would prevent" is a reason; "-2.67" is not."""
    params = make_params()
    pricer = price.Pricer(params)
    channel = next(c for c in params.channels if c.intrusive)
    worth_it = pricer.price(entry(hazard=0.9, ltv_remaining_inr=100_000.0), channel)
    assert worth_it.worth_asking
    assert "is worth INR" in worth_it.reason()

    hopeless = pricer.price(entry(hazard=0.0), channel)
    assert not hopeless.worth_asking
    assert hopeless.reason().startswith("not asked:")


def test_the_best_channel_is_the_most_valuable_affordable_one():
    """Against the *shipped* seven-channel ladder, not the two-channel test fixture:
    what is being checked is that a bigger budget buys a more effective channel, and a
    fixture with two channels cannot show that."""
    params = load_params()
    pricer = price.Pricer(params)
    rich = entry(hazard=0.9, ltv_remaining_inr=1_000_000.0)
    assert pricer.best_channel(rich, budget_inr=0.10).channel == "email"
    assert pricer.best_channel(rich, budget_inr=100.0).channel == "agent"


def test_no_channel_is_chosen_when_none_is_worth_it():
    params = load_params()
    pricer = price.Pricer(params)
    assert pricer.best_channel(entry(hazard=0.0), budget_inr=100.0) is None
