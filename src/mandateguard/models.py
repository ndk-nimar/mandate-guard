"""Domain models -- the contract every layer imports.

Field names mirror Razorpay Subscriptions/Tokens vocabulary where a direct equivalent
exists (`mandate_id`, `customer_id`, `method`, `status`, `current_end`, `expire_by`), so
that a reader coming from those APIs recognises the objects immediately.

Two design points that carry weight elsewhere:

* `Decision` represents a *not-asked* outcome as a first-class value, not as the absence
  of a record. The refusal ledger (T5.1) depends on this -- auditable non-action is one of
  the things that makes this system different from a message scheduler.
* `Mandate` carries two recovery probabilities, `recovery_after_lapse` (q) and
  `recovery_after_revocation` (r), with q > r enforced. See docs/problem.md 6.2: a single
  shared number cannot express that a failed ask converts a soft ending into a hard one.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Rail(StrEnum):
    """Payment rail the mandate is registered on."""

    UPI_AUTOPAY = "upi_autopay"
    CARD = "card"
    ENACH = "enach"
    PPI = "ppi"


class MandateStatus(StrEnum):
    """Lifecycle state. `EXPIRED` and `CANCELLED` are the two endings that matter and
    they are *not* interchangeable -- see docs/problem.md 6.2."""

    ACTIVE = "active"
    PENDING = "pending"
    HALTED = "halted"
    EXPIRED = "expired"  # lapsed quietly; customer neutral, often re-acquirable
    CANCELLED = "cancelled"  # revoked by the customer; harder to recover


class DeathKind(StrEnum):
    """How a mandate's spell ended, in the person-period frame (T1.4).

    `MandateStatus` describes a mandate's state at a point in time; this describes the
    *event* that ended a spell of coverage, which is what a survival model predicts.
    They are kept apart deliberately: a mandate can be `EXPIRED` at the snapshot for a
    death that happened eighteen months earlier, and the two prices in
    `docs/problem.md` 6.2 attach to the event, not to the state.
    """

    LAPSE = "lapse"  # coverage simply ran out -- recovers with probability q
    REVOCATION = "revocation"  # cancelled first -- recovers with probability r, r < q


class DecisionKind(StrEnum):
    ASKED = "asked"
    NOT_ASKED = "not_asked"


class Channel(BaseModel):
    """A way to contact a customer, with its own price and efficacy prior.

    Distinct per-channel costs are what make the allocation a genuine multiple-choice
    knapsack rather than a sort (docs/problem.md 5.2).
    """

    model_config = ConfigDict(frozen=True)

    name: str
    cost_inr: float = Field(ge=0)
    efficacy_prior: float = Field(ge=0, le=1, description="prior P(re-consent | contacted)")
    intrusive: bool = Field(
        description="Non-intrusive zero-cost channels do not consume the ask budget "
        "(docs/problem.md 5.3), though they still incur fatigue."
    )


class Mandate(BaseModel):
    """One standing authorisation."""

    model_config = ConfigDict(frozen=True)

    mandate_id: str
    customer_id: str
    method: Rail
    status: MandateStatus
    amount_inr: float = Field(gt=0, description="per-debit amount")
    debit_frequency_days: int = Field(gt=0)
    current_end: date = Field(description="end of the current billing cycle")
    expire_by: date = Field(description="mandate validity expiry")

    ltv_remaining_inr: float = Field(
        ge=0, description="expected remaining revenue if the mandate survives (L)"
    )
    recovery_after_lapse: float = Field(
        ge=0, le=1, description="q -- P(re-acquire | lapsed quietly)"
    )
    recovery_after_revocation: float = Field(
        ge=0, le=1, description="r -- P(re-acquire | revoked in irritation)"
    )
    reachability_value_inr: float = Field(
        ge=0, description="R -- option value of still holding a channel to this customer"
    )

    @model_validator(mode="after")
    def _lapse_recovers_better_than_revocation(self) -> Self:
        """q > r is a modelling invariant, not a coincidence of the data.

        Equal values collapse the two endings into one and silently delete the argument
        that a failed ask can convert a soft ending into a hard one.
        """
        if self.recovery_after_lapse <= self.recovery_after_revocation:
            raise ValueError(
                f"recovery_after_lapse (q={self.recovery_after_lapse}) must exceed "
                f"recovery_after_revocation (r={self.recovery_after_revocation}); "
                "see docs/problem.md 6.2"
            )
        return self

    def loss_on_lapse(self) -> float:
        """L * (1 - q)."""
        return self.ltv_remaining_inr * (1.0 - self.recovery_after_lapse)

    def loss_on_revocation(self, alpha: float = 1.0) -> float:
        """L * (1 - r) + alpha * R -- strictly worse than lapsing, by construction."""
        return (
            self.ltv_remaining_inr * (1.0 - self.recovery_after_revocation)
            + alpha * self.reachability_value_inr
        )


class MandateWeek(BaseModel):
    """What a policy is shown about one mandate in one week of the horizon.

    A policy cannot decide anything useful from a `Mandate` alone: it needs this week's
    hazard, and it needs to know how much of this mandate is still there. So the harness
    hands it this view instead, and `Policy.allocate` takes a list of these rather than a
    list of mandates.

    `alive` is the honest cost of a deterministic simulation. In production a mandate is
    alive or it is not; here it carries the probability it survived to this week, and the
    harness scales every outcome by it. A policy is free to use it -- `GreedyEV` does --
    or to ignore it, which is exactly the failure mode `ChronologicalCap` exists to
    represent: real first-come-first-served systems do spend contacts on customers who
    are already gone.
    """

    model_config = ConfigDict(frozen=True)

    mandate_id: str
    week: int = Field(ge=0)
    hazard: float = Field(ge=0, le=1, description="h[i,t] if nobody is contacted")
    alive: float = Field(ge=0, le=1, description="P(still live at the start of this week)")
    ltv_remaining_inr: float = Field(ge=0)
    reachability_value_inr: float = Field(ge=0)
    recovery_after_lapse: float = Field(ge=0, le=1)
    recovery_after_revocation: float = Field(ge=0, le=1)
    asks_so_far: int = Field(ge=0, description="how many asks this customer has already had")
    weeks_since_last_ask: int | None = Field(
        default=None, description="d[i,t] in weeks; None if never contacted"
    )
    hazard_path: list[float] | None = Field(
        default=None,
        description="h[i,t..T] projected forward from this week; None when unavailable",
    )
    """The rest of the horizon's hazards, for an arm that can plan over them.

    Offered to **every** arm, not just to P5, and that is the point. The harness has had
    this path since T2.1a (`eval/forecast.py` builds it) and simply was not passing it on;
    handing it to one arm only would make "the multi-period arm beats the single-period
    one" a claim about *information* rather than about formulation, and the ladder exists
    to isolate allocation.

    `None` is a real case rather than a defensive default: the API layer (T5.3) is handed
    one mandate in one week by a caller who may have no forecast at all, and an arm has to
    degrade rather than fail. `allocator/whittle.py` falls back to projecting this week's
    hazard flat, and says what that costs.
    """

    @model_validator(mode="after")
    def _lapse_recovers_better_than_revocation(self) -> Self:
        """`q > r`, enforced on the *view* as well as on the mandate.

        `Mandate` has carried this since T0.6, but the policy never sees a `Mandate` --
        it sees this. The harness builds these from the forecast, so an invariant that
        only guarded the construction path would not guard the object every arm actually
        prices against.
        """
        if self.recovery_after_lapse <= self.recovery_after_revocation:
            raise ValueError(
                f"recovery_after_lapse (q={self.recovery_after_lapse}) must exceed "
                f"recovery_after_revocation (r={self.recovery_after_revocation}); "
                "see docs/problem.md 6.2"
            )
        return self

    def loss_on_lapse(self) -> float:
        """L * (1 - q) -- what a quiet ending costs."""
        return self.ltv_remaining_inr * (1.0 - self.recovery_after_lapse)

    def loss_on_revocation(self, alpha: float = 1.0) -> float:
        """L * (1 - r) + alpha * R -- strictly worse, by construction (problem.md 6.2)."""
        return (
            self.ltv_remaining_inr * (1.0 - self.recovery_after_revocation)
            + alpha * self.reachability_value_inr
        )


class Decision(BaseModel):
    """What the allocator decided for one mandate in one week.

    A `NOT_ASKED` decision is a real record with a real reason and a real rupee number.
    That is the point of the refusal ledger.
    """

    model_config = ConfigDict(frozen=True)

    mandate_id: str
    week: int = Field(ge=0)
    kind: DecisionKind
    channel: str | None = Field(default=None, description="set only when kind is ASKED")
    value_inr: float = Field(description="net expected rupee value of this decision")
    reason: str = Field(description="plain-language justification, shown in /explain")
    template_id: str | None = Field(
        default=None, description="feeds the template-reuse penalty in value/fatigue.py"
    )

    @model_validator(mode="after")
    def _channel_matches_kind(self) -> Self:
        if self.kind is DecisionKind.ASKED and self.channel is None:
            raise ValueError("an ASKED decision must name a channel")
        if self.kind is DecisionKind.NOT_ASKED and self.channel is not None:
            raise ValueError("a NOT_ASKED decision must not name a channel")
        return self


class LedgerEntry(BaseModel):
    """One append-only record. Carries everything `replay` needs to reproduce the
    decision exactly (T5.2)."""

    model_config = ConfigDict(frozen=True)

    decision_id: str
    decision: Decision
    policy_hash: str = Field(description="hash of mandate_policy.yaml at decision time")
    model_version: str
    seed: int
    created_at: date


class PolicyRule(BaseModel):
    """One compiled regulatory rule. Every rule traces back to a circular clause -- rules
    without a citation are rejected, which is what keeps the LLM policy compiler honest."""

    model_config = ConfigDict(frozen=True)

    rule_id: str
    clause: str = Field(min_length=1, description="citation into the source circular")
    description: str
    expression: str


class AllocationRequest(BaseModel):
    mandates: list[Mandate]
    budget_inr: float = Field(ge=0)
    week: int = Field(ge=0)


class AllocationResponse(BaseModel):
    decisions: list[Decision]
    theta_inr: float | None = Field(
        default=None, description="shadow price; None for policies with no dual"
    )
    budget_spent_inr: float = Field(ge=0)
