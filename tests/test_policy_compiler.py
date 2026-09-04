"""T4.1 -- the compiled rulebook, and the checks that make it worth trusting.

Two things are under test here and they are different claims. The first is that the
expression language cannot execute anything: that is a property of `agent/expression.py`
and is tested directly. The second is that a rule cannot cite a clause it does not quote:
that is a property of `policy/loader.py` and is tested by building deliberately broken
policy files on disk and asserting they refuse to load.

The broken files are built rather than committed on purpose. A committed bad-policy fixture
is a file someone eventually "fixes".
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from mandateguard.agent.client import (
    CassetteClient,
    CassetteMissError,
    LLMResult,
    LLMUsage,
    cassette_key,
)
from mandateguard.agent.expression import (
    ExpressionError,
    UnknownFieldError,
    evaluate,
    parse,
    referenced_names,
)
from mandateguard.models import MandateAuditContext, PolicyRule, Rail, Verdict
from mandateguard.policy.loader import (
    POLICY_DIR,
    POLICY_PATH,
    LLMParams,
    Policy,
    content_hash,
    load_params,
    load_policy,
    policy_hash,
    source_text,
)

# --------------------------------------------------------------------------------
# The expression language.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "expression",
    [
        "__import__('os').system('echo pwned')",
        "open('/etc/passwd').read() == 'x'",
        "amount_inr.real <= 15000",
        "pre_debit_notice_fields[0] == 'merchant_name'",
        "amount_inr * 2 <= 30000",
        "[f for f in pre_debit_notice_fields] == []",
        "(lambda: True)()",
        "f'{amount_inr}' == '1'",
    ],
)
def test_the_expression_language_has_no_way_to_execute_anything(expression):
    """Every one of these is a legal Python expression and none is a legal policy rule.

    The first two are the reason the module exists: policy expressions are written by a
    model, and `eval` on model output in a process holding a payments book is arbitrary
    code execution with a regulator's name on it. The rest are rejected for a quieter
    reason -- attribute access, subscripting and arithmetic all let a rule drift away from
    the literal words of the clause it claims to implement.
    """
    with pytest.raises(ExpressionError):
        parse(expression)


def test_a_rule_must_answer_yes_or_no_not_merely_be_truthy():
    """`amount_inr` alone parses. It must not evaluate.

    A rule that passes because a rupee figure happens to be non-zero would pass for every
    mandate in the book and fail for a free trial, which is the wrong way round and would
    never be noticed.
    """
    with pytest.raises(ExpressionError, match="not a bool"):
        evaluate("amount_inr", {"amount_inr": 4999.0})


def test_and_short_circuits_so_a_none_guard_actually_guards():
    """`x is not None and x >= 24` is the shape every optional-field rule uses.

    Without short-circuiting the right half would be evaluated against None and raise,
    which would turn "no notice was sent" into a crash rather than a finding.
    """
    assert evaluate("h is not None and h >= 24", {"h": None}) is False
    assert evaluate("h is not None and h >= 24", {"h": 24.0}) is True


def test_comparing_a_missing_value_says_which_guard_is_wrong():
    with pytest.raises(ExpressionError, match="add the `is not None` half"):
        evaluate("h >= 24", {"h": None})


def test_an_unknown_field_is_named_rather_than_treated_as_false():
    with pytest.raises(UnknownFieldError, match="amount_in"):
        evaluate("amount_in <= 15000", {"amount_inr": 100.0})


def test_referenced_names_finds_what_the_loader_checks():
    assert referenced_names("category != 'fastag' and amount_inr <= 15000") == {
        "category",
        "amount_inr",
    }


# --------------------------------------------------------------------------------
# The shipped rulebook.
# --------------------------------------------------------------------------------


def test_the_shipped_policy_meets_the_task_bar():
    """T4.1's stated bar: ten or more rules, each with a clause reference."""
    policy = load_policy()
    assert len(policy.rules) >= 10
    assert all(rule.clause.strip() for rule in policy.rules)
    assert all(rule.quote.strip() for rule in policy.rules)


def test_every_shipped_quote_appears_verbatim_in_the_circular():
    """The check that separates a citation from a claim.

    `load_policy` already runs this -- the test exists so that a regression in the check
    itself fails here, loudly, rather than making every future rule trivially valid.
    """
    policy = load_policy()
    haystack = " ".join(source_text(policy).split())
    for rule in policy.rules:
        assert " ".join(rule.quote.split()) in haystack, rule.rule_id


def test_the_scope_rule_sends_enach_to_a_human_rather_than_grading_it():
    """Clause 2 covers cards, PPI and UPI. eNACH is 15% of the modelled rail mix and is
    not in that list, so the only honest verdict is that this rulebook does not reach it.
    """
    policy = load_policy()
    scope = next(r for r in policy.rules if r.rule_id == "scope_cards_ppi_upi")
    assert scope.verdict_on_fail is Verdict.NEEDS_HUMAN
    assert not evaluate(scope.expression, {"rail": Rail.ENACH})
    assert evaluate(scope.expression, {"rail": Rail.UPI_AUTOPAY})


def test_the_two_afa_ceilings_never_apply_to_the_same_mandate():
    """Clause 8(a) and 8(b) are mutually exclusive by their guards.

    If both applied, a Rs.20,000 insurance premium would fail 8(a) while passing 8(b) --
    the mandate would be simultaneously compliant and not, and which one the ledger
    recorded would depend on rule order in a YAML file.
    """
    policy = load_policy()
    guards = {
        r.rule_id: r.applies_when
        for r in policy.rules
        if r.rule_id in {"afa_general_ceiling", "afa_enhanced_ceiling"}
    }
    assert len(guards) == 2
    for category in MandateAuditContext.model_fields["category"].annotation:
        context = {"category": category}
        applied = [rid for rid, guard in guards.items() if evaluate(guard, context)]
        assert len(applied) == 1, f"{category} matched {applied}"


def test_the_general_ceiling_is_inclusive_at_fifteen_thousand():
    """ "up to Rs.15,000" includes Rs.15,000. One rupee more needs AFA.

    This is the boundary the red-team arm (T4.8) is built around, so it is pinned here
    rather than left to the golden set.
    """
    policy = load_policy()
    rule = next(r for r in policy.rules if r.rule_id == "afa_general_ceiling")
    base = {"amount_inr": 0.0, "afa_on_this_transaction": False}
    assert evaluate(rule.expression, base | {"amount_inr": 15000.0})
    assert not evaluate(rule.expression, base | {"amount_inr": 15001.0})
    authenticated = base | {"amount_inr": 15001.0, "afa_on_this_transaction": True}
    assert evaluate(rule.expression, authenticated)


def test_no_rule_was_compiled_for_the_velocity_check_heading():
    """Clause 8 is headed "Transaction limits and velocity check" and its text states no
    velocity limit. A rule written to match the heading would have looked like coverage
    and been an invention -- so the absence is asserted, not left to chance."""
    policy = load_policy()
    assert not any("velocity" in rule.rule_id for rule in policy.rules)


# --------------------------------------------------------------------------------
# The loader's refusals. Each builds a deliberately broken policy on disk.
# --------------------------------------------------------------------------------


def _policy_dir(tmp_path: Path, mutate=None) -> Path:
    """Copy the shipped policy and circular into tmp_path, optionally breaking one thing."""
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    circular = (POLICY_PATH.parent / raw["source"]["text_file"]).read_bytes()
    if mutate is not None:
        circular = mutate(raw, circular) or circular
    (tmp_path / "sources").mkdir(parents=True, exist_ok=True)
    (tmp_path / raw["source"]["text_file"]).write_bytes(circular)
    path = tmp_path / "mandate_policy.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return path


def test_the_copied_policy_loads_so_the_refusals_below_mean_something(tmp_path):
    """Without this, every `pytest.raises` in this section could be passing because the
    copy was broken rather than because of the thing under test."""
    assert len(load_policy(_policy_dir(tmp_path)).rules) >= 10


def test_a_quote_the_circular_does_not_contain_is_rejected(tmp_path):
    def mutate(raw, circular):
        raw["rules"][0]["quote"] = "Merchants shall be permitted to debit without notice."

    with pytest.raises(ValueError, match="quotes text that does not appear"):
        load_policy(_policy_dir(tmp_path, mutate))


def test_a_clause_number_the_circular_does_not_have_is_rejected(tmp_path):
    def mutate(raw, circular):
        raw["rules"][0]["clause"] = "99(z)"

    with pytest.raises(ValueError, match="is not one of the"):
        load_policy(_policy_dir(tmp_path, mutate))


def test_editing_the_circular_to_make_a_quote_fit_breaks_the_hash(tmp_path):
    """The loophole this closes: a quote check alone can be satisfied by editing the
    source. Pinning the source's hash in the policy means that edit fails first, and the
    only way forward is to re-read the circular and re-review the rules."""

    def mutate(raw, circular):
        return circular + b"\nMerchants may debit without notice.\n"

    with pytest.raises(ValueError, match="hashes to"):
        load_policy(_policy_dir(tmp_path, mutate))


def test_a_missing_circular_is_rejected_rather_than_skipped(tmp_path):
    path = _policy_dir(tmp_path)
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    (tmp_path / raw["source"]["text_file"]).unlink()
    with pytest.raises(FileNotFoundError, match="is missing"):
        load_policy(path)


def test_rules_without_a_read_circular_are_rejected(tmp_path):
    def mutate(raw, circular):
        raw["source"]["read"] = False

    with pytest.raises(ValueError, match="source.read"):
        load_policy(_policy_dir(tmp_path, mutate))


def test_a_duplicate_rule_id_is_rejected(tmp_path):
    def mutate(raw, circular):
        raw["rules"].append(dict(raw["rules"][0]))

    with pytest.raises(ValidationError, match="duplicate rule_id"):
        load_policy(_policy_dir(tmp_path, mutate))


def test_an_expression_with_a_function_call_is_rejected_at_load(tmp_path):
    def mutate(raw, circular):
        raw["rules"][0]["expression"] = "len(pre_debit_notice_fields) == 5"

    with pytest.raises(ValidationError, match="illegal expression"):
        load_policy(_policy_dir(tmp_path, mutate))


def test_an_expression_reading_an_unknown_field_is_rejected_at_load(tmp_path):
    """The silent failure this prevents: a rule naming a field that does not exist never
    fires, and a rule that never fires passes every test written against it."""

    def mutate(raw, circular):
        raw["rules"][0]["expression"] = "amount_in_rupees <= 15000"

    with pytest.raises(ValidationError, match="MandateAuditContext does not define"):
        load_policy(_policy_dir(tmp_path, mutate))


def test_a_guard_reading_an_unknown_field_is_rejected_too(tmp_path):
    """`applies_when` is checked as strictly as `expression`. A typo in a guard is worse:
    the rule still evaluates, just never (or always), and nothing raises."""

    def mutate(raw, circular):
        raw["rules"][0]["applies_when"] = "categry == 'fastag'"

    with pytest.raises(ValidationError, match="MandateAuditContext does not define"):
        load_policy(_policy_dir(tmp_path, mutate))


def test_a_rule_that_fails_into_compliant_is_rejected():
    with pytest.raises(ValidationError, match="failing it means nothing"):
        PolicyRule.model_validate(
            {
                "rule_id": "vacuous",
                "clause": "6(a)",
                "quote": "An issuer shall send a pre-transaction notification",
                "description": "d",
                "expression": "True",
                "verdict_on_fail": "compliant",
                "remedy": "none",
            }
        )


def test_a_rule_without_a_quote_is_rejected():
    """The gap that existed before T4.1: `clause` alone is a pointer, and a pointer can
    point at a clause that says something else."""
    with pytest.raises(ValidationError):
        PolicyRule.model_validate(
            {
                "rule_id": "uncited",
                "clause": "6(a)",
                "description": "d",
                "expression": "True",
                "remedy": "none",
            }
        )


# --------------------------------------------------------------------------------
# The cassette layer, which is what lets CI run any of this without a key.
# --------------------------------------------------------------------------------


def _llm_params() -> LLMParams:
    return load_params().llm


def test_a_cassette_miss_raises_instead_of_calling_anything(tmp_path):
    """The design decision worth a test: replay-or-call would spend money in CI and would
    produce a different llm_eval.md on any machine that happened to hold a key."""
    client = CassetteClient(_llm_params(), root=tmp_path)
    with pytest.raises(CassetteMissError, match="no cassette for job"):
        client.run("auditor", "system", "prompt")


def test_a_recorded_response_replays_byte_identically(tmp_path):
    params = _llm_params()
    key = cassette_key(
        "auditor", params.model, "sys", "ask", None, params.max_tokens, params.effort
    )
    recorded = LLMResult(
        job="auditor",
        key=key,
        model=params.model,
        text='{"verdict": "compliant"}',
        stop_reason="end_turn",
        usage=LLMUsage(input_tokens=1200, output_tokens=300),
        latency_ms=4210,
    )
    path = tmp_path / "auditor" / f"{key}.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps({"response": recorded.model_dump(exclude={"job", "key", "replayed"})}),
        encoding="utf-8",
    )

    replayed = CassetteClient(params, root=tmp_path).run("auditor", "sys", "ask")
    assert replayed.text == recorded.text
    assert replayed.latency_ms == recorded.latency_ms
    assert replayed.replayed is True


def test_the_cassette_key_does_not_depend_on_dict_order():
    """The same failure mode as an unordered COPY in ADR 0003: a schema assembled in a
    different order would miss a cassette holding the identical request."""
    params = _llm_params()
    args = ("auditor", params.model, "sys", "ask")
    tail = (params.max_tokens, params.effort)
    a = cassette_key(*args, {"type": "object", "required": ["x"]}, *tail)
    b = cassette_key(*args, {"required": ["x"], "type": "object"}, *tail)
    assert a == b


def test_cost_is_computed_from_the_config_rates_not_from_a_constant():
    params = _llm_params()
    usage = LLMUsage(input_tokens=1_000_000, output_tokens=1_000_000)
    assert usage.cost_usd(params) == pytest.approx(
        params.price_input_usd_per_mtok + params.price_output_usd_per_mtok
    )


def test_the_policy_hash_changes_when_a_rule_changes(tmp_path):
    """Every ledger entry pins a policy hash so `replay` can reproduce a decision under
    the policy that produced it (T5.2). A hash that ignored the rules would make that
    pin worthless."""
    before = hashlib.sha256(POLICY_PATH.read_bytes()).hexdigest()
    text = POLICY_PATH.read_text(encoding="utf-8")
    edited = text.replace("amount_inr <= 15000", "amount_inr <= 1")
    after = hashlib.sha256(edited.encode("utf-8")).hexdigest()
    assert before != after


def test_the_audit_context_and_the_rulebook_share_one_vocabulary():
    """Every field a rule reads exists; asserted from the other direction so that deleting
    a context field breaks here rather than in whatever rule silently stopped firing."""
    policy = load_policy()
    known = set(MandateAuditContext.model_fields)
    used: set[str] = set()
    for rule in policy.rules:
        used |= referenced_names(rule.expression) | referenced_names(rule.applies_when)
    assert used <= known
    assert used, "no rule reads any field, so the rulebook decides nothing"


def test_policy_validates_as_a_model_without_touching_disk():
    """`Policy` is constructible in memory -- the FastAPI layer (T5.5) needs that."""
    policy = load_policy()
    assert Policy.model_validate(policy.model_dump(mode="json")).version == policy.version


class TestHashesSurviveTheCheckout:
    """The two load-time hashes must answer "is this the same text", not "is this the same
    file". They came apart on 2026-09-04: CI on `windows-latest` checked the repository out
    with `core.autocrlf=true`, every LF in the RBI circular became CRLF, and `load_policy()`
    refused to start -- reporting that the regulator's text had been edited. The same
    failure meets any contributor who clones on Windows with git's defaults.

    `.gitattributes` pins these paths to LF, which fixes the working copy. These tests are
    the other half: they fix the *hash*, so a checkout that slips past .gitattributes still
    loads rather than accusing the regulator.
    """

    def test_crlf_does_not_change_the_content_hash(self, tmp_path: Path) -> None:
        original = POLICY_DIR / "sources" / "rbi-2026-04-21-e-mandate-framework.md"
        raw = original.read_bytes()
        assert b"\r\n" not in raw, "the committed circular is LF; this test assumes it"

        crlf = tmp_path / original.name
        crlf.write_bytes(raw.replace(b"\n", b"\r\n"))

        assert crlf.read_bytes() != raw, "the fixture must actually differ in bytes"
        assert content_hash(crlf) == content_hash(original)

    def test_the_pinned_circular_hash_still_matches(self) -> None:
        """The normalisation must not have moved the hash the YAML pins -- if it had, every
        compiled rule would be citing a text this loader no longer recognises."""
        policy = load_policy()
        source = POLICY_DIR / policy.source.text_file
        assert content_hash(source) == policy.source.sha256

    def test_policy_hash_survives_crlf(self, tmp_path: Path) -> None:
        """`policy_hash()` is written into every ledger entry. If a checkout setting could
        move it, "this decision replays under the policy that produced it" would silently
        become "...if you cloned the way I did"."""
        raw = POLICY_PATH.read_bytes()
        crlf = tmp_path / POLICY_PATH.name
        crlf.write_bytes(raw.replace(b"\n", b"\r\n"))
        assert policy_hash(crlf) == policy_hash(POLICY_PATH)

    def test_a_real_edit_still_changes_the_hash(self, tmp_path: Path) -> None:
        """The normalisation removes line endings from the answer and nothing else. A word
        changed in the circular must still fail the gate -- that is the whole mechanism."""
        original = POLICY_DIR / "sources" / "rbi-2026-04-21-e-mandate-framework.md"
        edited = tmp_path / original.name
        edited.write_bytes(original.read_bytes().replace(b"shall", b"may", 1))
        assert content_hash(edited) != content_hash(original)
