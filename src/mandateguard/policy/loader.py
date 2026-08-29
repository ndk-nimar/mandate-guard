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
    """The four terms of the rupee price (T3.2), each traced to a different paper."""

    mu_good_outcome: float = Field(ge=0)
    nu_complaint: float = Field(ge=0)
    alpha_reachability: float = Field(ge=0)
    gamma_fatigue: float = Field(ge=0)
    fatigue_half_life_days: int = Field(gt=0)
    rho_template_reuse: float = Field(ge=0)
    backfire_avoided_per_softer_step: float = Field(ge=0, lt=1)
    """Chrome, USENIX Security 2021. Strictly below 1: at 1 the softest channel would
    carry exactly zero backfire, which would make it free to spam and hand every arm an
    unbounded ask budget through the back door."""


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


class InterventionParams(BaseModel):
    """What an ask does to a mandate. Every field here is swept except the last.

    The validator enforces the one thing that is not a matter of taste: backfire has to
    grow with contact count. A configuration where the twelfth ask is *safer* than the
    first would quietly delete the entire reason this project rations asks, and every
    result would then say "contact everyone" -- correctly, for that world.
    """

    uplift_scale: float = Field(ge=0)
    backfire_first_ask: float = Field(ge=0, le=1)
    backfire_twelfth_ask: float = Field(ge=0, le=1)
    natural_revocation_share: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def check_backfire_grows(self) -> InterventionParams:
        if self.backfire_twelfth_ask < self.backfire_first_ask:
            raise ValueError(
                f"intervention.backfire_twelfth_ask ({self.backfire_twelfth_ask}) is "
                f"below backfire_first_ask ({self.backfire_first_ask}). Backfire that "
                "shrinks with repetition removes the reason to ration asks at all, and "
                "every arm in the ladder would then correctly recommend spraying. See "
                "docs/problem.md 5.1."
            )
        return self

    def backfire(self, ask_number: int) -> float:
        """`b(n)` for the nth ask this customer has received, 1-indexed.

        Geometric rather than linear between the two anchors, because the anchors are
        given as a ratio (0.6% to 6% is "ten times worse", not "5.4 points worse") and a
        linear ladder would put the growth in the wrong place.
        """
        if ask_number <= 1 or self.backfire_first_ask == 0:
            return self.backfire_first_ask
        ratio = self.backfire_twelfth_ask / self.backfire_first_ask
        return min(1.0, self.backfire_first_ask * ratio ** ((ask_number - 1) / 11))


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


def _check_channels(channels: list[Channel]) -> list[Channel]:
    """Three things a channel table has to be before an optimiser can use it.

    **Names unique.** A `Decision` names its channel as a string and the harness looks it
    up; two channels sharing a name means one of them can never be selected and the other
    silently absorbs its decisions.

    **At least one intrusive channel.** Non-intrusive channels cost nothing
    (`docs/problem.md` 5.3), so a table of only those gives every arm an unbounded budget
    and the ladder compares nothing.

    **No dominated channel.** If a channel is at least as cheap *and* at least as
    effective as another, nothing would ever choose the other one -- and the
    multiple-choice knapsack (T3.3) is then quietly smaller than the config claims.
    That is not an error in the data, it is an error in the experiment: the whole reason
    channels have distinct costs is to make the allocation a genuine choice rather than
    a sort (`problem.md` 5.2).
    """
    names = [c.name for c in channels]
    if len(set(names)) != len(names):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        raise ValueError(
            f"channel names must be unique; {duplicates} appear more than once. A "
            "Decision names its channel as a string, so a duplicate makes one of them "
            "unreachable and the other silently absorb its decisions."
        )
    if not any(c.intrusive for c in channels):
        raise ValueError(
            "at least one channel must be intrusive, or no arm can spend its budget and "
            "the evaluation ladder compares nothing. See docs/problem.md 5.3."
        )
    for cheap in channels:
        for dear in channels:
            if cheap.name == dear.name:
                continue
            cheaper = cheap.cost_inr <= dear.cost_inr
            better = cheap.efficacy_prior >= dear.efficacy_prior
            if (
                cheaper
                and better
                and (cheap.cost_inr, -cheap.efficacy_prior)
                != (
                    dear.cost_inr,
                    -dear.efficacy_prior,
                )
            ):
                raise ValueError(
                    f"channel {dear.name!r} (cost {dear.cost_inr}, efficacy "
                    f"{dear.efficacy_prior}) is dominated by {cheap.name!r} (cost "
                    f"{cheap.cost_inr}, efficacy {cheap.efficacy_prior}): no optimiser "
                    "would ever choose it, so the multiple-choice knapsack is smaller "
                    "than this config claims. Channels exist to make the allocation a "
                    "choice rather than a sort -- see docs/problem.md 5.2."
                )
    return channels


class Params(BaseModel):
    channels: list[Channel]
    value: ValueParams
    recovery: RecoveryParams
    intervention: InterventionParams
    horizon: HorizonParams
    india: IndiaParams
    seed: int

    @model_validator(mode="after")
    def check_channels(self) -> Params:
        _check_channels(self.channels)
        return self


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
