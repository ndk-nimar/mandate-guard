"""Job 3 of four: the pre-transaction notice, with a re-consent ask riding on it (T4.3).

### Whose notice this is

Clause 6(a) reads "**An issuer** shall send a pre-transaction notification", and clause 3(a)
takes 'issuer' from the 2025 authentication directions -- the card, PPI or account issuer.
This project is merchant-side. It does not discharge clause 6 and must not claim to.

So what this module composes is a notice **for an issuer or payment aggregator to send**,
and the re-consent ask attached to it is a commercial arrangement with that party rather
than a regulatory entitlement. `docs/limitations.md` §8.2 states the sentence this project
is therefore not allowed to say. The composer still checks the notice against clause 6's
content rules, because a notice that fails them is useless to the party who *does* have the
obligation.

### Why the ask can ride on the notice at all

The pre-debit notice is being sent regardless -- it is mandatory, it is already in the
customer's hand, and it costs the merchant nothing extra (`docs/problem.md` §7). That makes
it the cheapest rung on the channel ladder by a wide margin, and it is the reason a
re-consent ask has any economics at all. What it must not become is a second, louder ask
wearing a regulatory notice as a disguise: the notice's own job comes first, and
`agent/linter.py` fails a notice whose required content is missing no matter how good the
ask reads.

### The generate-check-regenerate loop, and why it stops at two

    compose -> lint -> pass  -> send
                    -> fail  -> compose again, told what failed
                             -> lint -> pass -> send
                                     -> fail -> ESCALATE, send nothing

Two attempts, then a human. Not three, and not "retry until it passes": a model that fails
the same check twice is failing it for a reason the prompt has not fixed, and a loop that
keeps going until the linter is satisfied is a loop optimising text against a checker rather
than writing a correct notice. The deterministic template is offered alongside the
escalation as the safe alternative -- it is checked by the same linter, and a test asserts
it passes.

`TemplateComposer` alone needs no model at all. That is the mode CI runs, and it is T5.3's
"LLM down leads to rules-only" rung: the notice still goes out, it is just not as well
written.
"""

from __future__ import annotations

import hashlib
import json
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from mandateguard.agent.client import LLMClient
from mandateguard.agent.linter import LintReport, NoticeFacts, lint_notice, load_dark_patterns

JOB = "notice_composer"
MAX_ATTEMPTS = 2

__all__ = ["JOB", "MAX_ATTEMPTS", "ComposedNotice", "LLMComposer", "TemplateComposer"]


class ComposedNotice(BaseModel):
    """A notice and the evidence that it may be sent.

    `text` is never returned unlinted, and `escalated` is not a warning flag -- when it is
    true the caller must not send `text`. The field exists so the refusal is a value the
    ledger can record (T5.1) rather than an exception the caller may catch and ignore.
    """

    model_config = ConfigDict(frozen=True)

    mandate_id: str
    text: str
    source: str = Field(description="'template', 'llm' or 'llm_retry'")
    lint: LintReport
    attempts: int = Field(ge=0)
    escalated: bool = False
    escalation_reason: str = ""
    fallback_text: str = Field(
        default="", description="the deterministic notice, when the model's was rejected"
    )
    template_id: str = Field(description="content hash; feeds value/fatigue.py's reuse penalty")

    @property
    def sendable(self) -> bool:
        return self.lint.passed and not self.escalated


def _template_id(text: str) -> str:
    """A content hash, so "the same template twice" means the same words twice.

    `value/fatigue.py` charges `rho_template_reuse` when a customer gets the same wording
    again. Keying that on a template *name* would let a composer rewrite every sentence,
    keep the name, and dodge a penalty that exists because the customer noticed the
    repetition -- and the customer is reading the words, not the name.
    """
    return "tmpl_" + hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()[:12]


class TemplateComposer:
    """The deterministic notice. No model, no network, byte-identical across runs.

    Deliberately plain. It is what goes out when the model is unavailable or has failed
    twice, so its job is to be correct rather than warm, and every sentence in it exists
    because a clause or a check asks for it.
    """

    def compose(self, facts: NoticeFacts, *, cta_url: str | None = None) -> ComposedNotice:
        lines = [
            "Pre-transaction notification",
            "",
            (
                f"{facts.merchant_name} will debit Rs.{facts.amount_text()} from your account "
                f"on {facts.debit_at:%d %B %Y} at {facts.debit_at:%H:%M}."
            ),
            "",
            f"E-mandate reference: {facts.mandate_reference}",
            f"Reason for debit: {facts.reason}",
            "",
            (
                f"To stop this debit, or the e-mandate itself, go to {facts.opt_out_url}. "
                "Your bank will confirm the request with an additional factor of "
                "authentication and send you an intimation."
            ),
            "",
            f"Questions or complaints: {facts.grievance_contact}",
        ]
        if cta_url:
            lines += [
                "",
                (
                    "This mandate is nearing the end of its validity period. If you want it "
                    f"to continue, you can renew it at {cta_url}. If you do nothing, it will "
                    "simply end on its expiry date."
                ),
            ]
        text = "\n".join(lines)
        report = lint_notice(text, facts)
        return ComposedNotice(
            mandate_id=facts.mandate_id,
            text=text,
            source="template",
            lint=report,
            attempts=0,
            template_id=_template_id(text),
        )


SYSTEM = """\
You write pre-transaction notifications for Indian recurring mandates, for a bank or payment \
aggregator to send to its customer. The notice is a regulatory obligation under RBI's \
Digital Payments E-mandate Framework, 2026, clause 6. It is not marketing.

Every notice you write is checked by a deterministic linter before it can be sent. The \
linter has the true facts and compares your text to them, so there is nothing to gain from \
writing anything you were not given.

MUST contain, in your own sentences:
* the merchant's name, exactly as given
* the amount, exactly as given -- and NO other rupee amount anywhere in the text
* the date of the debit as "DD Month YYYY" and the time as 24-hour HH:MM
* the e-mandate reference, exactly as given
* the reason for the debit
* the opt-out URL, exactly as given, described as stopping this debit or the mandate itself
* the grievance contact, exactly as given

MUST NOT contain, because India's Central Consumer Protection Authority prohibits these \
(Guidelines for Prevention and Regulation of Dark Patterns, 2023):
* urgency beyond the debit date you were given -- no "act now", no "last chance". The \
deadline is real and stating it plainly is enough.
* a guilt-loaded way to decline. The customer declining is a normal outcome, not a failure.
* anything that makes stopping the mandate sound harder than starting it was.
* any promise to keep contacting them.
* double negatives around the opt-out.

Tone: plain, short, factual. A person should be able to read it in fifteen seconds and know \
exactly what will be taken, when, and how to stop it. Write the notice only -- no preamble, \
no subject line, no sign-off block, no markdown.
"""

RETRY_NOTE = """\

Your previous attempt was rejected by the linter. Fix exactly these and change nothing else:

{failures}

Previous attempt:
{previous}
"""


class LLMComposer:
    """Model-written prose, gated by the linter, with a deterministic fallback."""

    def __init__(self, client: LLMClient, *, template: TemplateComposer | None = None) -> None:
        self.client = client
        self.template = template or TemplateComposer()
        self.patterns = load_dark_patterns()

    def compose(self, facts: NoticeFacts, *, cta_url: str | None = None) -> ComposedNotice:
        prompt = _facts_prompt(facts, cta_url)
        previous = ""
        report: LintReport | None = None

        for attempt in range(1, MAX_ATTEMPTS + 1):
            message = prompt
            if report is not None:
                message += RETRY_NOTE.format(
                    failures="\n".join(f"* {f.check_id}: {f.detail}" for f in report.findings),
                    previous=previous,
                )
            result = self.client.run(JOB, SYSTEM, message)
            if result.refused:
                return self._escalate(
                    facts,
                    attempt,
                    f"the composer job was refused (stop_reason={result.stop_reason!r}).",
                )

            previous = result.text.strip()
            report = lint_notice(previous, facts, patterns=self.patterns)
            if report.passed:
                return ComposedNotice(
                    mandate_id=facts.mandate_id,
                    text=previous,
                    source="llm" if attempt == 1 else "llm_retry",
                    lint=report,
                    attempts=attempt,
                    template_id=_template_id(previous),
                )

        assert report is not None  # the loop runs at least once
        return self._escalate(
            facts,
            MAX_ATTEMPTS,
            f"failed the linter on both attempts: {report.summary()}",
            report=report,
            rejected=previous,
        )

    def _escalate(
        self,
        facts: NoticeFacts,
        attempts: int,
        reason: str,
        *,
        report: LintReport | None = None,
        rejected: str = "",
    ) -> ComposedNotice:
        """Stop, and hand back the safe notice clearly marked as not-yet-sent.

        `text` carries the rejected draft rather than the fallback, so a reviewer sees what
        the model actually wrote; `fallback_text` carries the deterministic one. Putting the
        fallback in `text` would let a caller that ignores `escalated` send something
        reasonable, which sounds forgiving and would hide every escalation this loop ever
        raises.
        """
        safe = self.template.compose(facts)
        return ComposedNotice(
            mandate_id=facts.mandate_id,
            text=rejected,
            source="llm_retry" if attempts > 1 else "llm",
            lint=report or LintReport(findings=[], checks_run=1),
            attempts=attempts,
            escalated=True,
            escalation_reason=reason,
            fallback_text=safe.text,
            template_id=_template_id(rejected) if rejected else safe.template_id,
        )


def _facts_prompt(facts: NoticeFacts, cta_url: str | None) -> str:
    """The facts, as JSON, so the prompt has one obvious reading.

    `sort_keys=True` for the same reason `cassette_key` sorts: a dict rendered in a
    different order is a different prompt and therefore a different cassette.
    """
    payload = {
        "merchant_name": facts.merchant_name,
        "amount_inr": facts.amount_text(),
        "debit_date": f"{facts.debit_at:%d %B %Y}",
        "debit_time_24h": f"{facts.debit_at:%H:%M}",
        "mandate_reference": facts.mandate_reference,
        "reason_for_debit": facts.reason,
        "opt_out_url": facts.opt_out_url,
        "grievance_contact": facts.grievance_contact,
    }
    if cta_url:
        payload["renewal_url"] = cta_url
    lines = [
        "Write the pre-transaction notification for this debit.",
        "",
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
    ]
    if cta_url:
        lines += [
            "",
            "This mandate is close to the end of its validity period. Add ONE short "
            "sentence offering renewal at renewal_url, and say plainly that doing nothing "
            "lets the mandate end. Do not press, and do not make the notice about the "
            "renewal -- the notification is the obligation and the offer is a courtesy.",
        ]
    return "\n".join(lines)


def facts_from_amount(
    mandate_id: str,
    merchant_name: str,
    amount_inr: float | str | Decimal,
    **kwargs,
) -> NoticeFacts:
    """Build `NoticeFacts` with the amount coerced through `str` into `Decimal`.

    `Decimal(499.10)` is 499.100000000000022737367544323205947875976562500, and every string
    the linter builds from it would then miss. `Decimal(str(499.10))` is 499.10.
    """
    return NoticeFacts(
        mandate_id=mandate_id,
        merchant_name=merchant_name,
        amount_inr=Decimal(str(amount_inr)),
        **kwargs,
    )
