"""Config and policy loading, with the invariants that matter enforced at load time.

Three rules are enforced here rather than trusted:
  * every policy rule cites a clause (an uncited rule is a hallucinated rule)
  * the policy file's hash is recoverable, so every ledger entry can pin the exact
    policy version a decision was made under, and `replay` can reproduce it (T5.2)
  * the recovery parameters stay ordered and inside their measured ceiling (T1.2)
"""

from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field, model_validator

from mandateguard.models import Channel, PolicyRule, Rail

ROOT = Path(__file__).resolve().parents[3]
PARAMS_PATH = ROOT / "config" / "params.yaml"
POLICY_PATH = ROOT / "policy" / "mandate_policy.yaml"


class ValueParams(BaseModel):
    mu_good_outcome: float
    nu_complaint: float
    alpha_reachability: float
    gamma_fatigue: float
    fatigue_half_life_days: int = Field(gt=0)
    rho_template_reuse: float


class RecoveryParams(BaseModel):
    """q and r, plus the empirical ceiling the r sweep is not allowed to leave.

    The invariants are enforced here rather than only in `models.Mandate`, because the
    YAML is edited by hand far more often than a `Mandate` is constructed: an
    inconsistent config should fail on load, not three layers downstream.
    """

    after_lapse: float = Field(ge=0, le=1)
    after_revocation: float = Field(ge=0, le=1)
    swept_ceiling_after_revocation: float = Field(ge=0, le=1)
    """Upper bound on r measured in T1.2 (docs/mapping.md 2). The sweep runs over
    (0, ceiling]; nothing above it has any evidence behind it."""

    @model_validator(mode="after")
    def check_ordering(self) -> RecoveryParams:
        if self.after_lapse <= self.after_revocation:
            raise ValueError(
                f"recovery.after_lapse (q={self.after_lapse}) must exceed "
                f"recovery.after_revocation (r={self.after_revocation}): a mandate the "
                "customer deliberately killed cannot be easier to win back than one "
                "that merely expired. See docs/problem.md 6.2."
            )
        if self.after_revocation > self.swept_ceiling_after_revocation:
            raise ValueError(
                f"recovery.after_revocation (r={self.after_revocation}) exceeds its "
                f"measured ceiling ({self.swept_ceiling_after_revocation}). The ceiling "
                "comes from T1.2; raising r above it means claiming revoked mandates "
                "recover more often than cancelled KKBox subscriptions did, with no "
                "evidence for it."
            )
        return self


class HorizonParams(BaseModel):
    weeks: int = Field(gt=0)
    budget_inr_per_week: float = Field(ge=0)


class IndiaParams(BaseModel):
    """The KKBox -> Indian-mandate bridge (T1.3). Every field here is a decision.

    The rail mix is the one that most needs guarding: it is a synthetic overlay, and a
    mix that does not sum to 1 would silently drop or double-count part of the book.
    """

    snapshot_date: date
    ntd_to_inr: float = Field(gt=0)
    rail_mix: dict[Rail, float]
    upi_autopay_afa_threshold_inr: float = Field(gt=0)
    mandate_validity_days: int = Field(gt=0)
    reachability_fraction_of_ltv: float = Field(ge=0)
    plausible_age_years: tuple[int, int]
    default_debit_frequency_days: int = Field(gt=0)

    @model_validator(mode="after")
    def check_mix(self) -> IndiaParams:
        if any(share < 0 for share in self.rail_mix.values()):
            raise ValueError("india.rail_mix shares must be non-negative")
        total = sum(self.rail_mix.values())
        if abs(total - 1.0) > 1e-9:
            raise ValueError(
                f"india.rail_mix must sum to 1, got {total}. A mix that does not sum to "
                "1 silently drops or double-counts part of the mandate book."
            )
        low, high = self.plausible_age_years
        if not 0 < low < high:
            raise ValueError(f"india.plausible_age_years must be 0 < low < high, got {low}, {high}")
        return self


class Params(BaseModel):
    channels: list[Channel]
    value: ValueParams
    recovery: RecoveryParams
    horizon: HorizonParams
    india: IndiaParams
    seed: int


class Policy(BaseModel):
    version: int
    source: dict[str, Any]
    rules: list[PolicyRule]


def _read_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def load_params(path: Path = PARAMS_PATH) -> Params:
    return Params.model_validate(_read_yaml(path))


def load_policy(path: Path = POLICY_PATH) -> Policy:
    return Policy.model_validate(_read_yaml(path))


def policy_hash(path: Path = POLICY_PATH) -> str:
    """Content hash of the policy file, recorded on every ledger entry.

    Lets us say, in one line: change the policy and old decisions still replay under the
    policy that produced them.
    """
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
