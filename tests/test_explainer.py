"""T4.5 -- the refusal explainer: a rupee-backed reason for every mandate not asked.

The bar the task sets is "ledger entries carry a human-readable reason string", and the
last test in this file is that literal check. Everything above it is about the two ways a
refusal explanation goes wrong: saying the wrong *kind* of thing (a budget loss described
as a judgement about the customer), and saying a number that is not true.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from mandateguard.agent.client import LLMResult, LLMUsage
from mandateguard.agent.explainer import (
    RefusalExplainer,
    RefusalFacts,
    RefusalKind,
    _invented_amounts,
    explain_deterministically,
)
from mandateguard.models import Decision, DecisionKind, LedgerEntry


def not_worth_asking(**overrides) -> RefusalFacts:
    base = {
        "mandate_id": "mg_1",
        "week": 3,
        "kind": RefusalKind.NOT_WORTH_ASKING,
        "channel": "sms",
        "gain_inr": Decimal("0.04"),
        "backfire_inr": Decimal("0.09"),
        "fatigue_inr": Decimal("0.05"),
        "channel_cost_inr": Decimal("0.15"),
        "net_inr": Decimal("-0.25"),
    }
    return RefusalFacts(**(base | overrides))


def outbid(**overrides) -> RefusalFacts:
    base = {
        "mandate_id": "mg_2",
        "week": 3,
        "kind": RefusalKind.OUTBID,
        "channel": "whatsapp",
        "gain_inr": Decimal("12.40"),
        "backfire_inr": Decimal("3.10"),
        "fatigue_inr": Decimal("1.05"),
        "channel_cost_inr": Decimal("0.35"),
        "net_inr": Decimal("7.90"),
        "budget_inr": Decimal("500.00"),
        "theta_inr": Decimal("4.5626"),
        "competing_asks": 48_213,
    }
    return RefusalFacts(**(base | overrides))


class ScriptedClient:
    """Returns a fixed rewrite. Not a model; see docs/limitations.md §8.5."""

    def __init__(self, text: str, *, stop_reason: str = "end_turn") -> None:
        self.text = text
        self.stop_reason = stop_reason

    def run(self, job, system, prompt, *, schema=None):
        return LLMResult(
            job=job,
            key="scripted",
            model="scripted",
            text=self.text,
            stop_reason=self.stop_reason,
            usage=LLMUsage(input_tokens=0, output_tokens=0),
            latency_ms=0,
        )


# --------------------------------------------------------------------------------
# Three refusals, three different sentences.
# --------------------------------------------------------------------------------


def test_a_worthless_ask_says_more_budget_would_not_help():
    text = explain_deterministically(not_worth_asking())
    assert "A larger budget would not change this" in text
    assert "INR 0.04" in text


def test_an_outbid_ask_says_it_was_a_budget_decision_not_a_judgement():
    """The distinction the whole refusal ledger turns on. One of these says "buy more
    budget"; the other says "do not bother". A ledger that renders them identically has
    thrown away the only actionable thing in it."""
    text = explain_deterministically(outbid())
    assert "budget decision" in text
    assert "48,213 asks competed" in text
    assert "INR 4.56" in text  # theta, the week's cut-off


def test_the_floor_policy_does_not_pretend_to_have_judged_the_mandate():
    """P0 contacts nobody. Rendering that as 480,000 individual assessments would make the
    floor arm's ledger look like a decision it never made."""
    text = explain_deterministically(
        RefusalFacts(mandate_id="mg_3", week=0, kind=RefusalKind.FLOOR)
    )
    assert "never contacts anyone" in text
    assert "not a judgement about this mandate" in text


def test_the_explanation_names_the_cost_that_actually_sank_the_ask():
    """The four-term decomposition is correct and does not answer the question anyone asks,
    which is "what stopped it". Here the channel cost (0.15) is the largest of the three."""
    text = explain_deterministically(not_worth_asking())
    assert "the cost of the message itself" in text

    fatigued = not_worth_asking(fatigue_inr=Decimal("2.00"), net_inr=Decimal("-2.20"))
    assert "annoyance of contacting them again" in explain_deterministically(fatigued)


def test_an_outbid_refusal_that_names_no_channel_is_rejected():
    """A mandate cannot lose a budget contest it never entered."""
    with pytest.raises(ValidationError, match="cannot lose a budget contest"):
        outbid(channel=None)


def test_an_outbid_refusal_with_a_negative_value_is_rejected():
    """A negative-value ask did not lose to a better one; it was never worth making, and
    mislabelling it moves a mandate between the two answers above."""
    with pytest.raises(ValidationError, match="never worth making"):
        outbid(net_inr=Decimal("-1.00"))


def test_the_same_facts_explain_identically_every_time():
    """ADR 0003 again: a refusal reason is a derived artefact and it lands in the ledger."""
    assert explain_deterministically(outbid()) == explain_deterministically(outbid())


# --------------------------------------------------------------------------------
# The fabrication check, which is the leash on the model.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize("facts_fn", [not_worth_asking, outbid])
def test_the_deterministic_sentence_passes_its_own_fabrication_check(facts_fn):
    """Load-bearing, and the first version of this module failed it.

    The sentence prints backfire + fatigue + channel cost as one figure. That sum was not
    in `allowed_amounts`, so a rewrite faithfully repeating the sentence it was given would
    have been rejected for quoting a number "the record does not contain" -- and every
    correct rewrite of a NOT_WORTH_ASKING refusal would have been discarded.
    """
    facts = facts_fn()
    assert _invented_amounts(explain_deterministically(facts), facts) == []


def test_a_rewrite_that_invents_a_number_is_discarded_and_recorded():
    facts = not_worth_asking()
    explainer = RefusalExplainer(client=ScriptedClient("We skipped this to save INR 9.99."))
    explanation = explainer.explain(facts)
    assert explanation.source == "deterministic"
    assert explanation.text == explain_deterministically(facts)
    assert explanation.invented_amounts == ["INR 9.99"]
    assert explanation.rejected_rewrite == "We skipped this to save INR 9.99."


def test_a_rewrite_that_only_repeats_the_given_figures_is_used():
    facts = not_worth_asking()
    rewrite = "Skipped in week 3. The ask was worth INR 0.04 and would have cost INR 0.29."
    explanation = RefusalExplainer(client=ScriptedClient(rewrite)).explain(facts)
    assert explanation.source == "llm"
    assert explanation.text == rewrite


def test_a_rewrite_may_write_a_figure_without_indian_grouping():
    """`INR 500` and `INR 500.00` are the same figure. Rejecting a rewrite over a trailing
    zero would discard correct text and quietly make the model arm look useless."""
    facts = outbid()
    rewrite = "Not contacted: INR 500 of budget went to better asks; this one was INR 7.9."
    assert RefusalExplainer(client=ScriptedClient(rewrite)).explain(facts).source == "llm"


def test_a_rewrite_may_drop_the_minus_sign():
    """Prose says "loses 25 paise", not "is worth -0.25"."""
    facts = not_worth_asking()
    rewrite = "Not contacted: the ask loses INR 0.25 once every cost is counted."
    assert RefusalExplainer(client=ScriptedClient(rewrite)).explain(facts).source == "llm"


def test_bare_numbers_in_a_rewrite_are_not_treated_as_money():
    """The week number and the count of competing asks are not claims about rupees."""
    facts = outbid()
    rewrite = "In week 3, 48213 asks competed and this one lost. Budget was INR 500.00."
    assert RefusalExplainer(client=ScriptedClient(rewrite)).explain(facts).source == "llm"


def test_a_refusal_from_the_model_falls_back_silently():
    """There is a correct answer already in hand, so there is nothing to escalate. This is
    the one place in Phase 4 where a model failure is *not* worth a human's attention."""
    facts = not_worth_asking()
    client = ScriptedClient("I cannot help with that.", stop_reason="refusal")
    explanation = RefusalExplainer(client=client).explain(facts)
    assert explanation.source == "deterministic"
    assert explanation.text == explain_deterministically(facts)


def test_an_empty_rewrite_falls_back():
    facts = not_worth_asking()
    assert RefusalExplainer(client=ScriptedClient("   ")).explain(facts).source == "deterministic"


def test_no_client_means_no_call_and_a_real_explanation():
    """T5.3's degradation ladder: LLM down leads to rules-only, and the ledger still reads."""
    explanation = RefusalExplainer().explain(not_worth_asking())
    assert explanation.source == "deterministic"
    assert explanation.text


# --------------------------------------------------------------------------------
# T4.5's stated bar: it reaches the ledger.
# --------------------------------------------------------------------------------


def test_only_a_not_asked_decision_is_rewritten():
    """An ASKED decision already carries the pricer's justification; replacing it with a
    refusal explanation would put the wrong sentence against a contact that happened."""
    asked = Decision(
        mandate_id="mg_1",
        week=3,
        kind=DecisionKind.ASKED,
        channel="sms",
        value_inr=1.2,
        reason="original",
    )
    assert RefusalExplainer().explain_decision(asked, not_worth_asking()).reason == "original"


def test_a_ledger_entry_carries_the_human_readable_reason():
    """T4.5's done-when, asserted end to end: a not-asked decision goes into a LedgerEntry
    and the reason that comes out the other side is a sentence, in rupees, that names why."""
    decision = Decision(
        mandate_id="mg_1",
        week=3,
        kind=DecisionKind.NOT_ASKED,
        value_inr=0.0,
        reason="not asked: via sms the expected value is INR -0.25",
    )
    explained = RefusalExplainer().explain_decision(decision, not_worth_asking())
    entry = LedgerEntry(
        decision_id="mg_1:w3",
        decision=explained,
        policy_hash="ce28096eeba5ad9d",
        model_version="rules-only",
        seed=20260905,
        created_at=date(2026, 9, 2),
    )
    assert entry.decision.kind is DecisionKind.NOT_ASKED
    assert "INR" in entry.decision.reason
    assert "Not contacted in week 3" in entry.decision.reason
    assert entry.decision.reason != decision.reason


def test_the_explained_decision_is_a_new_object_not_a_mutation():
    """`Decision` is frozen, and the ledger has to be able to say which text was recorded at
    the time rather than which text the object holds now."""
    decision = Decision(
        mandate_id="mg_1", week=3, kind=DecisionKind.NOT_ASKED, value_inr=0.0, reason="before"
    )
    explained = RefusalExplainer().explain_decision(decision, not_worth_asking())
    assert decision.reason == "before"
    assert explained is not decision
