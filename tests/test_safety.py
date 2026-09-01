"""T5.3 -- the safety layer.

The task names one test explicitly: **a test proves the spend cap cannot be crossed**, and
every ladder step has its own. "Proves" is doing work in that sentence. A test that spends
up to the cap and stops shows the cap works on one path; the cap has to hold on every path,
including the ones nobody wrote a test for.

So the cap is checked twice here: once by example, and once by hammering the guard with a
few thousand randomised requests and asserting the invariant `spent <= cap` never breaks
regardless of the order, size, or refusal pattern of what was asked for.
"""

from __future__ import annotations

import random
from datetime import date

import pytest

from mandateguard.policy.loader import SafetyParams
from mandateguard.safety.guard import (
    Action,
    ActionKind,
    Degradation,
    Guard,
    Halted,
)

POLICY_HASH = "ce28096eeba5ad9d"


def safety(**overrides) -> SafetyParams:
    base = {
        "mode": "shadow",
        "kill_switch_file": "data/KILL",
        "max_sends_per_window": 500,
        "window_seconds": 3600,
        "max_spend_inr_per_run": 6000.0,
        "max_model_age_days": 30,
    }
    return SafetyParams.model_validate(base | overrides)


class FakeClock:
    """A clock the rate-limit tests can move. Real time in a limiter test is a flake."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def contact(cost: float = 1.0, mandate_id: str = "mg_1") -> Action:
    return Action(kind=ActionKind.CONTACT, mandate_id=mandate_id, cost_inr=cost)


def guard(**overrides) -> Guard:
    """A live guard by default, so a test about limits is about limits and not about mode."""
    params = overrides.pop("params", safety(mode="live"))
    overrides.setdefault("kill_switch_file", None)
    if overrides["kill_switch_file"] is None:
        # Point the switch at a path that cannot exist, so an unrelated file on the
        # developer's machine cannot make every test in this file pass for the wrong reason.
        import tempfile
        from pathlib import Path

        overrides["kill_switch_file"] = Path(tempfile.mkdtemp()) / "KILL"
    return Guard(params, **overrides)


# --------------------------------------------------------------------------------
# The spend cap. The one the task names.
# --------------------------------------------------------------------------------


def test_the_spend_cap_stops_the_contact_that_would_cross_it():
    g = guard(params=safety(mode="live", max_spend_inr_per_run=10.0))
    for _ in range(10):
        assert g.authorise(contact(1.0)).acted
    assert g.spent_inr == pytest.approx(10.0)

    refused = g.authorise(contact(1.0))
    assert not refused.allowed
    assert "spend cap" in refused.reason
    assert g.spent_inr == pytest.approx(10.0), "a refused contact must not be charged"


def test_the_cap_refuses_before_spending_not_after():
    """The difference between a cap and a report. A guard that recorded the spend and then
    noticed would be over the limit by the time it said so."""
    g = guard(params=safety(mode="live", max_spend_inr_per_run=10.0))
    assert not g.authorise(contact(10.01)).allowed
    assert g.spent_inr == 0.0


def test_the_spend_cap_cannot_be_crossed_by_any_sequence():
    """The proof, rather than the example. Three thousand randomised requests -- varied
    costs, some of them larger than the whole cap -- and the invariant is checked after
    every single one. An example test shows the cap works on the path it walks; this shows
    it holds on paths nobody wrote."""
    rng = random.Random(20260905)
    cap = 250.0
    g = guard(params=safety(mode="live", max_spend_inr_per_run=cap, max_sends_per_window=10**9))

    allowed = 0
    for _ in range(3_000):
        cost = rng.choice([0.0, 0.05, 0.15, 2.0, 25.0, 40.0, 300.0])
        if g.authorise(contact(cost)).acted:
            allowed += 1
        assert g.spent_inr <= cap + 1e-9, f"cap crossed at {g.spent_inr}"

    assert allowed > 0, "the fixture refused everything, so it proved nothing"
    assert g.spent_inr <= cap + 1e-9


def test_the_cap_applies_in_shadow_mode_too():
    """A dry run that did not consume the allowance would report that the live run fits
    inside the cap when it does not. Shadow is about not acting, not about not counting."""
    g = guard(params=safety(mode="shadow", max_spend_inr_per_run=5.0))
    for _ in range(5):
        result = g.authorise(contact(1.0))
        assert result.allowed and not result.acted
    assert not g.authorise(contact(1.0)).allowed


# --------------------------------------------------------------------------------
# Shadow mode.
# --------------------------------------------------------------------------------


def test_shadow_is_the_shipped_default():
    """The default is the design. Live-unless-configured is the same code with one word
    changed, and it ships live by accident exactly once."""
    from mandateguard.policy.loader import load_params

    assert load_params().safety.mode == "shadow"


def test_in_shadow_mode_an_action_is_allowed_and_must_not_be_performed():
    g = guard(params=safety(mode="shadow"))
    result = g.authorise(contact())
    assert result.allowed is True
    assert result.shadow is True
    assert result.acted is False, "acted is what a caller must branch on"


def test_in_live_mode_the_same_action_is_performed():
    assert guard(params=safety(mode="live")).authorise(contact()).acted is True


def test_a_misspelled_mode_is_rejected_at_load():
    """`mode: shaddow` under a truthiness check reads as not-shadow, and the system starts
    contacting customers because someone misspelled the word meant to stop it."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError, match="turns a typo into a live system"):
        safety(mode="shaddow")


# --------------------------------------------------------------------------------
# The kill switch.
# --------------------------------------------------------------------------------


def test_the_kill_switch_file_halts_everything(tmp_path):
    switch = tmp_path / "KILL"
    g = Guard(safety(mode="live"), kill_switch_file=switch)
    assert g.authorise(contact()).acted

    switch.write_text("stopped by ops", encoding="utf-8")
    refused = g.authorise(contact())
    assert not refused.allowed
    assert refused.state is Degradation.HALTED
    assert "kill switch file present" in refused.reason


def test_the_kill_switch_is_noticed_after_the_guard_was_built(tmp_path):
    """Caching the state would make a switch created mid-run invisible for the life of the
    run -- and "I touched the file and it kept sending" is the failure this prevents."""
    switch = tmp_path / "KILL"
    g = Guard(safety(mode="live"), kill_switch_file=switch)
    assert g.state()[0] is Degradation.NORMAL
    switch.touch()
    assert g.state()[0] is Degradation.HALTED


def test_tripping_in_process_halts_and_there_is_no_way_back():
    """No `untrip`. Resuming after a kill is a person restarting the system having looked
    at why it stopped, not a method next to the one that stopped it."""
    g = guard()
    g.trip("saw something wrong")
    assert not g.authorise(contact()).allowed
    assert not hasattr(g, "untrip")
    assert not hasattr(g, "reset")


def test_require_running_raises_for_the_outer_loop():
    """`authorise` returns a refusal so the ledger can hold it; this exists so a loop over
    16,000 mandates does not produce 16,000 identical halted rows."""
    g = guard()
    g.require_running()
    g.trip("stop")
    with pytest.raises(Halted, match="stop"):
        g.require_running()


# --------------------------------------------------------------------------------
# The rate limiter.
# --------------------------------------------------------------------------------


def test_the_rate_limiter_stops_a_correct_system_going_too_fast():
    """The budget stops the system spending too much; this stops it spending correctly but
    too fast. A bug pricing every ask at zero passes the budget check and empties the book
    into a send queue in one minute."""
    clock = FakeClock()
    g = guard(params=safety(mode="live", max_sends_per_window=3, window_seconds=60), clock=clock)
    for _ in range(3):
        assert g.authorise(contact(0.0)).acted
    refused = g.authorise(contact(0.0))
    assert not refused.allowed
    assert "rate limit" in refused.reason


def test_the_window_slides(clock=None):
    clock = FakeClock()
    g = guard(params=safety(mode="live", max_sends_per_window=2, window_seconds=60), clock=clock)
    assert g.authorise(contact(0.0)).acted
    assert g.authorise(contact(0.0)).acted
    assert not g.authorise(contact(0.0)).allowed

    clock.advance(61)
    assert g.authorise(contact(0.0)).acted


def test_a_zero_cost_contact_still_consumes_the_rate_limit():
    """The in-app channel costs INR 0. If free contacts were unlimited, the one channel
    that reaches every customer in the book would have no ceiling at all."""
    clock = FakeClock()
    g = guard(params=safety(mode="live", max_sends_per_window=2, window_seconds=60), clock=clock)
    g.authorise(contact(0.0))
    g.authorise(contact(0.0))
    assert not g.authorise(contact(0.0)).allowed


# --------------------------------------------------------------------------------
# The degradation ladder. One test per rung, as the task asks.
# --------------------------------------------------------------------------------


def test_rung_1_llm_down_leads_to_rules_only():
    g = guard()
    g.mark_llm_unavailable("no cassette and no credential")
    state, why = g.state()
    assert state is Degradation.RULES_ONLY
    assert "no cassette" in why

    refused = g.authorise(Action(kind=ActionKind.MODEL_CALL, cost_usd=0.01))
    assert not refused.allowed
    assert "rules-only" in refused.reason

    assert g.authorise(contact()).acted, "contacts continue; only the model call stops"


def test_rung_2_a_stale_model_leads_to_the_conservative_floor():
    """The counter-intuitive rung. An old model still outputs a confident hazard and the
    allocator still spends real money on it; not asking is the only bounded action."""
    g = guard(model_trained_on=date(2026, 1, 1), today=date(2026, 9, 2))
    state, why = g.state()
    assert state is Degradation.CONSERVATIVE_FLOOR
    assert "244 days old" in why

    refused = g.authorise(contact())
    assert not refused.allowed
    assert refused.state is Degradation.CONSERVATIVE_FLOOR


def test_a_fresh_model_does_not_degrade():
    g = guard(model_trained_on=date(2026, 8, 20), today=date(2026, 9, 2))
    assert g.state()[0] is Degradation.NORMAL
    assert g.authorise(contact()).acted


def test_the_staleness_boundary_is_the_configured_limit():
    limit = safety(mode="live", max_model_age_days=30)
    exact = Guard(limit, model_trained_on=date(2026, 8, 3), today=date(2026, 9, 2))
    assert exact.model_age_days == 30
    assert exact.state()[0] is Degradation.NORMAL

    over = Guard(limit, model_trained_on=date(2026, 8, 2), today=date(2026, 9, 2))
    assert over.model_age_days == 31
    assert over.state()[0] is Degradation.CONSERVATIVE_FLOOR


def test_rung_3_a_policy_hash_mismatch_halts_and_says_so():
    """The rulebook changed under a running system. Halting is the only action that cannot
    be wrong -- the same refusal `replay` makes, for the same reason."""
    g = guard(expected_policy_hash="0000000000000000")
    state, why = g.state()
    assert state is Degradation.HALTED
    assert "policy hash mismatch" in why
    assert not g.authorise(contact()).allowed


def test_a_matching_policy_hash_does_not_halt():
    from mandateguard.policy.loader import policy_hash

    g = guard(expected_policy_hash=policy_hash())
    assert g.state()[0] is Degradation.NORMAL


def test_the_worst_rung_wins_when_several_apply():
    """Ordering by severity is a property of the type, not of a comparison somebody wrote:
    two independent problems must resolve to the worse one."""
    g = guard(model_trained_on=date(2026, 1, 1), today=date(2026, 9, 2))
    g.mark_llm_unavailable()
    assert g.state()[0] is Degradation.CONSERVATIVE_FLOOR

    g.trip("and now this")
    assert g.state()[0] is Degradation.HALTED


def test_the_ladder_is_ordered_by_severity():
    assert (
        Degradation.NORMAL
        < Degradation.RULES_ONLY
        < Degradation.CONSERVATIVE_FLOOR
        < Degradation.HALTED
    )
    assert max(Degradation.RULES_ONLY, Degradation.HALTED) is Degradation.HALTED


# --------------------------------------------------------------------------------
# Ordering, which is itself a decision.
# --------------------------------------------------------------------------------


def test_a_halted_system_does_not_consume_the_rate_limit():
    clock = FakeClock()
    g = guard(params=safety(mode="live", max_sends_per_window=2, window_seconds=60), clock=clock)
    g.trip("stopped")
    for _ in range(10):
        assert not g.authorise(contact(0.0)).allowed
    assert g.contacts == 0
    assert g.spent_inr == 0.0


def test_a_refused_action_is_a_record_not_an_exception():
    """The ledger has to be able to say "this contact was not made because the kill switch
    was on". That is a row, not a stack trace."""
    g = guard()
    g.trip("stopped")
    result = g.authorise(contact())
    assert result.allowed is False
    assert result.reason
    assert result.state is Degradation.HALTED
