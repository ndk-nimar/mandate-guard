"""T4.6 entry point: build the golden set of mandate edge cases.

    uv run python scripts/build_golden.py            # rewrite tests/golden/mandates.jsonl
    uv run python scripts/build_golden.py --check    # fail if the committed file is stale

**The expected verdicts in this file are written from the circular, not read off the
auditor.** That distinction is the whole value of a golden set. Generating expectations by
running the system under test produces a regression baseline -- it will tell you when
behaviour changes, and it can never tell you that the behaviour was wrong to begin with,
because it was defined as correct. Every `expect=` below was decided by reading the clause
named in `why`, and where the auditor disagrees, the disagreement is a finding: either the
compiled rule is wrong or the expectation is, and the clause settles it.

That is not a hypothetical. Writing these cases is what surfaced the FASTag/clause-7
interaction: clause 6(d)'s carve-out is written against *pre*-transaction notification
only, so a FASTag mandate needs no notice before the debit and still needs one after it.
The expectation was written that way first, from the text, and the rules agreed.

The file is JSONL, one case per line, sorted by `case_id` so that a diff shows what changed
rather than what moved (ADR 0003).

### Two kinds of case

* **`requires: "rules"`** -- a structured `context`, judged by the deterministic engine.
  These need no model and are the ones CI scores.
* **`requires: "extraction"`** -- a free-text `record` that has to be read before it can be
  judged, with the expected verdict being an abstention because the record genuinely does
  not determine a field some rule reads. These need a model, no cassette exists yet
  (`docs/limitations.md` §8.5), and T4.7 reports them as unscored rather than skipping them
  quietly.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from mandateguard.models import MandateAuditContext, MandateCategory, Rail, Verdict

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "tests" / "golden" / "mandates.jsonl"

PRE_FIELDS = ["merchant_name", "amount", "debit_datetime", "mandate_reference", "reason"]
POST_FIELDS = [*PRE_FIELDS, "transaction_reference", "grievance_redressal"]

CASES: list[dict[str, Any]] = []


def case(
    case_id: str,
    why: str,
    expect: Verdict,
    citations: list[str] | None = None,
    **context: Any,
) -> None:
    """Register one case. `citations` is the set of clauses the verdict must rest on.

    Checked as a set rather than a list: two breaches have no natural order, and a golden
    set that pinned one would fail whenever a rule was reordered in the YAML.
    """
    base: dict[str, Any] = {
        "mandate_id": case_id,
        "rail": Rail.UPI_AUTOPAY.value,
        "category": MandateCategory.GENERAL.value,
        "amount_inr": 499.0,
        "pre_debit_notice_hours": 36.0,
        "pre_debit_notice_fields": list(PRE_FIELDS),
        "post_transaction_notice_fields": list(POST_FIELDS),
    }
    base.update(context)
    MandateAuditContext.model_validate(base)  # a malformed case fails here, not in CI
    CASES.append(
        {
            "case_id": case_id,
            "requires": "rules",
            "why": why,
            "expected_verdict": expect.value,
            "expected_citations": sorted(citations or []),
            "context": base,
        }
    )


def record_case(case_id: str, why: str, record: str, blocking_field: str) -> None:
    """A free-text record that cannot be judged without reading it -- an expected abstain.

    `blocking_field` names what the record does not determine. It is part of the expectation
    because "abstained" and "abstained for the right reason" are different results: an
    auditor that abstains on everything scores perfect abstain recall and is useless.
    """
    CASES.append(
        {
            "case_id": case_id,
            "requires": "extraction",
            "why": why,
            "expected_verdict": Verdict.NEEDS_HUMAN.value,
            "expected_citations": ["2"],
            "blocking_field": blocking_field,
            "record": record,
        }
    )


# --------------------------------------------------------------------------------
# Clause 2 -- applicability. The abstains that come from scope rather than doubt.
# --------------------------------------------------------------------------------

for rail in (Rail.UPI_AUTOPAY, Rail.CARD, Rail.PPI):
    case(
        f"scope_in_{rail.value}",
        f"clause 2 names cards / PPI / UPI; {rail.value} is inside the framework",
        Verdict.COMPLIANT,
        rail=rail.value,
    )

case(
    "scope_out_enach_clean",
    "clause 2 does not name eNACH. A clean eNACH mandate is still not gradeable here -- "
    "NACH runs under NPCI's guidelines, and `compliant` would be a claim about a rulebook "
    "that does not reach it",
    Verdict.NEEDS_HUMAN,
    ["2"],
    rail=Rail.ENACH.value,
)
case(
    "scope_out_enach_also_breaching",
    "an eNACH mandate that also notices late. Reporting the 6(a) breach would be a finding "
    "under a framework that does not apply -- scope outranks breach",
    Verdict.NEEDS_HUMAN,
    ["2"],
    rail=Rail.ENACH.value,
    pre_debit_notice_hours=2.0,
)
case(
    "scope_out_enach_fastag",
    "eNACH plus a FASTag category. Still out of scope; the exemption inside a framework "
    "cannot pull a mandate into it",
    Verdict.NEEDS_HUMAN,
    ["2"],
    rail=Rail.ENACH.value,
    category=MandateCategory.FASTAG.value,
    pre_debit_notice_hours=None,
)

# --------------------------------------------------------------------------------
# Clause 4 -- registration, validity, variable amounts, modification.
# --------------------------------------------------------------------------------

case(
    "reg_no_afa_at_registration",
    "clause 4(a): a mandate registered without AFA is void at the root, and every debit "
    "under it inherits the defect",
    Verdict.NON_COMPLIANT,
    ["4(a)"],
    afa_at_registration=False,
)
case(
    "reg_afa_present",
    "clause 4(a) satisfied -- the baseline the case above is measured against",
    Verdict.COMPLIANT,
)
case(
    "validity_not_specified",
    "clause 4(b): every e-mandate must specify its validity period",
    Verdict.NON_COMPLIANT,
    ["4(b)"],
    validity_period_specified=False,
)
case(
    "withdrawal_not_offered",
    "clause 4(b): the customer must be able to modify or withdraw at any point",
    Verdict.NON_COMPLIANT,
    ["4(b)"],
    withdrawal_facility_offered=False,
)
case(
    "validity_and_withdrawal_both_missing",
    "clause 4(b) holds both obligations in one sentence, so both failing is one finding",
    Verdict.NON_COMPLIANT,
    ["4(b)"],
    validity_period_specified=False,
    withdrawal_facility_offered=False,
)
case(
    "variable_without_customer_cap",
    "clause 4(c): a variable mandate must let the customer set a maximum",
    Verdict.NON_COMPLIANT,
    ["4(c)"],
    is_variable_amount=True,
    customer_cap_inr=None,
)
case(
    "variable_with_customer_cap",
    "clause 4(c) satisfied, and the debit is inside the cap",
    Verdict.COMPLIANT,
    is_variable_amount=True,
    customer_cap_inr=1000.0,
    amount_inr=499.0,
)
case(
    "variable_debit_above_cap",
    "the INFERENCE rule: clause 4(c) grants the facility and does not say exceeding it is "
    "a breach. This project reads the cap as binding (limitations.md 8.3)",
    Verdict.NON_COMPLIANT,
    ["4(c)"],
    is_variable_amount=True,
    customer_cap_inr=500.0,
    amount_inr=900.0,
)
case(
    "variable_debit_exactly_at_cap",
    "a cap is a maximum, so debiting exactly it is inside it",
    Verdict.COMPLIANT,
    is_variable_amount=True,
    customer_cap_inr=500.0,
    amount_inr=500.0,
)
case(
    "fixed_mandate_needs_no_cap",
    "clause 4(c) is guarded on variable mandates. A fixed mandate has no maximum to set, "
    "and firing this rule on the majority of the book would be a false finding",
    Verdict.COMPLIANT,
    is_variable_amount=False,
    customer_cap_inr=None,
)
case(
    "notification_mode_choice_absent",
    "clause 4(d): the customer chooses the channel for the pre-transaction notification",
    Verdict.NON_COMPLIANT,
    ["4(d)"],
    notification_mode_choice_offered=False,
)
case(
    "modification_without_afa",
    "clause 4(e): a change to an existing mandate needs AFA",
    Verdict.NON_COMPLIANT,
    ["4(e)"],
    is_modification=True,
    afa_on_modification=False,
)
case(
    "withdrawal_without_afa",
    "clause 4(e) covers withdrawal as well as modification -- letting a customer cancel "
    "without AFA is a compliance failure, not a courtesy",
    Verdict.NON_COMPLIANT,
    ["4(e)"],
    is_modification=True,
    afa_on_modification=False,
)
case(
    "no_modification_so_4e_is_silent",
    "clause 4(e) is guarded. An ordinary debit is not a modification",
    Verdict.COMPLIANT,
    is_modification=False,
    afa_on_modification=False,
)

# --------------------------------------------------------------------------------
# Clause 5 -- the first transaction.
# --------------------------------------------------------------------------------

case(
    "first_transaction_without_afa",
    "clause 5(a): the first transaction under an e-mandate requires AFA",
    Verdict.NON_COMPLIANT,
    ["5(a)"],
    is_first_transaction=True,
    afa_on_first_transaction=False,
)
case(
    "first_transaction_with_afa",
    "clause 5(a) satisfied; the clause allows it to be combined with registration's AFA",
    Verdict.COMPLIANT,
    is_first_transaction=True,
    afa_on_first_transaction=True,
)
case(
    "later_transaction_needs_no_first_afa",
    "clause 5(a) is guarded on the first transaction only",
    Verdict.COMPLIANT,
    is_first_transaction=False,
    afa_on_first_transaction=False,
)

# --------------------------------------------------------------------------------
# Clause 6(a) -- the 24-hour lead time. The boundary this project quotes most.
# --------------------------------------------------------------------------------

for hours, expect in [
    (0.0, Verdict.NON_COMPLIANT),
    (1.0, Verdict.NON_COMPLIANT),
    (12.0, Verdict.NON_COMPLIANT),
    (23.0, Verdict.NON_COMPLIANT),
    (23.9, Verdict.NON_COMPLIANT),
    (24.0, Verdict.COMPLIANT),
    (24.1, Verdict.COMPLIANT),
    (48.0, Verdict.COMPLIANT),
    (168.0, Verdict.COMPLIANT),
]:
    case(
        f"notice_lead_{str(hours).replace('.', 'p')}h",
        f"clause 6(a): 'at least 24 hours' is inclusive, so {hours}h is "
        f"{'inside' if expect is Verdict.COMPLIANT else 'outside'} it",
        expect,
        None if expect is Verdict.COMPLIANT else ["6(a)"],
        pre_debit_notice_hours=hours,
    )

case(
    "notice_never_sent",
    "clause 6(a): no notification at all is a different failure from one sent late, and "
    "the rule's `is not None` guard is what keeps it a finding rather than a crash",
    Verdict.NON_COMPLIANT,
    ["6(a)", "6(b)"],
    pre_debit_notice_hours=None,
    pre_debit_notice_fields=[],
)

# --------------------------------------------------------------------------------
# Clause 6(b) -- the five mandatory contents, one at a time.
# --------------------------------------------------------------------------------

for missing in PRE_FIELDS:
    case(
        f"notice_missing_{missing}",
        f"clause 6(b) lists five minimum contents; this notice omits {missing}",
        Verdict.NON_COMPLIANT,
        ["6(b)"],
        pre_debit_notice_fields=[f for f in PRE_FIELDS if f != missing],
    )

case(
    "notice_with_extra_fields_is_fine",
    "clause 6(b) says 'at the minimum'. More than five fields is not a breach",
    Verdict.COMPLIANT,
    pre_debit_notice_fields=[*PRE_FIELDS, "grievance_redressal", "support_phone"],
)

# --------------------------------------------------------------------------------
# Clause 6(c) -- the opt-out, and the AFA on it that gets missed.
# --------------------------------------------------------------------------------

case(
    "opt_out_absent",
    "clause 6(c): the customer must be able to opt out of the debit or the mandate",
    Verdict.NON_COMPLIANT,
    ["6(c)"],
    opt_out_offered=False,
)
case(
    "opt_out_without_afa",
    "clause 6(c): a one-click unsubscribe is a breach, not a UX improvement -- the opt-out "
    "itself has to be AFA-validated",
    Verdict.NON_COMPLIANT,
    ["6(c)"],
    opt_out_afa_validated=False,
)
case(
    "opt_out_absent_fails_once_not_twice",
    "`opt_out_requires_afa` is guarded on `opt_out_offered`, so one defect is one finding. "
    "Two findings for one defect inflates every non-compliance count downstream",
    Verdict.NON_COMPLIANT,
    ["6(c)"],
    opt_out_offered=False,
    opt_out_afa_validated=False,
)

# --------------------------------------------------------------------------------
# Clause 6(d) -- the exemption, and the breach that runs backwards.
# --------------------------------------------------------------------------------

for exempt in (MandateCategory.FASTAG, MandateCategory.NCMC):
    case(
        f"exempt_{exempt.value}_no_pre_notice",
        f"clause 6(d): {exempt.value} auto-replenishment needs no pre-transaction notice",
        Verdict.COMPLIANT,
        category=exempt.value,
        pre_debit_notice_hours=None,
        pre_debit_notice_fields=[],
        claims_notice_exemption=True,
    )
    case(
        f"exempt_{exempt.value}_still_needs_post_notice",
        "clause 6(d)'s carve-out is written against PRE-transaction notification only. "
        "Clause 7 carries no such exemption, and reading one into it would be inventing one",
        Verdict.NON_COMPLIANT,
        ["7"],
        category=exempt.value,
        pre_debit_notice_hours=None,
        pre_debit_notice_fields=[],
        claims_notice_exemption=True,
        post_transaction_notice_sent=False,
    )

case(
    "exemption_over_claimed_on_general",
    "the breach that runs backwards: suppressing the notice on a mandate that is neither "
    "FASTag nor NCMC. From inside the sending system this looks like 'notice not required'",
    Verdict.NON_COMPLIANT,
    ["6(a)", "6(b)", "6(d)"],
    claims_notice_exemption=True,
    pre_debit_notice_hours=None,
    pre_debit_notice_fields=[],
)
case(
    "exemption_over_claimed_but_notice_sent_anyway",
    "the exemption is claimed and the notice went out regardless. 6(a) and 6(b) are "
    "satisfied by the notice; 6(d) is still breached by the claim",
    Verdict.NON_COMPLIANT,
    ["6(d)"],
    claims_notice_exemption=True,
    pre_debit_notice_hours=36.0,
)
case(
    "fastag_not_claiming_the_exemption_still_needs_no_notice",
    "clause 6(d) is a property of the mandate, not of the claim. A FASTag mandate that "
    "sends a notice anyway is compliant, and one that does not is too",
    Verdict.COMPLIANT,
    category=MandateCategory.FASTAG.value,
    pre_debit_notice_hours=None,
    pre_debit_notice_fields=[],
    claims_notice_exemption=False,
)

# --------------------------------------------------------------------------------
# Clause 7 -- the post-transaction notification and its seven fields.
# --------------------------------------------------------------------------------

case(
    "post_notice_not_sent",
    "clause 7 is unconditional in the text",
    Verdict.NON_COMPLIANT,
    ["7"],
    post_transaction_notice_sent=False,
)
for missing in POST_FIELDS:
    case(
        f"post_notice_missing_{missing}",
        f"clause 7 lists seven minimum contents; this one omits {missing}",
        Verdict.NON_COMPLIANT,
        ["7"],
        post_transaction_notice_fields=[f for f in POST_FIELDS if f != missing],
    )

# --------------------------------------------------------------------------------
# Clause 8 -- the two AFA ceilings. The Rs.14,999 / Rs.15,001 boundary and the
# three named categories that lift it. This is the red-team surface (T4.8).
# --------------------------------------------------------------------------------

for amount, expect in [
    (1.0, Verdict.COMPLIANT),
    (14_999.0, Verdict.COMPLIANT),
    (15_000.0, Verdict.COMPLIANT),
    (15_000.5, Verdict.NON_COMPLIANT),
    (15_001.0, Verdict.NON_COMPLIANT),
    (99_999.0, Verdict.NON_COMPLIANT),
    (100_001.0, Verdict.NON_COMPLIANT),
]:
    case(
        f"afa_general_{int(amount * 10)}",
        f"clause 8(a): AFA-free up to Rs.15,000 inclusive, so Rs.{amount:,.2f} "
        f"{'passes' if expect is Verdict.COMPLIANT else 'needs AFA'}",
        expect,
        None if expect is Verdict.COMPLIANT else ["8(a)"],
        amount_inr=amount,
        afa_on_this_transaction=False,
    )

case(
    "afa_general_above_ceiling_with_afa",
    "clause 8(a) is about AFA-free authorisation, not a maximum debit. Authenticated, any "
    "amount passes",
    Verdict.COMPLIANT,
    amount_inr=250_000.0,
    afa_on_this_transaction=True,
)

for category in (
    MandateCategory.INSURANCE_PREMIUM,
    MandateCategory.MUTUAL_FUND,
    MandateCategory.CREDIT_CARD_BILL,
):
    for amount, expect in [
        (15_001.0, Verdict.COMPLIANT),
        (20_000.0, Verdict.COMPLIANT),
        (100_000.0, Verdict.COMPLIANT),
        (100_001.0, Verdict.NON_COMPLIANT),
    ]:
        case(
            f"afa_enhanced_{category.value}_{int(amount)}",
            f"clause 8(b) lifts {category.value} to Rs.1,00,000 inclusive. Without the "
            "guard on 8(a) this would fail two rules at once and be simultaneously "
            "compliant and not",
            expect,
            None if expect is Verdict.COMPLIANT else ["8(b)"],
            category=category.value,
            amount_inr=amount,
            afa_on_this_transaction=False,
        )

case(
    "afa_enhanced_category_with_afa_above_lakh",
    "clause 8(b), authenticated above its ceiling",
    Verdict.COMPLIANT,
    category=MandateCategory.INSURANCE_PREMIUM.value,
    amount_inr=500_000.0,
    afa_on_this_transaction=True,
)
case(
    "afa_utility_bill_is_not_an_enhanced_category",
    "clause 8(b)'s list is closed: insurance premiums, mutual funds, credit card bills. A "
    "utility bill at Rs.20,000 stays under the Rs.15,000 ceiling however much it resembles "
    "them -- modelled as `general`, which is what a utility debit is",
    Verdict.NON_COMPLIANT,
    ["8(a)"],
    category=MandateCategory.GENERAL.value,
    amount_inr=20_000.0,
    afa_on_this_transaction=False,
)
case(
    "afa_fastag_topup_above_general_ceiling",
    "FASTag is exempt from clause 6(d)'s notice, not from clause 8's ceilings. A "
    "Rs.20,000 replenishment without AFA breaches 8(a)",
    Verdict.NON_COMPLIANT,
    ["8(a)"],
    category=MandateCategory.FASTAG.value,
    amount_inr=20_000.0,
    afa_on_this_transaction=False,
    pre_debit_notice_hours=None,
    pre_debit_notice_fields=[],
)

# --------------------------------------------------------------------------------
# Clauses 9 and 10.
# --------------------------------------------------------------------------------

case(
    "no_grievance_redressal",
    "clause 9(a): a dispute redressal system must exist for the customer to use",
    Verdict.NON_COMPLIANT,
    ["9(a)"],
    grievance_redressal_available=False,
)
for charge, expect in [
    (0.0, Verdict.COMPLIANT),
    (0.01, Verdict.NON_COMPLIANT),
    (5.0, Verdict.NON_COMPLIANT),
    (99.0, Verdict.NON_COMPLIANT),
]:
    case(
        f"customer_charge_{int(charge * 100)}p",
        f"clause 10(a): no charges, not 'reasonable charges'. Rs.{charge:.2f} "
        f"{'is nothing' if expect is Verdict.COMPLIANT else 'is a charge'}",
        expect,
        None if expect is Verdict.COMPLIANT else ["10(a)"],
        customer_charges_inr=charge,
    )
case(
    "acquirer_did_not_check_merchant",
    "clause 10(c): the only sentence in the framework that reaches a merchant, and it "
    "reaches them through their acquirer",
    Verdict.NON_COMPLIANT,
    ["10(c)"],
    acquirer_compliance_checked=False,
)

# --------------------------------------------------------------------------------
# Combinations. A real book fails several clauses at once, and every one has to
# be cited -- a verdict that stops at the first breach hides the rest of the work.
# --------------------------------------------------------------------------------

case(
    "three_independent_breaches",
    "late notice, a charge levied, and no opt-out. Three clauses, three citations",
    Verdict.NON_COMPLIANT,
    ["6(a)", "6(c)", "10(a)"],
    pre_debit_notice_hours=1.0,
    customer_charges_inr=5.0,
    opt_out_offered=False,
)
case(
    "everything_wrong_at_once",
    "the worst case in the set: nine clauses breached simultaneously. Included because a "
    "verdict builder that truncated its citation list would look fine on every case above",
    Verdict.NON_COMPLIANT,
    ["4(a)", "4(b)", "4(d)", "6(a)", "6(b)", "6(c)", "7", "9(a)", "10(a)", "10(c)"],
    afa_at_registration=False,
    validity_period_specified=False,
    withdrawal_facility_offered=False,
    notification_mode_choice_offered=False,
    pre_debit_notice_hours=None,
    pre_debit_notice_fields=[],
    opt_out_offered=False,
    post_transaction_notice_sent=False,
    grievance_redressal_available=False,
    customer_charges_inr=25.0,
    acquirer_compliance_checked=False,
)
case(
    "enach_with_everything_wrong",
    "out of scope AND breaching nine clauses. Still needs_human: scope outranks breach, "
    "however many findings are waiting behind it",
    Verdict.NEEDS_HUMAN,
    ["2"],
    rail=Rail.ENACH.value,
    afa_at_registration=False,
    pre_debit_notice_hours=None,
    pre_debit_notice_fields=[],
    opt_out_offered=False,
    customer_charges_inr=25.0,
)
case(
    "amount_boundary_plus_late_notice",
    "Rs.15,001 without AFA and a 23-hour notice. Two clauses that a single-finding auditor "
    "would report as one",
    Verdict.NON_COMPLIANT,
    ["6(a)", "8(a)"],
    amount_inr=15_001.0,
    afa_on_this_transaction=False,
    pre_debit_notice_hours=23.0,
)
case(
    "variable_over_cap_and_over_ceiling",
    "a variable mandate debiting above both the customer's cap and clause 8(a)'s ceiling",
    Verdict.NON_COMPLIANT,
    ["4(c)", "8(a)"],
    is_variable_amount=True,
    customer_cap_inr=10_000.0,
    amount_inr=20_000.0,
    afa_on_this_transaction=False,
)
case(
    "clean_mandate_across_every_clause",
    "the all-clear. Without it, a verdict builder that returned NON_COMPLIANT "
    "unconditionally would pass most of this file",
    Verdict.COMPLIANT,
)

# --------------------------------------------------------------------------------
# Expected abstains from missing input, not from scope. These need extraction.
# --------------------------------------------------------------------------------

record_case(
    "record_silent_on_afa_above_ceiling",
    "Rs.20,000 with no statement about authentication. Defaulting the field to False would "
    "fail a debit that may have been properly authenticated -- a confident wrong verdict "
    "from a missing input",
    "Mandate MG-1102 on UPI AutoPay, Rs 20,000/- monthly to Acme Insurance. Debit "
    "scheduled 12 Sep 2026. Notice sent 10 Sep 2026.",
    "afa_on_this_transaction",
)
record_case(
    "record_silent_on_category_at_twenty_thousand",
    "Rs.20,000 where the category decides which ceiling applies. Insurance passes under "
    "8(b); anything else fails under 8(a). The record does not say which",
    "Mandate MG-1188, card rail, Rs 20,000/- per quarter to Sunrise Services Pvt Ltd. "
    "Registered with AFA. Notice sent 3 days ahead with all details.",
    "category",
)
record_case(
    "record_silent_on_rail",
    "the rail decides whether the framework applies at all (clause 2). A record that does "
    "not name it cannot be graded or excluded",
    "Standing instruction MG-1204 for Rs 899/- monthly to Hoichoi. Set up in March 2026, "
    "notices going out on schedule, no complaints on file.",
    "rail",
)
record_case(
    "record_silent_on_notice_timing",
    "a notice was sent; the record does not say when. Clause 6(a) is entirely about when",
    "Mandate MG-1250, UPI AutoPay, Rs 149/- monthly to a music service. Pre-debit "
    "notification was sent to the customer's registered email with all required details.",
    "pre_debit_notice_hours",
)
record_case(
    "record_silent_on_variable_cap",
    "a variable-amount mandate whose customer maximum is not recorded. Clause 4(c) turns "
    "on exactly that",
    "Mandate MG-1301, UPI AutoPay, variable amount billed monthly to CloudScale Hosting. "
    "Last debit Rs 2,340/-. Customer registered in Jan 2026 with bank authentication.",
    "customer_cap_inr",
)
record_case(
    "record_silent_on_opt_out",
    "clause 6(c) requires an opt-out facility. A record describing the notice's contents "
    "without mentioning one determines nothing about whether it exists",
    "Mandate MG-1355, PPI wallet, Rs 299/- monthly. Notice sent 48 hours ahead listing "
    "the merchant, the amount, the debit date and time, the mandate reference and reason.",
    "opt_out_offered",
)


# --------------------------------------------------------------------------------
# The red-team surface T4.8 names, and the two entries on that list that have no
# case here -- stated rather than silently missing.
#
# T4.8 lists: Rs.14,999 vs Rs.15,001 (covered, `afa_general_*`), the FASTag/NCMC
# exemption (covered, `exempt_*`), a mid-cycle modification (covered,
# `modification_without_afa`), a pre-April-2026 grandfathered mandate (below), the
# Rs.1 lakh insurance cap (covered, `afa_enhanced_*`), a variable-amount mandate with
# a customer cap (covered, `variable_*`), and cross-border.
#
# **Cross-border has no case, and that is the finding.** Clause 2 names it explicitly:
# "recurring transactions, domestic or cross-border". The framework therefore draws no
# distinction, so there is nothing for a rule to test and no case that would differ from
# `scope_in_card`. The risk cross-border carries is that a *reader* assumes it is out of
# scope; that is a documentation failure, not a rule failure, and a case asserting
# "cross-border behaves identically" would be a case asserting that a field nobody stores
# changes nothing.
#
# **Grandfathering is modelled as the text reads, and the text may be wrong for it.**
# Clause 1(b) is "effective immediately" and clause 11 repeals eight circulars with no
# savings clause, so a mandate registered in 2024 is judged by the 2026 rules like any
# other. The framework is *silent* on whether such mandates must be re-registered
# (`limitations.md` 8.4). The two cases below encode the reading this project ships;
# if the RBI clarifies otherwise, they are the two lines that change.
# --------------------------------------------------------------------------------

case(
    "grandfathered_registered_without_afa",
    "a mandate registered under the pre-2026 regime without AFA. No savings clause exists "
    "in clause 11, so clause 4(a) reaches it and the remedy is re-registration. This "
    "expectation encodes a reading of silence -- limitations.md 8.4",
    Verdict.NON_COMPLIANT,
    ["4(a)"],
    afa_at_registration=False,
)
case(
    "grandfathered_otherwise_clean",
    "an old mandate that happens to satisfy the new rules. Nothing about its age excuses "
    "or condemns it -- the framework has no transition period either way",
    Verdict.COMPLIANT,
)

# --------------------------------------------------------------------------------
# Rail crossed with the clauses that do not depend on rail. A rule that quietly
# only worked on UPI would pass every case above.
# --------------------------------------------------------------------------------

for other_rail in (Rail.CARD, Rail.PPI):
    case(
        f"{other_rail.value}_late_notice",
        f"clause 6(a) is not a UPI rule. A {other_rail.value} mandate breaches it identically",
        Verdict.NON_COMPLIANT,
        ["6(a)"],
        rail=other_rail.value,
        pre_debit_notice_hours=6.0,
    )
    case(
        f"{other_rail.value}_above_general_ceiling",
        f"clause 8(a) is not a UPI rule either -- Rs.15,001 on {other_rail.value} needs AFA",
        Verdict.NON_COMPLIANT,
        ["8(a)"],
        rail=other_rail.value,
        amount_inr=15_001.0,
        afa_on_this_transaction=False,
    )

# --------------------------------------------------------------------------------
# Cases that look like breaches and are not. A rulebook is judged by its false
# positives as much as by its catches -- every one of these would land a compliant
# mandate in a review queue.
# --------------------------------------------------------------------------------

case(
    "tiny_amount_is_not_suspicious",
    "Rs.1.00 is a legitimate debit. Nothing in the framework sets a floor",
    Verdict.COMPLIANT,
    amount_inr=1.0,
)
case(
    "very_long_notice_lead_is_fine",
    "clause 6(a) sets a minimum, not a window. Two weeks ahead is compliant",
    Verdict.COMPLIANT,
    pre_debit_notice_hours=336.0,
)
case(
    "post_notice_with_extra_fields",
    "clause 7 says 'at the minimum'",
    Verdict.COMPLIANT,
    post_transaction_notice_fields=[*POST_FIELDS, "support_phone", "bank_reference"],
)
case(
    "first_transaction_and_modification_together",
    "both guards true at once. A mandate modified before its first debit satisfies 4(e) "
    "and 5(a) separately, and neither rule swallows the other",
    Verdict.COMPLIANT,
    is_first_transaction=True,
    is_modification=True,
    afa_on_first_transaction=True,
    afa_on_modification=True,
)
case(
    "variable_mandate_at_the_general_ceiling",
    "a variable mandate debiting exactly Rs.15,000 inside a Rs.50,000 customer cap. Two "
    "ceilings, both satisfied, and a rule that confused them would fail this",
    Verdict.COMPLIANT,
    is_variable_amount=True,
    customer_cap_inr=50_000.0,
    amount_inr=15_000.0,
    afa_on_this_transaction=False,
)
case(
    "ncmc_at_the_general_ceiling",
    "NCMC is exempt from the notice, not from the ceiling. Rs.15,000 exactly passes",
    Verdict.COMPLIANT,
    category=MandateCategory.NCMC.value,
    amount_inr=15_000.0,
    pre_debit_notice_hours=None,
    pre_debit_notice_fields=[],
    claims_notice_exemption=True,
)

# --------------------------------------------------------------------------------
# The remaining pairwise interactions worth pinning.
# --------------------------------------------------------------------------------

case(
    "enhanced_category_but_notice_late",
    "clause 8(b) lifts the AFA ceiling and says nothing about notices. An insurance "
    "premium notified 3 hours ahead still breaches 6(a)",
    Verdict.NON_COMPLIANT,
    ["6(a)"],
    category=MandateCategory.INSURANCE_PREMIUM.value,
    amount_inr=50_000.0,
    pre_debit_notice_hours=3.0,
)
case(
    "modification_without_afa_on_an_out_of_scope_rail",
    "an eNACH modification without AFA. Scope still outranks the 4(e) breach",
    Verdict.NEEDS_HUMAN,
    ["2"],
    rail=Rail.ENACH.value,
    is_modification=True,
    afa_on_modification=False,
)
case(
    "charge_levied_on_an_exempt_category",
    "clause 10(a) reaches every mandate. FASTag's notice exemption does not buy it a fee",
    Verdict.NON_COMPLIANT,
    ["10(a)"],
    category=MandateCategory.FASTAG.value,
    pre_debit_notice_hours=None,
    pre_debit_notice_fields=[],
    claims_notice_exemption=True,
    customer_charges_inr=10.0,
)
case(
    "no_opt_out_on_an_exempt_mandate",
    "clause 6(c) is not carved out by 6(d). An NCMC mandate needs no pre-debit notice and "
    "still needs an opt-out",
    Verdict.NON_COMPLIANT,
    ["6(c)"],
    category=MandateCategory.NCMC.value,
    pre_debit_notice_hours=None,
    pre_debit_notice_fields=[],
    claims_notice_exemption=True,
    opt_out_offered=False,
)
case(
    "notice_contents_missing_on_an_exempt_mandate",
    "clause 6(b) is guarded by the same exemption as 6(a). An exempt mandate with an empty "
    "notice field set is not breaching a content rule it is exempt from",
    Verdict.COMPLIANT,
    category=MandateCategory.FASTAG.value,
    pre_debit_notice_hours=None,
    pre_debit_notice_fields=[],
)
case(
    "first_transaction_above_the_ceiling_without_afa",
    "clause 5(a) and clause 8(a) are different AFAs at different moments, and a first "
    "debit of Rs.20,000 with neither breaches both",
    Verdict.NON_COMPLIANT,
    ["5(a)", "8(a)"],
    is_first_transaction=True,
    afa_on_first_transaction=False,
    amount_inr=20_000.0,
    afa_on_this_transaction=False,
)
case(
    "acquirer_check_missing_on_an_otherwise_clean_mandate",
    "clause 10(c) is the merchant-facing obligation and fails alone. Included because it "
    "is the one rule this project can actually be evidence for",
    Verdict.NON_COMPLIANT,
    ["10(c)"],
    acquirer_compliance_checked=False,
)
case(
    "grievance_missing_but_post_notice_claims_it",
    "clause 7 wants grievance details in the notice; clause 9(a) wants the system to exist. "
    "A notice that names a redressal path that is not there breaches 9(a) and not 7",
    Verdict.NON_COMPLIANT,
    ["9(a)"],
    grievance_redressal_available=False,
    post_transaction_notice_fields=list(POST_FIELDS),
)
case(
    "notice_sent_but_no_fields_recorded",
    "a notice sent 36 hours ahead whose contents were not recorded at all. Clause 6(a) is "
    "satisfied and 6(b) is not, and conflating them would hide which half is broken",
    Verdict.NON_COMPLIANT,
    ["6(b)"],
    pre_debit_notice_hours=36.0,
    pre_debit_notice_fields=[],
)
case(
    "post_notice_sent_but_no_fields_recorded",
    "the same split on clause 7: sent, contents unknown",
    Verdict.NON_COMPLIANT,
    ["7"],
    post_transaction_notice_sent=True,
    post_transaction_notice_fields=[],
)


# --------------------------------------------------------------------------------
# The natural / adversarial split (T4.8, partial).
#
# T4.8 -- an adversarial *generator* that keeps producing new cases -- is CUT #2 in
# `docs/tasks.md`, and it is cut here: no credential exists to run a generator with, and
# a generator is exactly the part that cannot be faked, because its value is finding the
# cases nobody thought of.
#
# What is NOT cut is the number GATE 4 asks for. The gap between natural-set accuracy and
# adversarial-set accuracy is computable from this set as it stands, because a large part
# of it was written adversarially on purpose. So the split is declared here and
# `make_llm_eval.py` reports both figures and their difference.
#
# **The split is a judgement, and this is the judgement.** A case is adversarial when it
# exists to attack a specific decision boundary rather than to describe an ordinary
# mandate:
#
#   * it sits within one unit of a threshold in the text (Rs.14,999 / Rs.15,000 /
#     Rs.15,001; 23h / 23.9h / 24h / 24.1h; Rs.1,00,000 / Rs.1,00,001);
#   * it turns on an exemption interacting with a rule the exemption does not cover
#     (FASTag and clause 7, NCMC and clause 8(a), an over-claimed exemption);
#   * it is a scope trap -- out of scope AND breaching, where the tempting answer is the
#     breach;
#   * it combines several breaches, where a single-finding auditor looks correct;
#   * it is a near-miss that should NOT be a finding, which is the direction a cautious
#     system fails in.
#
# Everything else is natural: one ordinary defect, or a clean mandate. The rule is written
# down rather than eyeballed so that a reader can disagree with a specific line instead of
# with a vibe -- and so that adding a case later forces a decision about which family it
# joins.
# --------------------------------------------------------------------------------

ADVERSARIAL_IDS = {
    # Thresholds, within one unit either side.
    "afa_general_149990",
    "afa_general_150000",
    "afa_general_150005",
    "afa_general_150010",
    "afa_enhanced_insurance_premium_100000",
    "afa_enhanced_insurance_premium_100001",
    "afa_enhanced_mutual_fund_100000",
    "afa_enhanced_mutual_fund_100001",
    "afa_enhanced_credit_card_bill_100000",
    "afa_enhanced_credit_card_bill_100001",
    "afa_enhanced_insurance_premium_15001",
    "afa_enhanced_mutual_fund_15001",
    "afa_enhanced_credit_card_bill_15001",
    "notice_lead_23p0h",
    "notice_lead_23p9h",
    "notice_lead_24p0h",
    "notice_lead_24p1h",
    "variable_debit_exactly_at_cap",
    "variable_mandate_at_the_general_ceiling",
    "ncmc_at_the_general_ceiling",
    "customer_charge_1p",
    # Exemptions interacting with rules they do not cover.
    "exempt_fastag_still_needs_post_notice",
    "exempt_ncmc_still_needs_post_notice",
    "exemption_over_claimed_on_general",
    "exemption_over_claimed_but_notice_sent_anyway",
    "fastag_not_claiming_the_exemption_still_needs_no_notice",
    "charge_levied_on_an_exempt_category",
    "no_opt_out_on_an_exempt_mandate",
    "notice_contents_missing_on_an_exempt_mandate",
    "afa_fastag_topup_above_general_ceiling",
    "afa_utility_bill_is_not_an_enhanced_category",
    # Scope traps: the tempting answer is the breach.
    "scope_out_enach_also_breaching",
    "scope_out_enach_fastag",
    "enach_with_everything_wrong",
    "modification_without_afa_on_an_out_of_scope_rail",
    # Combinations, where a single-finding auditor looks correct.
    "three_independent_breaches",
    "everything_wrong_at_once",
    "amount_boundary_plus_late_notice",
    "variable_over_cap_and_over_ceiling",
    "first_transaction_above_the_ceiling_without_afa",
    "grievance_missing_but_post_notice_claims_it",
    "enhanced_category_but_notice_late",
    "opt_out_absent_fails_once_not_twice",
    "notice_sent_but_no_fields_recorded",
    "post_notice_sent_but_no_fields_recorded",
    # Near-misses that must NOT be findings -- the direction a cautious system fails in.
    "fixed_mandate_needs_no_cap",
    "no_modification_so_4e_is_silent",
    "later_transaction_needs_no_first_afa",
    "notice_with_extra_fields_is_fine",
    "post_notice_with_extra_fields",
    "first_transaction_and_modification_together",
    "very_long_notice_lead_is_fine",
    "tiny_amount_is_not_suspicious",
    # Grandfathering: a reading of the framework's silence.
    "grandfathered_registered_without_afa",
    "grandfathered_otherwise_clean",
}


def assign_families() -> None:
    """Tag every case, and refuse to ship a tag that names a case that does not exist.

    The stale-id check matters more than it looks: renaming a case would silently move it
    from adversarial to natural, the adversarial set would shrink, and the gap this whole
    split exists to measure would improve for no reason at all.
    """
    ids = {c["case_id"] for c in CASES}
    unknown = sorted(ADVERSARIAL_IDS - ids)
    if unknown:
        raise SystemExit(
            f"ADVERSARIAL_IDS names {len(unknown)} case(s) that do not exist: {unknown}. "
            "A stale id silently shrinks the adversarial set and improves the gap."
        )
    for entry in CASES:
        entry["family"] = "adversarial" if entry["case_id"] in ADVERSARIAL_IDS else "natural"


def build() -> str:
    """Serialise the cases, sorted, one JSON object per line."""
    assign_families()
    ids = [c["case_id"] for c in CASES]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise SystemExit(f"duplicate case_id(s): {duplicates}")
    lines = [
        json.dumps(c, sort_keys=True, ensure_ascii=False)
        for c in sorted(CASES, key=lambda c: c["case_id"])
    ]
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the committed file is stale")
    args = parser.parse_args()

    content = build()
    rules = sum(1 for c in CASES if c["requires"] == "rules")
    extraction = len(CASES) - rules
    adversarial = sum(1 for c in CASES if c["case_id"] in ADVERSARIAL_IDS)

    if args.check:
        existing = GOLDEN_PATH.read_text(encoding="utf-8") if GOLDEN_PATH.is_file() else ""
        if existing != content:
            print(
                f"{GOLDEN_PATH} is stale. Run `uv run python scripts/build_golden.py`.",
                file=sys.stderr,
            )
            return 1
        print(
            f"{GOLDEN_PATH.name} is current: {len(CASES)} cases ({rules} rules, "
            f"{extraction} extraction)."
        )
        return 0

    GOLDEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN_PATH.write_text(content, encoding="utf-8")
    print(f"wrote {len(CASES)} cases to {GOLDEN_PATH}")
    print(f"  {rules} judged by rules alone (CI scores these)")
    print(f"  {extraction} needing extraction, all expected abstains (no cassettes yet)")
    print(f"  {adversarial} adversarial, {len(CASES) - adversarial} natural (T4.8's gap)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
