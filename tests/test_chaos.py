"""T5.4 -- chaos. Kill the model, corrupt the policy, feed nulls.

The task's bar is "the system must degrade, not crash", and the whole file turns on what
those two words are allowed to mean here.

**Degrading** is one of exactly three things: a typed refusal the caller can record
(`Authorisation`, `needs_human`, an escalated notice), a documented exception of this
project's own (`LedgerBroken`, `ReplayRefused`, `CassetteMissError`, `Halted`,
`ExpressionError`), or a `ValueError`/`ValidationError` whose message names what went wrong.

**Crashing** is an `AttributeError`, `KeyError`, `IndexError`, `TypeError`,
`ZeroDivisionError` or a bare `Exception` from inside a module -- the family that means
nobody thought about this input. `assert_degrades` below encodes that distinction, and it is
the only assertion most of these tests make, because "it did not blow up in an unplanned way"
is the actual claim.

Some of these pass for a boring reason -- Pydantic rejects the input at the boundary. That
is a real answer and it is the design working: the models are the boundary. Where a check
lives is worth knowing, so the tests say which layer caught each one.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal

import pytest
import yaml
from pydantic import ValidationError

from mandateguard.agent.auditor import MandateAuditor
from mandateguard.agent.client import CassetteClient, CassetteMissError, LLMResult, LLMUsage
from mandateguard.agent.explainer import RefusalExplainer, RefusalFacts, RefusalKind
from mandateguard.agent.expression import ExpressionError, evaluate
from mandateguard.agent.linter import NoticeFacts, lint_notice
from mandateguard.agent.notice import LLMComposer, TemplateComposer
from mandateguard.allocator.base import NoAskPolicy
from mandateguard.eval import world
from mandateguard.ledger.replay import ReplayRefused, replay
from mandateguard.ledger.store import Ledger, LedgerBroken, build_entry
from mandateguard.models import MandateAuditContext, MandateCategory, Rail, Verdict
from mandateguard.policy.loader import POLICY_PATH, load_params, load_policy
from mandateguard.safety.guard import Action, ActionKind, Degradation, Guard, Halted
from tests.test_world import BOOK, make_params

CRASHES = (AttributeError, KeyError, IndexError, TypeError, ZeroDivisionError, RecursionError)
DEGRADATIONS = (
    ValueError,  # ValidationError and ExpressionError are both ValueError subclasses
    LedgerBroken,
    ReplayRefused,
    CassetteMissError,
    Halted,
    FileNotFoundError,
    RuntimeError,
)


def assert_degrades(fn, *args, **kwargs):
    """Call it. Either it returns, or it raises something explained. Never a crash."""
    try:
        return fn(*args, **kwargs)
    except CRASHES as exc:  # pragma: no cover -- the failure this file exists to catch
        raise AssertionError(
            f"{fn} crashed with {type(exc).__name__}: {exc}. That is the family that means "
            "nobody thought about this input."
        ) from exc
    except DEGRADATIONS as exc:
        assert str(exc).strip(), f"{type(exc).__name__} was raised with no message"
        return exc


# --------------------------------------------------------------------------------
# 1. Kill the model.
# --------------------------------------------------------------------------------


class DeadClient:
    """Raises on every call, the way a network does."""

    def run(self, job, system, prompt, *, schema=None):
        raise ConnectionError("connection reset by peer")


class GarbageClient:
    """Returns 200 OK and nonsense, which is the harder failure."""

    def __init__(self, text: str = "\x00\x01 not json at all") -> None:
        self.text = text

    def run(self, job, system, prompt, *, schema=None):
        return LLMResult(
            job=job,
            key="chaos",
            model="chaos",
            text=self.text,
            stop_reason="end_turn",
            usage=LLMUsage(input_tokens=0, output_tokens=0),
            latency_ms=0,
        )


def test_the_auditor_still_judges_when_the_model_is_gone():
    """The rules path needs no model at all, which is why "LLM down leads to rules-only"
    is the ordinary path with a step removed rather than an emergency branch."""
    context = MandateAuditContext(mandate_id="mg_1", rail=Rail.UPI_AUTOPAY, amount_inr=499.0)
    verdict = MandateAuditor(client=None).audit_context(context)
    assert verdict.verdict in set(Verdict)
    assert verdict.reason


def test_a_dead_client_raises_a_connection_error_rather_than_a_wrong_verdict():
    """Deliberate: a transport failure is not the auditor's to swallow. Swallowing it into
    `needs_human` would make a network blip indistinguishable from a mandate the record
    genuinely could not settle, and T4.7's abstain metrics would silently measure uptime."""
    auditor = MandateAuditor(client=DeadClient())
    with pytest.raises(ConnectionError):
        auditor.audit_record("mg_1", "some record")


def test_garbage_from_the_model_abstains_rather_than_being_parsed():
    verdict = assert_degrades(MandateAuditor(client=GarbageClient()).audit_record, "mg_1", "rec")
    assert verdict.verdict is Verdict.NEEDS_HUMAN
    assert "not JSON" in verdict.reason


@pytest.mark.parametrize(
    "text",
    [
        "",
        "null",
        "[]",
        '{"context": null, "unknown_fields": []}',
        '{"context": {"rail": "swift"}, "unknown_fields": []}',
        '{"context": {}, "unknown_fields": "not-a-list"}',
        "{" * 500,
    ],
)
def test_every_shape_of_bad_extraction_abstains(text):
    verdict = assert_degrades(
        MandateAuditor(client=GarbageClient(text)).audit_record, "mg_1", "rec"
    )
    assert verdict.verdict is Verdict.NEEDS_HUMAN


def test_the_notice_composer_escalates_rather_than_sending_garbage():
    facts = NoticeFacts(
        mandate_id="mg_1",
        merchant_name="Hoichoi",
        amount_inr=Decimal("499.00"),
        notice_at=datetime(2026, 9, 1, 9, 0),
        debit_at=datetime(2026, 9, 3, 20, 30),
        mandate_reference="MND-1",
        reason="Monthly subscription",
        opt_out_url="https://bank.example/stop",
        grievance_contact="grievance@bank.example",
    )
    composed = LLMComposer(GarbageClient()).compose(facts)
    assert composed.escalated
    assert not composed.sendable
    assert composed.fallback_text, "the deterministic notice must still be available"
    assert lint_notice(composed.fallback_text, facts).passed


def test_the_explainer_falls_back_to_the_deterministic_sentence():
    facts = RefusalFacts(mandate_id="mg_1", week=0, kind=RefusalKind.FLOOR)
    explanation = RefusalExplainer(client=GarbageClient()).explain(facts)
    assert explanation.source == "deterministic"
    assert explanation.text


def test_a_cassette_miss_is_named_rather_than_silently_calling_out(tmp_path):
    client = CassetteClient(load_params().llm, root=tmp_path)
    exc = assert_degrades(client.run, "auditor", "sys", "prompt")
    assert isinstance(exc, CassetteMissError)
    assert "no cassette for job" in str(exc)


def test_the_guard_records_the_model_being_gone_as_a_rung():
    guard = Guard(load_params().safety, kill_switch_file=None or _nowhere())
    guard.mark_llm_unavailable("connection reset by peer")
    assert guard.state()[0] is Degradation.RULES_ONLY
    assert not guard.authorise(Action(kind=ActionKind.MODEL_CALL)).allowed
    assert guard.authorise(Action(kind=ActionKind.CONTACT, cost_inr=1.0)).allowed


def _nowhere():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp()) / "KILL"


# --------------------------------------------------------------------------------
# 2. Corrupt the policy file.
# --------------------------------------------------------------------------------


def _corrupt(tmp_path, mutate) -> object:
    """Write a broken policy beside a copy of the circular and try to load it."""
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    circular = (POLICY_PATH.parent / raw["source"]["text_file"]).read_bytes()
    circular = mutate(raw, circular) or circular
    (tmp_path / "sources").mkdir(parents=True, exist_ok=True)
    (tmp_path / raw["source"]["text_file"]).write_bytes(circular)
    path = tmp_path / "mandate_policy.yaml"
    if isinstance(raw, str):
        path.write_text(raw, encoding="utf-8")
    else:
        path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return assert_degrades(load_policy, path)


def test_a_truncated_policy_file_is_named_not_crashed(tmp_path):
    path = tmp_path / "mandate_policy.yaml"
    path.write_text("version: 1\nsource:\n  name: half a file\n", encoding="utf-8")
    exc = assert_degrades(load_policy, path)
    assert isinstance(exc, ValidationError)


def test_an_empty_policy_file_is_named(tmp_path):
    path = tmp_path / "mandate_policy.yaml"
    path.write_text("", encoding="utf-8")
    assert_degrades(load_policy, path)


def test_unparseable_yaml_is_named(tmp_path):
    path = tmp_path / "mandate_policy.yaml"
    path.write_text("rules: [unclosed\n  - {{{\n", encoding="utf-8")
    try:
        load_policy(path)
    except yaml.YAMLError as exc:
        assert str(exc).strip()
    except CRASHES as exc:  # pragma: no cover
        raise AssertionError(f"crashed with {type(exc).__name__}") from exc


def test_a_rule_with_a_forged_quote_is_refused(tmp_path):
    def mutate(raw, circular):
        raw["rules"][0]["quote"] = "Merchants may debit whatever they like."

    exc = _corrupt(tmp_path, mutate)
    assert "does not appear" in str(exc)


def test_a_rule_with_an_injected_expression_is_refused(tmp_path):
    """The attack the expression whitelist exists for, driven through the loader."""

    def mutate(raw, circular):
        raw["rules"][0]["expression"] = "__import__('os').system('echo pwned') == 0"

    exc = _corrupt(tmp_path, mutate)
    assert "illegal expression" in str(exc)


def test_a_tampered_circular_is_refused_by_the_hash(tmp_path):
    def mutate(raw, circular):
        return circular.replace(b"24 hours", b"0 hours")

    exc = _corrupt(tmp_path, mutate)
    assert "hashes to" in str(exc)


def test_a_missing_circular_is_named(tmp_path):
    raw = yaml.safe_load(POLICY_PATH.read_text(encoding="utf-8"))
    path = tmp_path / "mandate_policy.yaml"
    path.write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True), encoding="utf-8")
    exc = assert_degrades(load_policy, path)
    assert isinstance(exc, FileNotFoundError)


def test_a_policy_change_under_a_running_system_halts_it():
    guard = Guard(load_params().safety, expected_policy_hash="deadbeefdeadbeef")
    state, why = guard.state()
    assert state is Degradation.HALTED
    assert "policy hash mismatch" in why
    with pytest.raises(Halted):
        guard.require_running()


# --------------------------------------------------------------------------------
# 3. Feed nulls.
# --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"mandate_id": None},
        {"mandate_id": "x", "rail": None},
        {"mandate_id": "x", "rail": "upi_autopay", "amount_inr": None},
        {"mandate_id": "x", "rail": "upi_autopay", "amount_inr": -1.0},
        {"mandate_id": "x", "rail": "upi_autopay", "amount_inr": float("nan")},
        {"mandate_id": "x", "rail": "upi_autopay", "pre_debit_notice_fields": None},
    ],
)
def test_a_null_riddled_audit_context_is_rejected_at_the_boundary(payload):
    """Caught by Pydantic, which is the design: the models are the boundary, and a null
    that reaches a rule expression is a null that got past the wrong layer."""
    exc = assert_degrades(MandateAuditContext.model_validate, payload)
    assert isinstance(exc, ValidationError)


def test_a_none_that_does_reach_an_expression_is_explained_not_crashed():
    """Some fields are legitimately `None` -- `pre_debit_notice_hours` when no notice was
    sent. A rule comparing one without its guard must say which guard is wrong."""
    exc = assert_degrades(evaluate, "h >= 24", {"h": None})
    assert isinstance(exc, ExpressionError)
    assert "add the `is not None` half" in str(exc)


def test_an_empty_book_runs_rather_than_dividing_by_zero():
    metrics = assert_degrades(world.run, [], NoAskPolicy(), make_params())
    assert metrics.mandates == 0
    assert metrics.retention_rate == 0.0
    assert metrics.inr_per_ask == 0.0


def test_a_zero_budget_runs():
    metrics = assert_degrades(
        world.run, BOOK, NoAskPolicy(), make_params(), budget_inr_per_week=0.0
    )
    assert metrics.asks_spent == 0


def test_an_empty_notice_is_rejected_with_every_missing_field_named():
    facts = NoticeFacts(
        mandate_id="mg_1",
        merchant_name="Hoichoi",
        amount_inr=Decimal("499.00"),
        notice_at=datetime(2026, 9, 1, 9, 0),
        debit_at=datetime(2026, 9, 3, 20, 30),
        mandate_reference="MND-1",
        reason="Monthly subscription",
        opt_out_url="https://bank.example/stop",
        grievance_contact="grievance@bank.example",
    )
    report = lint_notice("", facts)
    assert not report.passed
    assert len(report.findings) >= 5


def test_a_notice_of_nothing_but_control_characters_is_rejected():
    facts = TemplateComposer()  # noqa: F841 -- constructing it is part of the check
    from tests.test_notice import facts as notice_facts

    report = lint_notice("\x00\x01\x02\n\n\t", notice_facts())
    assert not report.passed


def test_an_empty_ledger_verifies_rather_than_erroring(tmp_path):
    stats = assert_degrades(Ledger(tmp_path / "empty.jsonl").verify)
    assert stats.entries == 0


def test_a_ledger_of_blank_lines_verifies_as_empty(tmp_path):
    path = tmp_path / "blank.jsonl"
    path.write_text("\n\n   \n", encoding="utf-8")
    assert Ledger(path).verify().entries == 0


def test_a_ledger_line_that_is_not_json_is_named(tmp_path):
    path = tmp_path / "bad.jsonl"
    path.write_text("this is not json\n", encoding="utf-8")
    exc = assert_degrades(Ledger(path).verify)
    assert isinstance(exc, ValidationError)


def test_a_ledger_line_missing_its_fields_is_named(tmp_path):
    path = tmp_path / "partial.jsonl"
    path.write_text(json.dumps({"decision_id": "x"}) + "\n", encoding="utf-8")
    assert_degrades(Ledger(path).verify)


def test_replaying_from_an_empty_ledger_refuses(tmp_path):
    exc = assert_degrades(replay, Ledger(tmp_path / "empty.jsonl"), "anything:at:all")
    assert isinstance(exc, ReplayRefused)


def test_a_ledger_entry_with_an_empty_snapshot_id_refuses_rather_than_guessing(tmp_path):
    """An entry that does not name its book cannot be replayed against one. Picking a
    default would produce a decision about a book the entry never mentioned."""
    from mandateguard.models import Decision, DecisionKind

    store = Ledger(tmp_path / "run.jsonl")
    params = make_params()
    store.append(
        build_entry(
            run_id="P0-x-s1-b1.00",
            arm="P0",
            decision=Decision(
                mandate_id="a-doomed",
                week=0,
                kind=DecisionKind.NOT_ASKED,
                value_inr=0.0,
                reason="r",
            ),
            policy_hash="",
            model_version="rules-only",
            seed=params.seed,
            snapshot_id="",
            created_at=date(2026, 9, 2),
        )
    )
    exc = assert_degrades(
        replay, store, "P0-x-s1-b1.00:a-doomed:w0", params=params, current_policy_hash=""
    )
    assert isinstance(exc, ValueError)
    assert "unknown snapshot" in str(exc)


def test_an_audit_of_the_emptiest_legal_mandate_still_produces_a_verdict():
    """Every optional field at its default. The auditor must still name clauses."""
    context = MandateAuditContext(mandate_id="", rail=Rail.CARD, amount_inr=0.0)
    verdict = MandateAuditor(client=None).audit_context(context)
    assert verdict.verdict is not None
    assert verdict.reason
    if verdict.verdict is not Verdict.COMPLIANT:
        assert verdict.citations


def test_the_auditor_survives_every_rail_and_category_combination():
    """20 rules times 24 combinations. A guard that raised on one of them would be a
    `needs_human` for a reason nobody could explain."""
    auditor = MandateAuditor(client=None)
    for rail in Rail:
        for category in MandateCategory:
            verdict = assert_degrades(
                auditor.audit_context,
                MandateAuditContext(
                    mandate_id="mg", rail=rail, category=category, amount_inr=999.0
                ),
            )
            assert verdict.verdict in set(Verdict)
