"""Config and policy loading, with the invariants that matter enforced at load time.

Two rules are enforced here rather than trusted:
  * every policy rule cites a clause (an uncited rule is a hallucinated rule)
  * the policy file's hash is recoverable, so every ledger entry can pin the exact
    policy version a decision was made under, and `replay` can reproduce it (T5.2)
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

from mandateguard.models import Channel, PolicyRule

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
    after_lapse: float = Field(ge=0, le=1)
    after_revocation: float = Field(ge=0, le=1)


class HorizonParams(BaseModel):
    weeks: int = Field(gt=0)
    budget_inr_per_week: float = Field(ge=0)


class Params(BaseModel):
    channels: list[Channel]
    value: ValueParams
    recovery: RecoveryParams
    horizon: HorizonParams
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
