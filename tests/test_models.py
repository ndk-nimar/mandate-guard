"""Contract tests for the domain models and the P0 floor policy (T0.6, T0.7)."""

from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from mandateguard.allocator import NoAskPolicy
from mandateguard.models import (
    Decision,
    DecisionKind,
    Mandate,
    MandateStatus,
    Rail,
)


def make_mandate(mandate_id: str = "mdt_1", **overrides) -> Mandate:
    base = dict(
        mandate_id=mandate_id,
        customer_id="cust_1",
        method=Rail.UPI_AUTOPAY,
        status=MandateStatus.ACTIVE,
        amount_inr=500.0,
        debit_frequency_days=30,
        current_end=date(2026, 9, 30),
        expire_by=date(2027, 4, 21),
        ltv_remaining_inr=9000.0,
        recovery_after_lapse=0.35,
        recovery_after_revocation=0.08,
        reachability_value_inr=1200.0,
    )
    return Mandate(**(base | overrides))


def test_mandate_round_trips_through_json():
    m = make_mandate()
    assert Mandate.model_validate_json(m.model_dump_json()) == m


@pytest.mark.parametrize(("q", "r"), [(0.10, 0.10), (0.05, 0.30)])
def test_lapse_must_recover_better_than_revocation(q: float, r: float):
    """q > r is a modelling invariant (docs/problem.md 6.2), enforced at construction.

    Equal or inverted values silently delete the argument that a failed ask can turn a
    soft ending into a hard one -- so they are rejected rather than tolerated.
    """
    with pytest.raises(ValidationError, match="recovery_after_lapse"):
        make_mandate(recovery_after_lapse=q, recovery_after_revocation=r)


def test_revocation_costs_more_than_lapse():
    """The economic consequence of q > r, asserted directly."""
    m = make_mandate()
    assert m.loss_on_revocation() > m.loss_on_lapse()


def test_reachability_widens_the_gap():
    """alpha * R lands only on revocation, so raising alpha must not touch lapse loss."""
    m = make_mandate()
    lapse = m.loss_on_lapse()
    assert m.loss_on_revocation(alpha=2.0) > m.loss_on_revocation(alpha=1.0) > lapse


def test_asked_decision_requires_a_channel():
    with pytest.raises(ValidationError, match="must name a channel"):
        Decision(mandate_id="mdt_1", week=0, kind=DecisionKind.ASKED, value_inr=1.0, reason="x")


def test_not_asked_decision_rejects_a_channel():
    with pytest.raises(ValidationError, match="must not name a channel"):
        Decision(
            mandate_id="mdt_1",
            week=0,
            kind=DecisionKind.NOT_ASKED,
            channel="sms",
            value_inr=0.0,
            reason="x",
        )


def test_p0_decides_for_every_mandate_and_spends_nothing():
    """The Policy contract is total: a decision per mandate, including the not-asked ones.

    Returning only the asked mandates would make the refusal ledger impossible.
    """
    mandates = [make_mandate(f"mdt_{i}") for i in range(5)]
    resp = NoAskPolicy().allocate(mandates, budget_inr=1000.0, week=0)

    assert len(resp.decisions) == len(mandates)
    assert {d.mandate_id for d in resp.decisions} == {m.mandate_id for m in mandates}
    assert all(d.kind is DecisionKind.NOT_ASKED for d in resp.decisions)
    assert all(d.reason for d in resp.decisions)
    assert resp.budget_spent_inr == 0.0
    assert resp.theta_inr is None
