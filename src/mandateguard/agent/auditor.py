"""Job 2 of four: a structured compliance verdict for one mandate (T4.2).

### The split that makes this auditable

The obvious build is to hand a mandate and a circular to a model and ask "is this
compliant?". That produces an answer nobody can check, that varies between runs, and that
cannot say *which clause* it turned on without being asked to also produce a citation it
may or may not have used.

This one is split instead:

* **The model extracts.** Real mandate records are messy -- a rail written as `"UPI"` or
  `"upi_autopay"` or `"UPI AutoPay (NPCI)"`, an amount as `"Rs 14,999/-"`, a notice
  timestamp in some local format. Turning that into `MandateAuditContext` is a language
  problem, and it is the one thing here a model is better at than code.
* **The rules judge.** Every verdict comes from `policy/mandate_policy.yaml` evaluated by
  `agent/expression.py`. Same context in, same verdict out, on every machine, forever --
  and every finding names the clause it came from because the rule carries it.

So the model never decides compliance. It decides *what the record says*, and it is allowed
to say it does not know.

### `needs_human` is reachable three ways, and they are different questions

1. **Scope.** `scope_cards_ppi_upi` fails for an eNACH mandate. Clause 2 covers cards, PPI
   and UPI; eNACH runs under NPCI's NACH guidelines. The rulebook does not reach it, and
   `compliant` would be a claim about a regulation that does not apply.
2. **Missing input.** Extraction could not determine a field that an applicable rule reads.
   Guessing here is how an auditor produces a confident wrong answer: `afa_on_this_transaction`
   defaulting to `False` would fail a Rs.20,000 debit that was, in fact, authenticated.
3. **An unevaluable rule.** If a rule raises, the mandate is not graded. A rule that cannot
   run is not a rule that passed.

Scope outranks breach, deliberately. A mandate that is outside the framework cannot be
*non*-compliant with it either, and reporting a breach under a rulebook that does not apply
is a false accusation rather than a conservative one.

### What happens when the model is not there

`MandateAuditor(client=None)` audits an already-structured context with no model at all,
and that is the mode CI runs. It is also the first rung of T5.3's degradation ladder --
"LLM down leads to rules-only" is not a fallback bolted on later, it is the ordinary path
with the extraction step removed.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mandateguard.agent.client import LLMClient
from mandateguard.agent.expression import (
    ExpressionError,
    evaluate,
    evaluate_tracing,
    referenced_names,
)
from mandateguard.models import (
    MandateAuditContext,
    MandateVerdict,
    PolicyRule,
    RuleOutcome,
    Verdict,
)
from mandateguard.policy.loader import Policy, load_policy, policy_hash

JOB = "mandate_auditor"

__all__ = ["ExtractionResult", "MandateAuditor", "RulesAuditor", "extraction_schema"]


class ExtractionResult(BaseModel):
    """What the model returns: a context, plus what it could not determine.

    `unknown_fields` is the load-bearing half. A model asked to fill a schema will fill it;
    the only way to get an honest abstention is to give it somewhere to put one, and then
    to treat that somewhere as more important than the values beside it.
    """

    model_config = ConfigDict(frozen=True)

    context: MandateAuditContext
    unknown_fields: list[str] = Field(
        default_factory=list,
        description="fields the record does not determine; never guessed",
    )
    notes: str = Field(default="", description="what the record said, in the model's words")


def extraction_schema() -> Mapping[str, Any]:
    """The JSON schema the extraction request declares.

    Built from `MandateAuditContext` rather than typed out, so a field added to the context
    is offered to the model on the next run instead of silently never being extracted.
    """
    properties: dict[str, Any] = {}
    for name, field in MandateAuditContext.model_fields.items():
        annotation = getattr(field.annotation, "__name__", str(field.annotation))
        entry: dict[str, Any] = {
            "description": field.description or f"{name} ({annotation})",
        }
        properties[name] = entry
    return {
        "type": "object",
        "properties": {
            "context": {"type": "object", "properties": properties},
            "unknown_fields": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "names of fields the record does not determine. Leave a field OUT of "
                    "`context` and name it here rather than guessing a value for it."
                ),
            },
            "notes": {"type": "string"},
        },
        "required": ["context", "unknown_fields", "notes"],
        "additionalProperties": False,
    }


SYSTEM = """\
You read one recurring-mandate record from an Indian payments system and turn it into a \
structured object. You do NOT decide whether it complies with anything -- a separate \
deterministic rule engine does that, using the fields you produce.

The one rule that matters: **never guess**. If the record does not determine a field, leave \
it out of `context` and put its name in `unknown_fields`. A guessed value produces a \
confident wrong compliance verdict, and a named unknown produces a human review. The second \
is always the better outcome.

Guessing includes filling a field from what is usually true. "Most mandates are \
authenticated at registration" is not evidence about this mandate.

Normalise what the record does state:

* `rail`: one of upi_autopay, card, enach, ppi. "UPI AutoPay", "UPI" -> upi_autopay. \
"NACH", "eNACH", "e-NACH" -> enach.
* `category`: one of general, insurance_premium, mutual_fund, credit_card_bill, fastag, ncmc. \
Use `general` only when the record says what the debit is for and it is none of the others; \
if the record does not say, that is an unknown.
* amounts: plain numbers in rupees. "Rs 14,999/-" -> 14999.
* `pre_debit_notice_hours`: hours between the notification and the debit. If no notification \
was sent at all, that is 0 notifications, not an unknown -- omit the field and say so in \
`notes` only if the record is silent about whether one was sent.
* the `*_notice_fields` sets: the field names the notice actually contained, from \
merchant_name, amount, debit_datetime, mandate_reference, transaction_reference, reason, \
grievance_redressal.

`notes` is one or two sentences on what the record actually said, in your own words. It is \
read by a human when the verdict is needs_human.
"""


class RulesAuditor:
    """The deterministic half. Given a context, produces a verdict and its citations.

    Holds the `Policy` rather than re-loading it: `load_policy` re-hashes a ~10 KB circular
    and re-checks twenty quotes, which is right once and wasteful 120 times.
    """

    def __init__(self, policy: Policy | None = None, *, policy_version: str | None = None) -> None:
        self.policy = policy or load_policy()
        self.policy_version = policy_version if policy_version is not None else policy_hash()

    def outcomes(self, context: MandateAuditContext) -> list[RuleOutcome]:
        """Every rule's result, including the ones that did not apply.

        Skipped rules are recorded rather than dropped. "Clause 6(a) did not apply because
        this is a FASTag mandate" is a different audit trail from "clause 6(a) was never
        evaluated", and only one of them survives someone asking why the notice rule never
        fires.
        """
        values = context.to_expression_context()
        results: list[RuleOutcome] = []
        for rule in self.policy.rules:
            results.append(self._evaluate(rule, values))
        return results

    def _evaluate(self, rule: PolicyRule, values: Mapping[str, Any]) -> RuleOutcome:
        try:
            applies = evaluate(rule.applies_when, values)
        except ExpressionError:
            return RuleOutcome(
                rule_id=rule.rule_id,
                clause=rule.clause,
                applied=True,
                passed=False,
                verdict_on_fail=Verdict.NEEDS_HUMAN,
                remedy=f"guard `{rule.applies_when}` could not be evaluated for this mandate",
            )
        if not applies:
            return RuleOutcome(
                rule_id=rule.rule_id,
                clause=rule.clause,
                applied=False,
                verdict_on_fail=rule.verdict_on_fail,
            )
        try:
            passed = evaluate(rule.expression, values)
        except ExpressionError:
            # An unevaluable rule is not a passed rule. Sending it to a human is the only
            # answer that does not invent one.
            return RuleOutcome(
                rule_id=rule.rule_id,
                clause=rule.clause,
                applied=True,
                passed=False,
                verdict_on_fail=Verdict.NEEDS_HUMAN,
                remedy=f"rule `{rule.rule_id}` could not be evaluated for this mandate",
            )
        return RuleOutcome(
            rule_id=rule.rule_id,
            clause=rule.clause,
            applied=True,
            passed=passed,
            verdict_on_fail=rule.verdict_on_fail,
            remedy=None if passed else rule.remedy,
        )

    def fields_read_by_applicable_rules(self, context: MandateAuditContext) -> set[str]:
        """Which context fields this mandate's verdict actually turned on.

        Used to turn "the model did not know X" into an abstention only when X matters. A
        mandate whose unknown field is read by no applicable rule is still gradeable, and
        abstaining on it is abstaining for the sake of caution rather than for cause -- which
        shows up in T4.7 as abstain precision falling and in production as a review queue
        full of mandates nobody needed to look at.

        The set comes from `evaluate_tracing`, which records what evaluation *touched*,
        not from `referenced_names`, which records what the string *contains*. The
        difference is short-circuiting, and it is not academic: `debit_within_customer_cap`
        is guarded by `is_variable_amount and customer_cap_inr is not None`, so the
        syntactic set makes every fixed-amount mandate in the book depend on a cap it does
        not have. That was the first version of this method, and it abstained on a clean
        mandate whose only unknown was a field no rule would ever read.

        A rule that raises contributes its syntactic set instead -- when evaluation could
        not finish, what it *would* have read is the best available answer, and erring
        wide on a rule that is already broken is the right direction.
        """
        values = context.to_expression_context()
        needed: set[str] = set()
        for rule in self.policy.rules:
            try:
                applies, touched = evaluate_tracing(rule.applies_when, values)
            except ExpressionError:
                needed |= referenced_names(rule.applies_when) | referenced_names(rule.expression)
                continue
            needed |= touched
            if not applies:
                continue
            try:
                needed |= evaluate_tracing(rule.expression, values)[1]
            except ExpressionError:
                needed |= referenced_names(rule.expression)
        return needed

    def audit(self, context: MandateAuditContext, *, decided_by: str = "rules") -> MandateVerdict:
        outcomes = self.outcomes(context)
        failures = [o for o in outcomes if o.applied and o.passed is False]

        scope = [o for o in failures if o.verdict_on_fail is Verdict.NEEDS_HUMAN]
        if scope:
            return MandateVerdict(
                mandate_id=context.mandate_id,
                verdict=Verdict.NEEDS_HUMAN,
                reason=_join(
                    "This mandate is not gradeable under the compiled framework: "
                    + "; ".join(f"clause {o.clause} ({o.rule_id}) -- {o.remedy}" for o in scope)
                ),
                citations=[o.clause for o in scope],
                outcomes=outcomes,
                decided_by=decided_by,
                policy_hash=self.policy_version,
            )

        if failures:
            return MandateVerdict(
                mandate_id=context.mandate_id,
                verdict=Verdict.NON_COMPLIANT,
                reason=_join(
                    f"{len(failures)} rule(s) failed: "
                    + "; ".join(f"clause {o.clause} ({o.rule_id}) -- {o.remedy}" for o in failures)
                ),
                citations=[o.clause for o in failures],
                outcomes=outcomes,
                decided_by=decided_by,
                policy_hash=self.policy_version,
            )

        applied = sum(1 for o in outcomes if o.applied)
        return MandateVerdict(
            mandate_id=context.mandate_id,
            verdict=Verdict.COMPLIANT,
            reason=(
                f"All {applied} applicable rule(s) passed, out of {len(outcomes)} compiled "
                f"from {self.policy.source.circular_no}."
            ),
            citations=[],
            outcomes=outcomes,
            decided_by=decided_by,
            policy_hash=self.policy_version,
        )


class MandateAuditor:
    """Extraction (optional, by model) followed by judgement (always, by rules)."""

    def __init__(self, *, client: LLMClient | None = None, policy: Policy | None = None) -> None:
        self.client = client
        self.rules = RulesAuditor(policy)

    def audit_context(self, context: MandateAuditContext) -> MandateVerdict:
        """Rules-only. No model, no network, deterministic."""
        return self.rules.audit(context)

    def audit_record(self, mandate_id: str, record: str) -> MandateVerdict:
        """Extract a context from a free-text record, then judge it.

        Every failure mode here abstains rather than guesses: a refusal, unparseable JSON,
        a payload that is not a valid context, or a named unknown that an applicable rule
        reads. Four different causes, one verdict, and the reason string says which.
        """
        if self.client is None:
            raise RuntimeError(
                "audit_record needs a model to extract a context from free text. For an "
                "already-structured mandate use audit_context, which needs nothing."
            )

        prompt = f"Mandate id: {mandate_id}\n\nRecord:\n{record}"
        result = self.client.run(JOB, SYSTEM, prompt, schema=extraction_schema())

        if result.refused:
            return self._abstain(
                mandate_id,
                f"the extraction job was refused (stop_reason={result.stop_reason!r}), so no "
                "field of this mandate was determined.",
            )
        try:
            payload = json.loads(result.text)
        except json.JSONDecodeError as exc:
            return self._abstain(mandate_id, f"extraction returned text that is not JSON: {exc}")

        try:
            extraction = ExtractionResult.model_validate(payload)
        except ValidationError as exc:
            return self._abstain(
                mandate_id,
                f"extraction produced a payload that is not a valid audit context: "
                f"{_first_error(exc)}",
            )

        context = extraction.context.model_copy(update={"mandate_id": mandate_id})
        blocking = sorted(
            set(extraction.unknown_fields) & self.rules.fields_read_by_applicable_rules(context)
        )
        if blocking:
            return self._abstain(
                mandate_id,
                f"the record does not determine {', '.join(blocking)}, and a compiled rule "
                f"reads {'them' if len(blocking) > 1 else 'it'}. Extraction notes: "
                f"{extraction.notes or 'none'}",
                context=context,
            )

        return self.rules.audit(context, decided_by="llm+rules")

    def _abstain(
        self, mandate_id: str, reason: str, *, context: MandateAuditContext | None = None
    ) -> MandateVerdict:
        """A `needs_human` verdict that still cites something.

        `MandateVerdict` refuses a non-compliant verdict with no citation, and an
        extraction failure has no rule to cite -- so it cites clause 2, the applicability
        clause, which is the honest answer to "under what authority are you telling me
        this?": none yet, because we could not establish what this mandate is.
        """
        return MandateVerdict(
            mandate_id=mandate_id,
            verdict=Verdict.NEEDS_HUMAN,
            reason=_join(reason),
            citations=["2"],
            outcomes=self.rules.outcomes(context) if context is not None else [],
            decided_by="llm+rules",
            policy_hash=self.rules.policy_version,
        )


def _join(text: str) -> str:
    """Collapse a reason to one line so it fits a ledger row and a CLI table."""
    return " ".join(text.split())


def _first_error(exc: ValidationError) -> str:
    errors: Iterable[Mapping[str, Any]] = exc.errors()
    for error in errors:
        location = ".".join(str(part) for part in error.get("loc", ()))
        return f"{location}: {error.get('msg')}"
    return str(exc)
