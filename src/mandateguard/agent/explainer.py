"""Job 4 of four: why this mandate was *not* asked, in rupees a person can argue with (T4.5).

### Who reads this

Not the customer. A customer who was never contacted has not been told anything and should
not be. This text is for the merchant's operations team, for whoever is asked "why did we
leave this subscriber alone", and for the auditor reading the refusal ledger (T5.1) a year
later. That audience wants the number and the trade-off, not reassurance.

### Why the existing string was not enough

`value/price.py` already produces a decomposed, rupee-backed refusal:

    not asked: via sms the expected value is INR -0.11. It would prevent only INR 0.04 of
    lapses while risking INR 0.09 of revocations, INR 0.05 of fatigue and INR 0.15 of cost

That is correct, and it is written for the person who built the pricer. It says "expected
value" without saying expected over what, it reports four terms without saying which one
decided the outcome, and it never mentions that a *different* mandate got the money. T4.5's
bar is a plain-language reason, and plain language is a different artefact from a correct
one.

### Three refusals, not one

Conflating them makes a refusal ledger useless, so they are separate kinds with separate
sentences:

* **`not_worth_asking`** -- every channel priced negative. This mandate was never a
  candidate; more budget would not change it.
* **`outbid`** -- there *was* a positive-value ask and it lost the week's budget to
  higher-value mandates. This is the actionable one, and it is the sentence the shadow
  price makes concrete: at theta, here is what one more rupee of budget would have bought.
* **`floor`** -- the policy does not contact anyone at all (P0). Not a judgement about this
  mandate, and saying so plainly keeps the P0 ledger from reading like 480,000 individual
  decisions.

### The model's part, and the leash on it

`RefusalExplainer(client=None)` produces the deterministic sentence and needs nothing. With
a client, the model *rewrites* that sentence into plainer English -- and its output is
checked back against the record before it is used: **every rupee figure in the rewrite must
be one of the figures it was given.** A rewrite that invents a number is discarded and the
deterministic sentence is used instead, silently and by design; there is no retry loop here
because there is a correct answer already in hand.

That check is the same one `agent/linter.py` runs on notices, for the same reason. The two
jobs differ in what a failure costs -- a bad notice is a regulatory breach and stops the
send, a bad explanation is a wrong number in a ledger and falls back -- so they differ in
what they do about it, not in whether they check.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from mandateguard.agent.client import LLMClient
from mandateguard.agent.linter import currency_amounts, indian_grouping
from mandateguard.models import Decision, DecisionKind

JOB = "refusal_explainer"

__all__ = [
    "JOB",
    "Explanation",
    "RefusalExplainer",
    "RefusalFacts",
    "RefusalKind",
    "explain_deterministically",
]


class RefusalKind(StrEnum):
    """Why a mandate was not asked. Three different answers to three different questions."""

    NOT_WORTH_ASKING = "not_worth_asking"
    OUTBID = "outbid"
    FLOOR = "floor"


class RefusalFacts(BaseModel):
    """The record a refusal explanation must stay inside.

    Every rupee figure the explanation is allowed to contain is a field here. That is not a
    convention -- `allowed_amounts()` is computed from these fields and anything else in the
    text is treated as fabricated.
    """

    model_config = ConfigDict(frozen=True)

    mandate_id: str
    week: int = Field(ge=0)
    kind: RefusalKind
    channel: str | None = Field(
        default=None, description="the best channel considered; None for the floor policy"
    )
    gain_inr: Decimal = Field(default=Decimal("0"), ge=0)
    backfire_inr: Decimal = Field(default=Decimal("0"), ge=0)
    fatigue_inr: Decimal = Field(default=Decimal("0"), ge=0)
    channel_cost_inr: Decimal = Field(default=Decimal("0"), ge=0)
    net_inr: Decimal = Decimal("0")
    budget_inr: Decimal | None = Field(default=None, ge=0)
    theta_inr: Decimal | None = Field(
        default=None, ge=0, description="the week's shadow price, when the arm has a dual"
    )
    competing_asks: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _an_outbid_refusal_names_what_it_lost(self) -> RefusalFacts:
        """`outbid` without a channel is `not_worth_asking` wearing the wrong label.

        The distinction is the only actionable thing in a refusal ledger -- one of them says
        "buy more budget", the other says "do not bother" -- and a mislabelled row quietly
        moves a mandate between those two answers.
        """
        if self.kind is RefusalKind.OUTBID and self.channel is None:
            raise ValueError(
                f"refusal for {self.mandate_id!r} is OUTBID but names no channel. A mandate "
                "cannot lose a budget contest it never entered; that is NOT_WORTH_ASKING."
            )
        if self.kind is RefusalKind.OUTBID and self.net_inr <= 0:
            raise ValueError(
                f"refusal for {self.mandate_id!r} is OUTBID with a net value of "
                f"{self.net_inr}. A negative-value ask did not lose to a better one, it was "
                "never worth making."
            )
        return self

    @property
    def total_cost_inr(self) -> Decimal:
        """Backfire plus fatigue plus channel cost.

        A derived figure, and it is in `allowed_amounts` for a reason worth stating: the
        deterministic sentence prints it, so a rewrite that faithfully repeats the sentence
        would otherwise be rejected for quoting a number the record "does not contain".
        The first version of this class made exactly that mistake, and it would have
        discarded every correct rewrite of a NOT_WORTH_ASKING refusal.
        """
        return self.backfire_inr + self.fatigue_inr + self.channel_cost_inr

    def allowed_amounts(self) -> set[str]:
        """Every rupee figure an explanation of this refusal may contain.

        Both spellings of each: `-0.11` and `0.11`, because prose usually writes "loses 11
        paise" rather than "is worth -0.11", and a fabrication check that rejected the
        natural phrasing would reject every good rewrite.

        The set is derived from the fields plus the one sum the deterministic sentence
        itself prints. Nothing else: a rewrite may repeat what it was given, in either sign
        and either grouping, and may not compute anything new.
        """
        figures = [
            self.gain_inr,
            self.backfire_inr,
            self.fatigue_inr,
            self.channel_cost_inr,
            self.total_cost_inr,
            self.net_inr,
            self.budget_inr,
            self.theta_inr,
        ]
        allowed: set[str] = set()
        for figure in figures:
            if figure is None:
                continue
            for value in (figure, -figure):
                quantised = value.quantize(Decimal("0.01"))
                allowed.add(indian_grouping(quantised).lstrip("-"))
                allowed.add(f"{abs(quantised):.2f}")
                if quantised == quantised.to_integral_value():
                    allowed.add(str(abs(int(quantised))))
        return allowed

    @property
    def dominant_cost(self) -> tuple[str, Decimal]:
        """Which of the three costs actually sank this ask.

        The four-term decomposition is correct and does not answer the question anyone
        asks, which is "what stopped it". Naming the largest term is what turns a
        breakdown into an explanation.
        """
        costs = {
            "the risk of pushing the customer into cancelling": self.backfire_inr,
            "the annoyance of contacting them again so soon": self.fatigue_inr,
            "the cost of the message itself": self.channel_cost_inr,
        }
        return max(costs.items(), key=lambda item: item[1])


def _rupees(value: Decimal) -> str:
    return f"INR {indian_grouping(value.quantize(Decimal('0.01')))}"


def explain_deterministically(facts: RefusalFacts) -> str:
    """The sentence that is always available. No model, byte-identical across runs."""
    if facts.kind is RefusalKind.FLOOR:
        return (
            f"Not contacted in week {facts.week}. This policy never contacts anyone -- it is "
            "the do-nothing floor every other arm is measured against, so this is not a "
            "judgement about this mandate."
        )

    if facts.kind is RefusalKind.NOT_WORTH_ASKING:
        label, amount = facts.dominant_cost
        return (
            f"Not contacted in week {facts.week}. Reaching this customer"
            + (f" by {facts.channel}" if facts.channel else "")
            + f" would have been worth {_rupees(facts.gain_inr)} in lapses avoided, and cost "
            f"{_rupees(facts.backfire_inr + facts.fatigue_inr + facts.channel_cost_inr)} "
            f"once {label} is counted -- {_rupees(amount)} of it from that alone. The ask "
            f"loses {_rupees(abs(facts.net_inr))}, so it was not made. A larger budget would "
            "not change this."
        )

    contest = ""
    if facts.competing_asks is not None and facts.budget_inr is not None:
        contest = (
            f" {facts.competing_asks:,} asks competed for {_rupees(facts.budget_inr)} this week."
        )
    threshold = ""
    if facts.theta_inr is not None:
        threshold = (
            f" The week's cut-off was {_rupees(facts.theta_inr)} of value per rupee spent; "
            "this ask was below it."
        )
    return (
        f"Not contacted in week {facts.week}. Asking by {facts.channel} was worth "
        f"{_rupees(facts.net_inr)} and was still not made: other mandates were worth more "
        f"per rupee of budget.{contest}{threshold} This one is a budget decision, not a "
        "judgement that the customer was not worth reaching."
    )


class Explanation(BaseModel):
    """The explanation, and whether a model touched it."""

    model_config = ConfigDict(frozen=True)

    mandate_id: str
    text: str = Field(min_length=1)
    source: str = Field(description="'deterministic' or 'llm'")
    rejected_rewrite: str = Field(
        default="", description="a model rewrite that failed the fabrication check"
    )
    invented_amounts: list[str] = Field(default_factory=list)


SYSTEM = """\
You rewrite one internal explanation of why a subscription-recovery system chose NOT to \
contact a customer. The reader is the merchant's operations team and, later, an auditor -- \
not the customer, who was never contacted and is not being written to.

Rewrite the explanation you are given into plain English. Keep it to two or three short \
sentences.

The one hard rule: **every rupee figure in your rewrite must be a figure that appears in \
the explanation you were given.** Do not round them, do not add them up, do not compute a \
percentage, and do not introduce a figure of your own. A rewrite containing any other \
number is discarded in full and the original is used instead, so there is nothing to gain \
by estimating.

Do not soften the decision and do not apologise for it. Not contacting someone is a normal, \
deliberate outcome of a budget and a risk calculation, and the reader needs to see the \
trade-off, not a reassurance about it. Write the rewrite only -- no preamble, no heading.
"""


class RefusalExplainer:
    """Deterministic by default; a model may rewrite, never invent."""

    def __init__(self, *, client: LLMClient | None = None) -> None:
        self.client = client

    def explain(self, facts: RefusalFacts) -> Explanation:
        baseline = explain_deterministically(facts)
        if self.client is None:
            return Explanation(mandate_id=facts.mandate_id, text=baseline, source="deterministic")

        result = self.client.run(JOB, SYSTEM, baseline)
        if result.refused or not result.text.strip():
            return Explanation(mandate_id=facts.mandate_id, text=baseline, source="deterministic")

        rewrite = result.text.strip()
        invented = _invented_amounts(rewrite, facts)
        if invented:
            return Explanation(
                mandate_id=facts.mandate_id,
                text=baseline,
                source="deterministic",
                rejected_rewrite=rewrite,
                invented_amounts=invented,
            )
        return Explanation(mandate_id=facts.mandate_id, text=rewrite, source="llm")

    def explain_decision(self, decision: Decision, facts: RefusalFacts) -> Decision:
        """Return the decision with its `reason` replaced by the explanation.

        Returns a new `Decision` rather than mutating one: `Decision` is frozen, and the
        ledger entry that quotes it (T5.1) has to be able to say which text was recorded
        at the time rather than which text the object holds now.
        """
        if decision.kind is not DecisionKind.NOT_ASKED:
            return decision
        return decision.model_copy(update={"reason": self.explain(facts).text})


def _invented_amounts(text: str, facts: RefusalFacts) -> list[str]:
    """Rupee figures in the rewrite that are not in the record.

    Compared as numbers rather than as strings, so `INR 0.10` and `INR 0.1` are the same
    figure and a rewrite is not rejected over a trailing zero. `INR 1,000` and `INR 1000`
    are likewise the same, which matters because Indian grouping is not universal even in
    India.
    """
    allowed_numeric: set[Decimal] = set()
    for spelling in facts.allowed_amounts():
        try:
            allowed_numeric.add(Decimal(spelling.replace(",", "")))
        except InvalidOperation:  # pragma: no cover -- allowed_amounts builds these itself
            continue

    invented: list[str] = []
    for whole, digits in currency_amounts(text):
        try:
            value = Decimal(digits.replace(",", ""))
        except InvalidOperation:
            invented.append(whole)
            continue
        if value not in allowed_numeric:
            invented.append(whole)
    return invented
