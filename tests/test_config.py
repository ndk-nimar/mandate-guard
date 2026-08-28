"""Config and policy loader tests (T0.8).

These guard the invariants the rest of the system assumes, so that a bad edit to a YAML
file fails here rather than silently changing every rupee number downstream.
"""

from __future__ import annotations

import pytest
import yaml
from pydantic import ValidationError

from mandateguard.policy.loader import (
    POLICY_PATH,
    Params,
    Policy,
    load_params,
    load_policy,
    policy_hash,
)


def test_params_load_and_validate():
    p = load_params()
    assert p.channels
    assert p.horizon.weeks == 12
    assert p.value.fatigue_half_life_days > 0


def test_recovery_after_lapse_exceeds_after_revocation():
    """q > r, checked at the config level too -- see docs/problem.md 6.2.

    models.Mandate enforces this per-mandate; this catches a bad default before any
    mandate is ever constructed from it.
    """
    r = load_params().recovery
    assert r.after_lapse > r.after_revocation


def test_at_least_one_free_non_intrusive_channel_exists():
    """docs/problem.md 5.3: skipped mandates are still contacted through a zero-cost
    channel. If none exists, "not selected" really would mean "abandoned"."""
    free = [c for c in load_params().channels if not c.intrusive]
    assert free, "no non-intrusive channel configured"
    assert all(c.cost_inr == 0 for c in free)


def test_channel_costs_are_distinct():
    """Distinct per-channel costs are what make this a multiple-choice knapsack rather
    than a sort (docs/problem.md 5.2). Uniform costs would degenerate the optimiser."""
    costs = [c.cost_inr for c in load_params().channels]
    assert len(set(costs)) > 1


def test_policy_loads_and_hashes_stably():
    pol = load_policy()
    assert pol.version >= 0
    assert policy_hash() == policy_hash()
    assert len(policy_hash()) == 16


def test_policy_rules_must_cite_a_clause():
    """An uncited rule is a hallucinated rule. The loader rejects it rather than
    trusting the LLM policy compiler's output (T4.1)."""
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    raw["rules"] = [{"rule_id": "r1", "clause": "", "description": "d", "expression": "true"}]
    with pytest.raises(ValidationError):
        Policy.model_validate(raw)


RECOVERY = {
    "after_lapse": 0.41,
    "after_revocation": 0.08,
    "swept_ceiling_after_revocation": 0.29,
}

INDIA = {
    "snapshot_date": "2017-02-28",
    "ntd_to_inr": 1.0,
    "rail_mix": {"upi_autopay": 0.55, "card": 0.25, "enach": 0.15, "ppi": 0.05},
    "upi_autopay_afa_threshold_inr": 15000.0,
    "mandate_validity_days": 730,
    "reachability_fraction_of_ltv": 0.15,
    "plausible_age_years": [13, 90],
    "default_debit_frequency_days": 30,
}


def params_payload(**overrides):
    """A minimal valid Params body, so each test can break exactly one thing."""
    payload = {
        "channels": [
            {"name": "free", "cost_inr": 0.0, "efficacy_prior": 0.02, "intrusive": False},
            {"name": "sms", "cost_inr": 0.15, "efficacy_prior": 0.05, "intrusive": True},
        ],
        "value": {
            "mu_good_outcome": 1.0,
            "nu_complaint": 1.0,
            "alpha_reachability": 1.0,
            "gamma_fatigue": 1.0,
            "fatigue_half_life_days": 15,
            "rho_template_reuse": 1.0,
        },
        "recovery": dict(RECOVERY),
        "horizon": {"weeks": 12, "budget_inr_per_week": 500.0},
        "india": dict(INDIA),
        "seed": 1,
    }
    payload.update(overrides)
    return payload


def test_the_minimal_payload_is_actually_valid():
    """Without this, every `pytest.raises` below could be passing because the payload
    was malformed in some unrelated way rather than because of the thing under test."""
    assert Params.model_validate(params_payload()).seed == 1


def test_negative_channel_cost_is_rejected():
    bad = [{"name": "bad", "cost_inr": -1.0, "efficacy_prior": 0.1, "intrusive": True}]
    with pytest.raises(ValidationError):
        Params.model_validate(params_payload(channels=bad))


def test_recovery_ordering_is_rejected_at_load_not_just_per_mandate():
    """q <= r is incoherent: a mandate the customer deliberately killed cannot be
    easier to win back than one that merely expired (T1.2 measured 0.41 vs a 0.29
    ceiling). Catching it in the YAML means it never reaches a Mandate at all."""
    with pytest.raises(ValidationError, match="must exceed"):
        Params.model_validate(params_payload(recovery=RECOVERY | {"after_lapse": 0.05}))


def test_r_cannot_be_swept_above_its_measured_ceiling():
    """0.35 is above the 0.29 ceiling but still below q, so only the ceiling check can
    reject it. That ceiling is the only thing keeping the r sweep attached to evidence:
    without it, `after_revocation: 0.35` would load silently and every saving the
    optimiser reports would rest on a number nobody measured."""
    with pytest.raises(ValidationError, match="ceiling"):
        Params.model_validate(params_payload(recovery=RECOVERY | {"after_revocation": 0.35}))


def test_shipped_r_sits_inside_its_ceiling():
    r = load_params().recovery
    assert 0 < r.after_revocation <= r.swept_ceiling_after_revocation < r.after_lapse


def test_a_rail_mix_that_does_not_sum_to_one_is_rejected():
    """The rail mix is a synthetic overlay (T1.3): KKBox never published what
    `payment_method_id` means, so the mix is assigned rather than measured. A mix
    summing to 0.9 would silently leave a tenth of the book on the last rail in the
    ladder, and nothing downstream would notice."""
    with pytest.raises(ValidationError, match="must sum to 1"):
        Params.model_validate(
            params_payload(india=INDIA | {"rail_mix": {"card": 0.5, "upi_autopay": 0.4}})
        )


def test_an_inverted_age_range_is_rejected():
    """`plausible_age_years` decides which ages become NULL. Inverted, it would null
    every age in the book and the field would look uniformly missing rather than wrong."""
    with pytest.raises(ValidationError, match="plausible_age_years"):
        Params.model_validate(params_payload(india=INDIA | {"plausible_age_years": [90, 13]}))


def test_the_shipped_snapshot_does_not_run_past_the_transaction_data():
    """`transactions` ends 2017-02-28 (docs/mapping.md 1). A snapshot after that would
    quietly build the book from a partial month and call it a full one."""
    from datetime import date

    assert load_params().india.snapshot_date <= date(2017, 2, 28)
