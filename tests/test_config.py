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


def test_negative_channel_cost_is_rejected():
    with pytest.raises(ValidationError):
        Params.model_validate(
            {
                "channels": [
                    {
                        "name": "bad",
                        "cost_inr": -1.0,
                        "efficacy_prior": 0.1,
                        "intrusive": True,
                    }
                ],
                "value": {
                    "mu_good_outcome": 1.0,
                    "nu_complaint": 1.0,
                    "alpha_reachability": 1.0,
                    "gamma_fatigue": 1.0,
                    "fatigue_half_life_days": 15,
                    "rho_template_reuse": 1.0,
                },
                "recovery": {"after_lapse": 0.3, "after_revocation": 0.1},
                "horizon": {"weeks": 12, "budget_inr_per_week": 500.0},
                "seed": 1,
            }
        )
