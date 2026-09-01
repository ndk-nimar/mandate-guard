"""T4.3/T4.4 -- the notice composer and the linter that decides whether it may be sent.

T4.4's stated bar is "the linter has its own unit tests, including deliberately bad
notices". Most of this file is deliberately bad notices, because a linter is only worth
what it *rejects* -- a suite of good notices passing proves the checks run, not that they
catch anything.

Every rejection test breaks exactly one thing against a notice that otherwise passes, so a
failure here names the check that broke rather than the fixture.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from mandateguard.agent.client import LLMResult, LLMUsage
from mandateguard.agent.linter import (
    MINIMUM_LEAD_HOURS,
    NoticeFacts,
    lint_notice,
    load_dark_patterns,
)
from mandateguard.agent.notice import MAX_ATTEMPTS, LLMComposer, TemplateComposer


def facts(**overrides) -> NoticeFacts:
    base = {
        "mandate_id": "mg_1",
        "merchant_name": "Hoichoi",
        "amount_inr": Decimal("499.00"),
        "notice_at": datetime(2026, 9, 1, 9, 0),
        "debit_at": datetime(2026, 9, 3, 20, 30),
        "mandate_reference": "MND-8842-XR",
        "reason": "Monthly subscription under your e-mandate",
        "opt_out_url": "https://bank.example/mandates/MND-8842-XR/stop",
        "grievance_contact": "grievance@bank.example / 1800-000-000",
    }
    return NoticeFacts(**(base | overrides))


@pytest.fixture(scope="module")
def patterns():
    return load_dark_patterns()


def good_notice(f: NoticeFacts) -> str:
    """A notice that passes every check, so each test below can break one thing."""
    return TemplateComposer().compose(f).text


# --------------------------------------------------------------------------------
# The baseline, and the fallback's own guarantee.
# --------------------------------------------------------------------------------


def test_the_deterministic_template_passes_its_own_linter(patterns):
    """Load-bearing. The template is what goes out when the model is unavailable or has
    failed twice, so a template that could not pass the linter would make the escalation
    path offer something unsendable."""
    report = lint_notice(good_notice(facts()), facts(), patterns=patterns)
    assert report.passed, report.summary()
    assert report.checks_run >= 12


def test_the_template_passes_with_the_re_consent_ask_attached(patterns):
    """The ask rides on the notice; it must not cost the notice its compliance."""
    composed = TemplateComposer().compose(facts(), cta_url="https://bank.example/renew")
    assert composed.lint.passed, composed.lint.summary()
    assert composed.sendable
    assert "renew" in composed.text


def test_the_template_is_byte_identical_across_runs():
    """ADR 0003 reaches here too: the fallback notice is a derived artefact."""
    first = TemplateComposer().compose(facts())
    second = TemplateComposer().compose(facts())
    assert first.text == second.text
    assert first.template_id == second.template_id


def test_the_template_id_is_a_content_hash_not_a_name():
    """`value/fatigue.py` charges rho_template_reuse when a customer gets the same wording
    twice. Keyed on a name, a composer could rewrite every sentence, keep the name, and
    dodge a penalty that exists because the customer noticed the repetition."""
    plain = TemplateComposer().compose(facts())
    with_ask = TemplateComposer().compose(facts(), cta_url="https://bank.example/renew")
    assert plain.template_id != with_ask.template_id


# --------------------------------------------------------------------------------
# Content: RBI clause 6(b)'s five fields, one deletion at a time.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("removed", "check_id"),
    [
        ("Hoichoi", "merchant_name_present"),
        ("MND-8842-XR", "mandate_reference_present"),
        ("Monthly subscription under your e-mandate", "reason_present"),
        ("499.00", "amount_present"),
        ("03 September 2026", "debit_datetime_present"),
        ("20:30", "debit_datetime_present"),
    ],
)
def test_a_notice_missing_a_required_field_is_rejected(patterns, removed, check_id):
    f = facts()
    broken = good_notice(f).replace(removed, "")
    report = lint_notice(broken, f, patterns=patterns)
    assert not report.passed
    assert check_id in {finding.check_id for finding in report.findings}


def test_a_notice_with_no_opt_out_path_is_rejected(patterns):
    """Clause 6(c). The customer must be able to decline this debit or the mandate."""
    f = facts()
    broken = good_notice(f).replace(f.opt_out_url, "your account settings")
    report = lint_notice(broken, f, patterns=patterns)
    assert "opt_out_path_present" in {finding.check_id for finding in report.findings}


def test_every_finding_names_the_authority_it_enforces(patterns):
    """A linter that says "failed check 7" is a linter people switch off."""
    f = facts()
    report = lint_notice("Your payment is due.", f, patterns=patterns)
    assert not report.passed
    assert all(finding.authority for finding in report.findings)
    assert any("clause 6(b)" in finding.authority for finding in report.findings)


def test_the_linter_reports_every_failure_not_just_the_first(patterns):
    """The composer regenerates against the findings. One failure per round turns a
    two-round budget into a guaranteed escalation for a notice with two easy defects."""
    report = lint_notice("Nothing useful here.", facts(), patterns=patterns)
    assert len(report.findings) >= 4


# --------------------------------------------------------------------------------
# Schedule: clause 6(a), checked against the clock rather than the prose.
# --------------------------------------------------------------------------------


def test_a_notice_sent_less_than_24_hours_ahead_fails_however_it_is_worded(patterns):
    """The check the wording cannot fix. A notice that *claims* 24 hours it does not have
    is a worse failure than one that omits the claim, and only the timestamps know."""
    late = facts(notice_at=datetime(2026, 9, 3, 9, 0))
    assert late.lead_hours < MINIMUM_LEAD_HOURS
    report = lint_notice(good_notice(late), late, patterns=patterns)
    assert "lead_time_at_least_24h" in {f.check_id for f in report.findings}
    assert "the notice or the debit has to move" in report.summary()


def test_exactly_24_hours_passes():
    """ "at least 24 hours" is inclusive, like clause 8(a)'s Rs.15,000."""
    exact = facts(notice_at=datetime(2026, 9, 2, 20, 30))
    assert exact.lead_hours == pytest.approx(24.0)
    assert lint_notice(good_notice(exact), exact).passed


def test_a_notice_dated_after_its_own_debit_cannot_be_constructed():
    """Otherwise the lead-time check reports a negative number as though it were a lead."""
    with pytest.raises(ValidationError, match="not after"):
        facts(notice_at=datetime(2026, 9, 4, 9, 0))


# --------------------------------------------------------------------------------
# Fabrication. The check that is in no circular and matters most for generated text.
# --------------------------------------------------------------------------------


def test_a_notice_stating_the_wrong_amount_is_rejected(patterns):
    """Rs.599 for a Rs.499 debit satisfies clause 6(b) -- an amount *is* present -- and is
    a false statement about somebody's money. Only this check asks whether it is true."""
    f = facts()
    wrong = good_notice(f).replace("Rs.499.00", "Rs.599.00")
    report = lint_notice(wrong, f, patterns=patterns)
    ids = {finding.check_id for finding in report.findings}
    assert "no_invented_amounts" in ids


def test_a_second_invented_amount_beside_the_right_one_is_still_caught(patterns):
    f = facts()
    padded = good_notice(f) + "\n\nYour next debit after this will be Rs.1,299.00."
    report = lint_notice(padded, f, patterns=patterns)
    assert "no_invented_amounts" in {finding.check_id for finding in report.findings}


def test_bare_numbers_are_not_treated_as_money(patterns):
    """ "at least 24 hours" and a reference full of digits are not claims about money.
    Flagging them would make this check useless within a day."""
    f = facts()
    padded = good_notice(f) + "\n\nWe notify you at least 24 hours ahead. Ticket 90210."
    assert lint_notice(padded, f, patterns=patterns).passed


@pytest.mark.parametrize("written", ["Rs.499", "Rs. 499.00", "INR 499.00", "₹499"])
def test_the_amount_may_be_written_any_of_the_usual_ways(patterns, written):
    """A linter that demands the paise fails correct notices, and in a
    regenerate-then-escalate loop that is a queue of humans reviewing notices that were
    fine."""
    f = facts()
    text = good_notice(f).replace("Rs.499.00", written)
    assert lint_notice(text, f, patterns=patterns).passed, written


def test_indian_digit_grouping_is_what_the_linter_looks_for(patterns):
    """Rs.1,00,000 -- not Rs.100,000. The notice is read in India."""
    f = facts(amount_inr=Decimal("100000.00"))
    assert f.amount_text() == "1,00,000.00"
    assert lint_notice(good_notice(f), f, patterns=patterns).passed


def test_a_paise_amount_survives_the_float_trap():
    """`Decimal(499.10)` is 499.1000000000000227..., and every string built from it would
    miss. The model uses Decimal for exactly this."""
    f = facts(amount_inr=Decimal("499.10"))
    assert f.amount_text() == "499.10"
    assert lint_notice(good_notice(f), f).passed


# --------------------------------------------------------------------------------
# Dark patterns: CCPA Guidelines, 2023. Six of the thirteen.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("addition", "check_id"),
    [
        ("No thanks, I don't want to save my subscription.", "no_confirm_shaming"),
        ("Act now -- last chance to keep your plan!", "no_false_urgency"),
        ("Call our support team to cancel this mandate.", "no_subscription_trap"),
        ("We will keep reminding you until you respond.", "no_nagging"),
        ("Uncheck this box if you do not want to stop the debit.", "no_trick_question"),
        ("In the unlikely event that you wish to stop, see below.", "no_interface_interference"),
    ],
)
def test_a_dark_pattern_is_rejected_even_when_every_rbi_field_is_present(
    patterns, addition, check_id
):
    """The gap this closes. Each of these notices satisfies every content rule in clause
    6(b) and is a prohibited practice under the Consumer Protection Act, 2019."""
    f = facts()
    report = lint_notice(good_notice(f) + "\n\n" + addition, f, patterns=patterns)
    assert not report.passed
    assert check_id in {finding.check_id for finding in report.findings}


def test_a_dark_pattern_finding_carries_the_regulators_own_definition(patterns):
    """So that a rejection can be argued with rather than merely obeyed."""
    f = facts()
    report = lint_notice(good_notice(f) + "\n\nHurry, act now!", f, patterns=patterns)
    finding = next(x for x in report.findings if x.check_id == "no_false_urgency")
    assert "CCPA" in finding.authority
    assert "false sense of urgency" in finding.detail


def test_the_dark_pattern_file_covers_six_named_ccpa_patterns(patterns):
    """Six of thirteen, and the file says why the other seven are not checkable in text."""
    assert len(patterns) == 6
    assert {p.ccpa_pattern for p in patterns} == {
        "Confirm shaming",
        "False urgency",
        "Subscription trap",
        "Nagging",
        "Trick question",
        "Interface interference",
    }
    assert all(p.phrases and p.definition and p.why_here for p in patterns)


# --------------------------------------------------------------------------------
# The generate-check-regenerate loop.
# --------------------------------------------------------------------------------


class ScriptedClient:
    """Returns queued responses in order. Not a model, and not pretending to be one.

    These tests prove the *loop* regenerates once and escalates on the second failure.
    They prove nothing about how often a real model writes a bad notice -- that needs
    recordings, and there are none (docs/limitations.md §8.5).
    """

    def __init__(self, *texts: str, stop_reason: str = "end_turn") -> None:
        self.texts = list(texts)
        self.stop_reason = stop_reason
        self.prompts: list[str] = []

    def run(self, job, system, prompt, *, schema=None):
        self.prompts.append(prompt)
        text = self.texts.pop(0) if self.texts else ""
        return LLMResult(
            job=job,
            key="scripted",
            model="scripted",
            text=text,
            stop_reason=self.stop_reason,
            usage=LLMUsage(input_tokens=0, output_tokens=0),
            latency_ms=0,
        )


def test_a_clean_first_draft_is_sent_without_a_second_call():
    f = facts()
    client = ScriptedClient(good_notice(f))
    composed = LLMComposer(client).compose(f)
    assert composed.sendable
    assert composed.source == "llm"
    assert composed.attempts == 1
    assert len(client.prompts) == 1


def test_a_failed_draft_is_regenerated_and_the_failures_are_handed_back():
    f = facts()
    client = ScriptedClient(good_notice(f) + "\n\nHurry, act now!", good_notice(f))
    composed = LLMComposer(client).compose(f)
    assert composed.sendable
    assert composed.source == "llm_retry"
    assert composed.attempts == 2
    assert "no_false_urgency" in client.prompts[1]
    assert "Your previous attempt was rejected" in client.prompts[1]


def test_two_failures_escalate_and_the_rejected_text_is_not_sendable():
    f = facts()
    client = ScriptedClient("Hurry, act now!", "Last chance, act now!")
    composed = LLMComposer(client).compose(f)
    assert composed.escalated
    assert not composed.sendable
    assert composed.attempts == MAX_ATTEMPTS
    assert "failed the linter on both attempts" in composed.escalation_reason


def test_the_escalation_keeps_the_rejected_draft_in_text_and_the_safe_one_beside_it():
    """`text` is what the model wrote, so a reviewer sees the actual failure. Putting the
    fallback in `text` would let a caller that ignores `escalated` send something
    reasonable -- which hides every escalation this loop will ever raise."""
    f = facts()
    composed = LLMComposer(ScriptedClient("act now", "act now")).compose(f)
    assert composed.text == "act now"
    assert composed.fallback_text.startswith("Pre-transaction notification")
    assert lint_notice(composed.fallback_text, f).passed


def test_a_refusal_escalates_immediately_without_a_retry():
    f = facts()
    client = ScriptedClient("I cannot help with that.", stop_reason="refusal")
    composed = LLMComposer(client).compose(f)
    assert composed.escalated
    assert composed.attempts == 1
    assert "refused" in composed.escalation_reason
    assert len(client.prompts) == 1


def test_the_loop_never_runs_more_than_twice():
    """Not three attempts, and not "retry until it passes": a loop that keeps going until
    the linter is satisfied optimises text against a checker rather than writing a correct
    notice."""
    f = facts()
    client = ScriptedClient(*["act now"] * 10)
    LLMComposer(client).compose(f)
    assert len(client.prompts) == MAX_ATTEMPTS


def test_an_invented_amount_from_the_model_is_caught_by_the_loop():
    """The failure mode this whole gate exists for, driven end to end."""
    f = facts()
    client = ScriptedClient(good_notice(f).replace("Rs.499.00", "Rs.4,990.00"), good_notice(f))
    composed = LLMComposer(client).compose(f)
    assert composed.sendable
    assert "no_invented_amounts" in client.prompts[1]
