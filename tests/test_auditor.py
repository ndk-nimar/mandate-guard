"""T4.2 -- the mandate auditor: what it decides, and what it refuses to decide.

Two halves, tested differently.

The **rules** half is deterministic and is tested directly against contexts: this mandate,
this verdict, these citations. Those tests are the real ones -- they are what runs in
production and what the golden set (T4.6) scores.

The **extraction** half needs a model, and no cassette has been recorded (see
`docs/limitations.md` §8.5). So it is tested through a scripted fake client, and the tests
below are explicit about what that proves and what it does not: it proves the *harness*
abstains on a refusal, on unparseable JSON, on an invalid payload and on a named unknown.
It proves nothing at all about how often a real model does any of those things. That number
has to come from recordings, and until it does, T4.7 will say so rather than fill it in.
"""

from __future__ import annotations

import json

import pytest

from mandateguard.agent.auditor import JOB, MandateAuditor, RulesAuditor, extraction_schema
from mandateguard.agent.client import LLMResult, LLMUsage
from mandateguard.models import (
    MandateAuditContext,
    MandateCategory,
    Rail,
    Verdict,
)
from mandateguard.policy.loader import load_policy

PRE_FIELDS = frozenset({"merchant_name", "amount", "debit_datetime", "mandate_reference", "reason"})
POST_FIELDS = PRE_FIELDS | {"transaction_reference", "grievance_redressal"}


def context(**overrides) -> MandateAuditContext:
    """A mandate that passes every applicable rule, so each test can break exactly one."""
    base = {
        "mandate_id": "mg_1",
        "rail": Rail.UPI_AUTOPAY,
        "category": MandateCategory.GENERAL,
        "amount_inr": 499.0,
        "pre_debit_notice_hours": 36.0,
        "pre_debit_notice_fields": PRE_FIELDS,
        "post_transaction_notice_fields": POST_FIELDS,
    }
    return MandateAuditContext(**(base | overrides))


@pytest.fixture(scope="module")
def auditor() -> MandateAuditor:
    """One auditor for the module: `load_policy` re-hashes the circular on every call."""
    return MandateAuditor()


# --------------------------------------------------------------------------------
# The baseline. Without it every assertion below could pass for the wrong reason.
# --------------------------------------------------------------------------------


def test_a_clean_mandate_is_compliant_and_cites_nothing(auditor):
    verdict = auditor.audit_context(context())
    assert verdict.verdict is Verdict.COMPLIANT
    assert verdict.citations == []
    assert verdict.decided_by == "rules"
    assert verdict.policy_hash


def test_the_verdict_records_the_rules_that_did_not_apply(auditor):
    """A skipped rule is part of the audit trail, not an absence from it.

    Without this, "clause 6(a) did not apply, this is a FASTag mandate" and "clause 6(a) was
    never evaluated" look identical from the ledger, and only one of them is fine.
    """
    outcomes = auditor.audit_context(context()).outcomes
    assert len(outcomes) == len(auditor.rules.policy.rules)
    skipped = [o for o in outcomes if not o.applied]
    assert skipped, "no rule was guarded, so the guards are doing nothing"
    assert all(o.passed is None for o in skipped)


# --------------------------------------------------------------------------------
# needs_human, reachable three ways. T4.2's stated bar.
# --------------------------------------------------------------------------------


def test_an_enach_mandate_is_not_graded_at_all(auditor):
    """Reachable way 1: scope. Clause 2 covers cards / PPI / UPI and eNACH is not in it.

    `compliant` would be a claim about a rulebook that does not reach this mandate, and
    `non_compliant` would be an accusation under one. Neither is available.
    """
    verdict = auditor.audit_context(context(rail=Rail.ENACH))
    assert verdict.verdict is Verdict.NEEDS_HUMAN
    assert verdict.citations == ["2"]
    assert "not gradeable" in verdict.reason


def test_scope_outranks_a_breach_rather_than_being_listed_beside_it(auditor):
    """An eNACH mandate that ALSO sends its notice late is still `needs_human`.

    The alternative -- reporting the 6(a) breach -- would be a finding under a framework
    that does not apply to this mandate. Conservative-looking and wrong.
    """
    verdict = auditor.audit_context(context(rail=Rail.ENACH, pre_debit_notice_hours=2.0))
    assert verdict.verdict is Verdict.NEEDS_HUMAN
    assert verdict.citations == ["2"]


def test_an_unevaluable_rule_abstains_rather_than_passing():
    """Reachable way 3. A rule that raises has not passed.

    Forced by dropping the guard off `debit_within_customer_cap`, whose expression is
    `amount_inr <= customer_cap_inr` -- against a fixed-amount mandate, that compares a
    float with None and Python refuses. This is a real failure mode rather than a contrived
    one: it is exactly what a mis-guarded rule looks like, and the compiler can produce one
    (the guard is a separate string the model writes, and nothing checks that a guard covers
    its own expression's None cases).

    The alternative behaviours are both worse. Treating the raise as a pass hides a broken
    rule behind a clean verdict; treating it as a breach accuses a mandate of violating a
    rule that never ran.
    """
    policy = load_policy()
    misguarded = [
        rule.model_copy(update={"applies_when": "True"})
        if rule.rule_id == "debit_within_customer_cap"
        else rule
        for rule in policy.rules
    ]
    broken_policy = policy.model_copy(update={"rules": misguarded})

    verdict = RulesAuditor(broken_policy).audit(context())
    assert verdict.verdict is Verdict.NEEDS_HUMAN
    assert "4(c)" in verdict.citations
    assert "could not be evaluated" in verdict.reason


# --------------------------------------------------------------------------------
# The clause boundaries that decide real money.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("amount", "category", "expected"),
    [
        (14_999.0, MandateCategory.GENERAL, Verdict.COMPLIANT),
        (15_000.0, MandateCategory.GENERAL, Verdict.COMPLIANT),
        (15_001.0, MandateCategory.GENERAL, Verdict.NON_COMPLIANT),
        (20_000.0, MandateCategory.INSURANCE_PREMIUM, Verdict.COMPLIANT),
        (20_000.0, MandateCategory.MUTUAL_FUND, Verdict.COMPLIANT),
        (20_000.0, MandateCategory.CREDIT_CARD_BILL, Verdict.COMPLIANT),
        (100_000.0, MandateCategory.INSURANCE_PREMIUM, Verdict.COMPLIANT),
        (100_001.0, MandateCategory.INSURANCE_PREMIUM, Verdict.NON_COMPLIANT),
    ],
)
def test_the_two_afa_ceilings_land_where_the_circular_puts_them(
    auditor, amount, category, expected
):
    """Clause 8(a) is Rs.15,000 inclusive; clause 8(b) lifts three named categories to
    Rs.1,00,000. A utility bill is not one of the three however much it resembles them."""
    assert auditor.audit_context(context(amount_inr=amount, category=category)).verdict is expected


def test_an_authenticated_debit_passes_at_any_amount(auditor):
    """Both ceilings are about AFA-free authorisation, not about a maximum debit."""
    verdict = auditor.audit_context(context(amount_inr=250_000.0, afa_on_this_transaction=True))
    assert verdict.verdict is Verdict.COMPLIANT


def test_a_notice_at_twenty_three_hours_fails_and_names_clause_six_a(auditor):
    """24 hours, not one calendar day."""
    verdict = auditor.audit_context(context(pre_debit_notice_hours=23.0))
    assert verdict.verdict is Verdict.NON_COMPLIANT
    assert verdict.citations == ["6(a)"]


def test_a_fastag_mandate_needs_no_pre_debit_notice_but_still_needs_the_post_one(auditor):
    """Clause 6(d) exempts FASTag and NCMC from the *pre*-transaction notification. Clause 7
    carries no such carve-out, and reading one into it would be inventing an exemption."""
    exempt = context(category=MandateCategory.FASTAG, pre_debit_notice_hours=None)
    assert auditor.audit_context(exempt).verdict is Verdict.COMPLIANT

    silent = exempt.model_copy(update={"post_transaction_notice_sent": False})
    verdict = auditor.audit_context(silent)
    assert verdict.verdict is Verdict.NON_COMPLIANT
    assert "7" in verdict.citations


def test_claiming_the_fastag_exemption_without_being_fastag_is_the_breach(auditor):
    """The one rule that runs backwards. Every other clause is an obligation and the breach
    is failing it; here the breach is claiming an exemption you do not hold -- which from
    inside a sending system looks exactly like "notice not required"."""
    verdict = auditor.audit_context(
        context(claims_notice_exemption=True, pre_debit_notice_hours=None)
    )
    assert verdict.verdict is Verdict.NON_COMPLIANT
    assert "6(d)" in verdict.citations


def test_a_one_click_opt_out_is_a_breach_not_a_courtesy(auditor):
    """Clause 6(c): "Any such opt-out shall be validated by the issuer using AFA." """
    verdict = auditor.audit_context(context(opt_out_afa_validated=False))
    assert verdict.verdict is Verdict.NON_COMPLIANT
    assert "6(c)" in verdict.citations


def test_a_missing_opt_out_fails_once_not_twice(auditor):
    """`opt_out_requires_afa` is guarded on `opt_out_offered` so that a mandate with no
    opt-out at all produces one finding. Two findings for one defect inflates every
    non-compliance count downstream."""
    verdict = auditor.audit_context(context(opt_out_offered=False, opt_out_afa_validated=False))
    assert verdict.citations.count("6(c)") == 1


def test_a_variable_mandate_debiting_above_the_customers_cap_fails(auditor):
    """The rule marked INFERENCE in the YAML -- clause 4(c) grants the facility and does not
    spell out that exceeding it is a breach. Tested so that the inference is visible in the
    suite rather than only in a comment."""
    verdict = auditor.audit_context(
        context(is_variable_amount=True, customer_cap_inr=500.0, amount_inr=900.0)
    )
    assert verdict.verdict is Verdict.NON_COMPLIANT
    assert "4(c)" in verdict.citations


def test_several_breaches_are_all_cited_not_just_the_first(auditor):
    verdict = auditor.audit_context(
        context(pre_debit_notice_hours=1.0, customer_charges_inr=5.0, opt_out_offered=False)
    )
    assert verdict.verdict is Verdict.NON_COMPLIANT
    assert {"6(a)", "10(a)", "6(c)"} <= set(verdict.citations)


def test_every_finding_carries_a_remedy(auditor):
    verdict = auditor.audit_context(context(pre_debit_notice_hours=1.0))
    failed = [o for o in verdict.outcomes if o.applied and o.passed is False]
    assert failed and all(o.remedy for o in failed)


def test_a_finding_without_a_citation_cannot_be_constructed():
    """`MandateVerdict` refuses it. An audit finding with no clause is an opinion."""
    from pydantic import ValidationError

    from mandateguard.models import MandateVerdict

    with pytest.raises(ValidationError, match="cites no clause"):
        MandateVerdict(mandate_id="x", verdict=Verdict.NON_COMPLIANT, reason="because")


# --------------------------------------------------------------------------------
# Which fields actually decide a verdict -- the input to the abstain rule.
# --------------------------------------------------------------------------------


def test_only_fields_that_an_applicable_rule_reads_can_block_a_verdict(auditor):
    """`customer_cap_inr` decides nothing for a fixed-amount mandate, so not knowing it is
    not a reason to abstain. Abstaining anyway is what drives abstain precision down."""
    needed = auditor.rules.fields_read_by_applicable_rules(context())
    assert "amount_inr" in needed
    assert "customer_cap_inr" not in needed

    variable = auditor.rules.fields_read_by_applicable_rules(
        context(is_variable_amount=True, customer_cap_inr=500.0)
    )
    assert "customer_cap_inr" in variable


# --------------------------------------------------------------------------------
# The extraction harness, through a scripted fake.
#
# What follows tests the HARNESS, not the model. It shows that each failure mode
# abstains rather than guesses. It says nothing about how often a real model hits them.
# --------------------------------------------------------------------------------


class ScriptedClient:
    """Returns a fixed response. Not a model, and not pretending to be one."""

    def __init__(self, text: str, *, stop_reason: str = "end_turn") -> None:
        self.text = text
        self.stop_reason = stop_reason
        self.calls: list[tuple[str, str]] = []

    def run(self, job, system, prompt, *, schema=None):
        self.calls.append((job, prompt))
        return LLMResult(
            job=job,
            key="scripted",
            model="scripted",
            text=self.text,
            stop_reason=self.stop_reason,
            usage=LLMUsage(input_tokens=0, output_tokens=0),
            latency_ms=0,
        )


def _extraction(**context_overrides) -> str:
    payload = {
        "context": {
            "mandate_id": "mg_1",
            "rail": "upi_autopay",
            "category": "general",
            "amount_inr": 499.0,
            "pre_debit_notice_hours": 36.0,
            "pre_debit_notice_fields": sorted(PRE_FIELDS),
            "post_transaction_notice_fields": sorted(POST_FIELDS),
        }
        | context_overrides.pop("context", {}),
        "unknown_fields": context_overrides.pop("unknown_fields", []),
        "notes": context_overrides.pop("notes", "a clean UPI AutoPay mandate"),
    }
    return json.dumps(payload)


def test_a_clean_extraction_reaches_the_rules_and_says_who_decided():
    client = ScriptedClient(_extraction())
    verdict = MandateAuditor(client=client).audit_record("mg_1", "UPI AutoPay, Rs 499/month")
    assert verdict.verdict is Verdict.COMPLIANT
    assert verdict.decided_by == "llm+rules"
    assert client.calls == [(JOB, "Mandate id: mg_1\n\nRecord:\nUPI AutoPay, Rs 499/month")]


def test_an_unknown_field_that_a_rule_reads_abstains():
    """The design's whole point. `afa_on_this_transaction` defaulting to False would fail a
    Rs.20,000 debit that was in fact authenticated -- a confident wrong verdict from a
    missing input."""
    client = ScriptedClient(
        _extraction(
            context={"amount_inr": 20_000.0},
            unknown_fields=["afa_on_this_transaction"],
            notes="the record does not say whether AFA ran",
        )
    )
    verdict = MandateAuditor(client=client).audit_record("mg_1", "...")
    assert verdict.verdict is Verdict.NEEDS_HUMAN
    assert "afa_on_this_transaction" in verdict.reason
    assert "does not say whether AFA ran" in verdict.reason


def test_an_unknown_field_no_rule_reads_does_not_block_the_verdict():
    client = ScriptedClient(_extraction(unknown_fields=["customer_cap_inr"]))
    assert MandateAuditor(client=client).audit_record("mg_1", "...").verdict is Verdict.COMPLIANT


def test_a_refusal_abstains_rather_than_being_parsed():
    client = ScriptedClient("I cannot help with that.", stop_reason="refusal")
    verdict = MandateAuditor(client=client).audit_record("mg_1", "...")
    assert verdict.verdict is Verdict.NEEDS_HUMAN
    assert "refused" in verdict.reason


def test_text_that_is_not_json_abstains():
    client = ScriptedClient("Here is the mandate: it looks fine to me.")
    verdict = MandateAuditor(client=client).audit_record("mg_1", "...")
    assert verdict.verdict is Verdict.NEEDS_HUMAN
    assert "not JSON" in verdict.reason


def test_a_payload_that_is_not_a_valid_context_abstains():
    client = ScriptedClient(json.dumps({"context": {"rail": "swift"}, "unknown_fields": []}))
    verdict = MandateAuditor(client=client).audit_record("mg_1", "...")
    assert verdict.verdict is Verdict.NEEDS_HUMAN
    assert "not a valid audit context" in verdict.reason


def test_the_extracted_mandate_id_cannot_override_the_one_asked_about():
    """A model that echoes the wrong id would file this verdict against another mandate."""
    client = ScriptedClient(_extraction(context={"mandate_id": "somebody_elses"}))
    verdict = MandateAuditor(client=client).audit_record("mg_1", "...")
    assert verdict.mandate_id == "mg_1"


def test_auditing_free_text_without_a_client_is_refused_not_guessed():
    with pytest.raises(RuntimeError, match="needs a model"):
        MandateAuditor().audit_record("mg_1", "...")


def test_the_extraction_schema_offers_every_context_field():
    """Generated from the model, so a new context field is offered on the next run rather
    than silently never extracted."""
    schema = extraction_schema()
    assert set(schema["properties"]["context"]["properties"]) == set(
        MandateAuditContext.model_fields
    )
    assert "unknown_fields" in schema["required"]


def test_the_rules_auditor_is_reusable_and_pins_one_policy_version():
    """The auditor holds a Policy rather than re-loading it 120 times, and every verdict it
    produces carries the same hash -- which is what makes T5.2's replay meaningful."""
    rules = RulesAuditor()
    first = rules.audit(context())
    second = rules.audit(context(mandate_id="mg_2"))
    assert first.policy_hash == second.policy_hash
    assert len(first.policy_hash) == 16
