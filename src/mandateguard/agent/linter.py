"""T4.4 -- the deterministic compliance linter. Plain Python. No model anywhere.

This is the gate that makes the notice composer (T4.3) shippable rather than impressive.
**No unvalidated model text ever reaches a regulated notice.** A composer that produces
good prose 95% of the time produces a regulatory breach one notice in twenty, at whatever
volume the merchant sends, and neither the model nor the person reading the demo will be
the one who notices.

So every generated notice is checked here first, against facts the system already knows,
and a notice that fails is regenerated rather than sent. Two failures escalate to a human.

### The three families of check, and why the third one exists

**Content (RBI clause 6(b)).** Five fields must be in the text: merchant name, amount,
date and time of debit, e-mandate reference, and reason. These are the clause's own list.

**Schedule (RBI clause 6(a)).** At least 24 hours between the notification and the debit.
This is checked against the *timestamps*, not against the text -- a notice that claims a
24-hour lead time it does not have is a worse failure than one that omits the claim, and
only the timestamps can tell the difference.

**Fabrication.** Every rupee amount that appears in the text must be the real one. This
check is not in any circular, and it is the one that matters most for LLM-composed text:
a notice that says Rs.599 when the debit is Rs.499 passes every content rule in clause
6(b) -- the amount *is* present -- and is a false statement about somebody's money. The
content checks ask "is it there"; this asks "is it true".

**Dark patterns (CCPA, 2023).** Six of the thirteen patterns the Central Consumer
Protection Authority prohibits, in `policy/dark_patterns.yaml` with their published
definitions. The phrase lists in that file are this project's detector, not the
regulator's, and the file says so.

### What this linter cannot do

It reads text. It cannot see a UI, so the visual form of interface interference -- a
greyed-out decline button next to a bold accept -- is invisible to it. It matches literal
phrases, so a dark pattern written in words nobody listed passes. And it checks amounts but
not dates for fabrication, because a date can be written a dozen ways and a half-working
date parser produces false failures, which in a regenerate-then-escalate loop means a queue
of humans reviewing correct notices. Recorded in `docs/limitations.md` rather than papered
over.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from mandateguard.policy.loader import POLICY_DIR

DARK_PATTERNS_PATH = POLICY_DIR / "dark_patterns.yaml"

MINIMUM_LEAD_HOURS = 24.0
"""RBI/DPSS/2026-27/396 clause 6(a). Not a tunable: it is a number in a circular."""

_CURRENCY = re.compile(
    r"(?:₹|Rs\.?|INR)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)",
    re.IGNORECASE,
)

__all__ = [
    "DARK_PATTERNS_PATH",
    "MINIMUM_LEAD_HOURS",
    "DarkPattern",
    "LintFinding",
    "LintReport",
    "NoticeFacts",
    "currency_amounts",
    "indian_grouping",
    "lint_notice",
    "load_dark_patterns",
]


def currency_amounts(text: str) -> list[tuple[str, str]]:
    """Every currency-marked amount in the text, as `(whole match, digits)`.

    Only currency-marked numbers -- `Rs.`, `INR`, or the rupee sign. Bare integers are left
    alone deliberately: "at least 24 hours", a mandate reference, a week number and a count
    of candidate asks are not claims about money, and a fabrication check that flagged them
    would be switched off within a day.

    Shared with `agent/explainer.py`, which runs the same check for the same reason: any
    text a model writes about somebody's money has to be checked back against the record.
    """
    return [(m.group(0), m.group(1)) for m in _CURRENCY.finditer(text)]


class NoticeFacts(BaseModel):
    """What is true, against which the notice's words are checked.

    Every field here comes from the mandate book, never from the model. That direction is
    the whole design: the composer is told these facts and its output is checked back
    against them, so a fabricated amount is caught by comparing text to record rather than
    by hoping the model was careful.
    """

    model_config = ConfigDict(frozen=True)

    mandate_id: str
    merchant_name: str = Field(min_length=1)
    amount_inr: Decimal = Field(gt=0)
    """Decimal, not float. `499.10` as a float is 499.10000000000002, and a linter that
    formats that into a string and looks for it in the text fails every correct notice."""
    notice_at: datetime
    debit_at: datetime
    mandate_reference: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    opt_out_url: str = Field(min_length=1)
    grievance_contact: str = Field(min_length=1)

    @model_validator(mode="after")
    def _the_debit_comes_after_the_notice(self) -> NoticeFacts:
        if self.debit_at <= self.notice_at:
            raise ValueError(
                f"debit_at ({self.debit_at}) is not after notice_at ({self.notice_at}). A "
                "notice sent after its own debit is not a late notice, it is a receipt, and "
                "the lead-time check would report a negative number as if it were a lead."
            )
        return self

    @property
    def lead_hours(self) -> float:
        return (self.debit_at - self.notice_at).total_seconds() / 3600.0

    def amount_text(self) -> str:
        """The amount as it should appear, in Indian digit grouping.

        `1,00,000` rather than `100,000`: the notice is read in India, and the linter has to
        look for the string a correct notice would actually contain.
        """
        return indian_grouping(self.amount_inr)


class DarkPattern(BaseModel):
    """One CCPA-prohibited pattern, plus this project's phrase detector for it."""

    model_config = ConfigDict(frozen=True)

    id: str
    ccpa_pattern: str
    definition: str
    why_here: str
    phrases: list[str] = Field(min_length=1)


class LintFinding(BaseModel):
    """One reason a notice must not be sent.

    `authority` names what the check enforces, so a failure can be argued with rather than
    merely obeyed. A linter that says "failed check 7" is a linter people disable.
    """

    model_config = ConfigDict(frozen=True)

    check_id: str
    authority: str
    detail: str


class LintReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    findings: list[LintFinding] = Field(default_factory=list)
    checks_run: int = Field(ge=1)

    @property
    def passed(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        if self.passed:
            return f"passed all {self.checks_run} checks"
        return "; ".join(f"{f.check_id} ({f.authority}): {f.detail}" for f in self.findings)


def load_dark_patterns(path=DARK_PATTERNS_PATH) -> list[DarkPattern]:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [DarkPattern.model_validate(entry) for entry in raw["patterns"]]


def lint_notice(
    text: str, facts: NoticeFacts, *, patterns: list[DarkPattern] | None = None
) -> LintReport:
    """Check one notice. Returns every failure, not the first one.

    All of them, deliberately: the composer is handed the findings to regenerate against,
    and a linter that reports one failure per round turns a two-round budget into a
    guaranteed escalation for a notice with two easy defects.
    """
    patterns = load_dark_patterns() if patterns is None else patterns
    haystack = _normalise(text)
    findings: list[LintFinding] = []
    checks = 0

    # --- Content: RBI clause 6(b)'s own list of five. ---
    required = [
        ("merchant_name_present", facts.merchant_name, "the merchant's name"),
        ("mandate_reference_present", facts.mandate_reference, "the e-mandate reference"),
        ("reason_present", facts.reason, "the reason for the debit"),
    ]
    for check_id, value, label in required:
        checks += 1
        if _normalise(value) not in haystack:
            findings.append(
                LintFinding(
                    check_id=check_id,
                    authority="RBI/DPSS/2026-27/396 clause 6(b)",
                    detail=f"the notice does not state {label} ({value!r}).",
                )
            )

    checks += 1
    if not _states_amount(haystack, facts):
        findings.append(
            LintFinding(
                check_id="amount_present",
                authority="RBI/DPSS/2026-27/396 clause 6(b)",
                detail=f"the notice does not state the amount ({facts.amount_text()}).",
            )
        )

    checks += 1
    if not _states_debit_datetime(haystack, facts):
        findings.append(
            LintFinding(
                check_id="debit_datetime_present",
                authority="RBI/DPSS/2026-27/396 clause 6(b)",
                detail=(
                    "the notice does not state the date and time of the debit "
                    f"({facts.debit_at:%d %B %Y} at {facts.debit_at:%H:%M})."
                ),
            )
        )

    # --- The opt-out: RBI clause 6(c). ---
    checks += 1
    if _normalise(facts.opt_out_url) not in haystack:
        findings.append(
            LintFinding(
                check_id="opt_out_path_present",
                authority="RBI/DPSS/2026-27/396 clause 6(c)",
                detail=(
                    f"the notice does not carry the opt-out path ({facts.opt_out_url}). The "
                    "customer must be able to decline this debit or the mandate itself."
                ),
            )
        )

    # --- The schedule: RBI clause 6(a), checked against the clock, not the prose. ---
    checks += 1
    if facts.lead_hours < MINIMUM_LEAD_HOURS:
        findings.append(
            LintFinding(
                check_id="lead_time_at_least_24h",
                authority="RBI/DPSS/2026-27/396 clause 6(a)",
                detail=(
                    f"only {facts.lead_hours:.1f} hours between the notice and the debit; "
                    f"clause 6(a) requires at least {MINIMUM_LEAD_HOURS:.0f}. No wording "
                    "fixes this -- the notice or the debit has to move."
                ),
            )
        )

    # --- Fabrication: not in any circular, and the one that matters for generated text. ---
    checks += 1
    for wrong in _foreign_amounts(text, facts):
        findings.append(
            LintFinding(
                check_id="no_invented_amounts",
                authority="this project (see agent/linter.py)",
                detail=(
                    f"the notice states an amount of {wrong} that is not this debit's "
                    f"{facts.amount_text()}. The content rules are satisfied by any amount "
                    "being present; only this check asks whether it is the right one."
                ),
            )
        )

    # --- Dark patterns: CCPA Guidelines, 2023. ---
    for pattern in patterns:
        checks += 1
        hit = next((p for p in pattern.phrases if _normalise(p) in haystack), None)
        if hit is not None:
            findings.append(
                LintFinding(
                    check_id=f"no_{pattern.id}",
                    authority=f"CCPA Dark Patterns Guidelines, 2023 -- {pattern.ccpa_pattern}",
                    detail=f"the notice contains {hit!r}. {pattern.definition}",
                )
            )

    return LintReport(findings=findings, checks_run=checks)


# --------------------------------------------------------------------------------
# Helpers.
# --------------------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Lower-case with whitespace collapsed. Nothing else.

    Punctuation is left alone: "no thanks, i" and "no thanks i" are different strings and
    the phrase list is written to match what a composer actually writes.
    """
    return " ".join(text.split()).lower()


def indian_grouping(amount: Decimal) -> str:
    """1234567.5 -> '12,34,567.50'. Last three digits, then pairs."""
    quantised = amount.quantize(Decimal("0.01"))
    sign = "-" if quantised < 0 else ""
    whole, _, frac = f"{abs(quantised):.2f}".partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        parts = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        whole = ",".join([*parts, tail])
    return f"{sign}{whole}.{frac}"


def _amount_spellings(facts: NoticeFacts) -> set[str]:
    """Every way a correct notice might write this amount.

    Four spellings rather than one, because a notice that writes `Rs.499` for a `499.00`
    debit is correct and a linter that demands the paise is a linter that fails correct
    notices -- which, in a regenerate-then-escalate loop, is a queue of humans reviewing
    notices that were fine.
    """
    quantised = facts.amount_inr.quantize(Decimal("0.01"))
    grouped = indian_grouping(quantised)
    plain = f"{quantised:.2f}"
    spellings = {grouped, plain}
    if quantised == quantised.to_integral_value():
        whole = str(int(quantised))
        spellings |= {whole, grouped.rsplit(".", 1)[0]}
    return {_normalise(s) for s in spellings}


def _states_amount(haystack: str, facts: NoticeFacts) -> bool:
    return any(spelling in haystack for spelling in _amount_spellings(facts))


def _states_debit_datetime(haystack: str, facts: NoticeFacts) -> bool:
    """The date in any of three common written forms, and the time of day.

    Both halves are required by clause 6(b) ("date / time of debit"). The time is matched
    as 24-hour `HH:MM`, which is what the template writes; a composer using "8:30 PM" would
    fail this check, and that is the intended trade -- one written form the whole system
    agrees on beats a parser guessing between `05/06` and `06/05`.
    """
    date_forms = {
        f"{facts.debit_at:%d %B %Y}",
        f"{facts.debit_at:%d %b %Y}",
        f"{facts.debit_at:%Y-%m-%d}",
    }
    has_date = any(_normalise(form) in haystack for form in date_forms)
    has_time = _normalise(f"{facts.debit_at:%H:%M}") in haystack
    return has_date and has_time


def _foreign_amounts(text: str, facts: NoticeFacts) -> list[str]:
    """Currency amounts in the text that are not this debit's amount.

    Only currency-marked numbers are examined -- `Rs.`, `INR`, or the rupee sign. Bare
    integers are left alone on purpose: "at least 24 hours" and a mandate reference full of
    digits are not claims about money, and flagging them would make this check useless
    within a day.
    """
    correct = _amount_spellings(facts)
    wrong: list[str] = []
    for whole, digits in currency_amounts(text):
        written = _normalise(digits)
        if written in correct:
            continue
        try:
            value = Decimal(written.replace(",", ""))
        except (ArithmeticError, ValueError):
            wrong.append(whole)
            continue
        if value != facts.amount_inr.quantize(Decimal("0.01")):
            wrong.append(whole)
    return wrong
