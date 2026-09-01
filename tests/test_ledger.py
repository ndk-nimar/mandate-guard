"""T5.1 -- the append-only decision ledger.

The task's bar is "a full eval run produces a ledger and a test asserts append-only", and
"append-only" is the part worth arguing about. Opening a file in `"a"` mode is a claim about
one process's manners; it is not a property of the file. So the tests below check the
property that is actually worth having: **a change to a written row is detectable, and the
detection names the row.**

The last section runs a real two-week world through the harness and ledgers every decision
it makes, asked and not-asked, which is the "full eval run" half.
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from mandateguard.agent.explainer import RefusalExplainer, RefusalFacts, RefusalKind
from mandateguard.allocator.base import NoAskPolicy
from mandateguard.ledger.store import GENESIS, Ledger, LedgerBroken, build_entry, decision_id
from mandateguard.models import AllocationResponse, Decision, DecisionKind, LedgerEntry
from tests.test_world import BOOK, make_params


def decision(mandate_id: str = "mg_1", week: int = 0, asked: bool = False) -> Decision:
    if asked:
        return Decision(
            mandate_id=mandate_id,
            week=week,
            kind=DecisionKind.ASKED,
            channel="sms",
            value_inr=1.25,
            reason="asking via sms is worth INR 1.25",
            template_id="tmpl_abc123",
        )
    return Decision(
        mandate_id=mandate_id,
        week=week,
        kind=DecisionKind.NOT_ASKED,
        value_inr=0.0,
        reason="Not contacted in week 0. The ask loses INR 0.25.",
    )


def entry(mandate_id: str = "mg_1", week: int = 0, asked: bool = False) -> LedgerEntry:
    return build_entry(
        run_id="P4-sample-s1-b500.00",
        arm="P4",
        decision=decision(mandate_id, week, asked),
        policy_hash="ce28096eeba5ad9d",
        model_version="rules-only",
        seed=1,
        snapshot_id="sample",
        created_at=date(2026, 9, 2),
    )


@pytest.fixture
def ledger(tmp_path) -> Ledger:
    return Ledger(tmp_path / "run.jsonl")


# --------------------------------------------------------------------------------
# The chain.
# --------------------------------------------------------------------------------


def test_an_empty_ledger_starts_at_genesis(ledger):
    """A fixed, obvious first value rather than an empty string, so a truncated file
    cannot pass verification by looking like a fresh one."""
    assert ledger.head == GENESIS
    assert ledger.verify().entries == 0


def test_each_entry_chains_to_the_one_before_it(ledger):
    first = ledger.append(entry("mg_1"))
    second = ledger.append(entry("mg_2"))
    assert first.prev_hash == GENESIS
    assert second.prev_hash == first.entry_hash
    assert ledger.head == second.entry_hash
    assert ledger.verify().entries == 2


def test_append_returns_the_stored_entry_not_the_one_passed_in(ledger):
    """The caller's object has no hashes. Handing it back would give them a record that is
    not what is on disk, and they would file it as though it were."""
    unwritten = entry()
    assert unwritten.entry_hash == ""
    stored = ledger.append(unwritten)
    assert stored.entry_hash
    assert stored is not unwritten
    assert unwritten.entry_hash == ""


def test_editing_a_row_is_detected_and_the_row_is_named(ledger):
    """The property that makes "append-only" mean something. Opening in `"a"` mode stops
    this process from rewriting history and stops nothing else."""
    ledger.extend([entry(f"mg_{i}") for i in range(5)])
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    tampered = json.loads(lines[2])
    tampered["decision"]["reason"] = "Contacted, actually."
    lines[2] = json.dumps(tampered)
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(LedgerBroken, match="line 3"):
        Ledger(ledger.path).verify()


def test_deleting_a_row_from_the_middle_is_detected(ledger):
    ledger.extend([entry(f"mg_{i}") for i in range(5)])
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    del lines[2]
    ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    with pytest.raises(LedgerBroken, match="chain parts here"):
        Ledger(ledger.path).verify()


def test_truncating_the_tail_leaves_a_valid_but_shorter_chain(ledger):
    """Stated rather than hidden: cutting the END of the file is NOT detectable by the
    chain alone, because a prefix of a valid chain is a valid chain. Detecting it needs the
    head published somewhere the file cannot reach -- which this project does not have."""
    ledger.extend([entry(f"mg_{i}") for i in range(5)])
    full_head = ledger.head
    lines = ledger.path.read_text(encoding="utf-8").splitlines()
    ledger.path.write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")

    shortened = Ledger(ledger.path)
    assert shortened.verify().entries == 3
    assert shortened.head != full_head


def test_a_reopened_ledger_continues_the_same_chain(tmp_path):
    """Otherwise a second process appending would start a second chain inside one file and
    every row after the seam would fail verification for the wrong reason."""
    path = tmp_path / "run.jsonl"
    first = Ledger(path)
    first.append(entry("mg_1"))
    head = first.head

    second = Ledger(path)
    assert second.head == head
    second.append(entry("mg_2"))
    assert Ledger(path).verify().entries == 2


def test_the_store_offers_no_way_to_rewrite_history():
    """The API is the guarantee. A `truncate()` or `update()` that sounds routine is how an
    append-only log stops being one."""
    forbidden = {"truncate", "delete", "update", "clear", "rewrite", "remove", "pop"}
    assert not forbidden & set(dir(Ledger))


# --------------------------------------------------------------------------------
# What the entry has to carry.
# --------------------------------------------------------------------------------


def test_an_entry_carries_everything_replay_needs(ledger):
    """T5.2 re-runs a decision under the policy that produced it, which needs all six."""
    stored = ledger.append(entry())
    assert stored.policy_hash
    assert stored.model_version == "rules-only"
    assert stored.seed == 1
    assert stored.snapshot_id == "sample"
    assert stored.arm == "P4"
    assert stored.decision.week == 0


def test_an_asked_decision_carries_its_template_id(ledger):
    """`value/fatigue.py` charges rho_template_reuse on repeated wording, so the ledger has
    to record which wording was used or the penalty cannot be audited."""
    stored = ledger.append(entry(asked=True))
    assert stored.decision.template_id == "tmpl_abc123"


def test_a_not_asked_entry_carries_a_rupee_backed_reason(ledger):
    """T4.5's output landing in T5.1's record -- the whole point of a refusal ledger."""
    stored = ledger.append(entry())
    assert stored.decision.kind is DecisionKind.NOT_ASKED
    assert "INR" in stored.decision.reason


def test_decision_ids_are_unique_per_run_mandate_and_week():
    a = decision_id("P4-sample-s1-b500.00", "mg_1", 3)
    b = decision_id("P4-sample-s1-b900.00", "mg_1", 3)
    assert a != b, "the same mandate at a different budget is a different decision"
    assert a == "P4-sample-s1-b500.00:mg_1:w3"


def test_find_returns_the_entry_or_nothing(ledger):
    ledger.extend([entry("mg_1", week=0), entry("mg_2", week=0)])
    found = ledger.find(decision_id("P4-sample-s1-b500.00", "mg_2", 0))
    assert found is not None and found.decision.mandate_id == "mg_2"
    assert ledger.find("some-other-run:mg_2:w0") is None


# --------------------------------------------------------------------------------
# A full eval run, ledgered. T5.1's other half.
# --------------------------------------------------------------------------------


def test_a_full_eval_run_produces_a_verifiable_ledger(tmp_path):
    """The harness writes every decision it makes, asked and not-asked, through the sink.

    `NoAskPolicy` is used because its ledger is entirely refusals, which is the case that
    matters: a log of what was sent is a message log, and a log of what was declined is the
    thing this project claims to have.
    """
    from mandateguard.eval import world

    params = make_params()
    book = BOOK
    ledger = Ledger(tmp_path / "run.jsonl")
    explainer = RefusalExplainer()

    def sink(week: int, response: AllocationResponse) -> None:
        ledger.extend(
            [
                build_entry(
                    run_id="P0-test-s1-b1.00",
                    arm="P0",
                    decision=explainer.explain_decision(
                        d, RefusalFacts(mandate_id=d.mandate_id, week=week, kind=RefusalKind.FLOOR)
                    ),
                    policy_hash="ce28096eeba5ad9d",
                    model_version="rules-only",
                    seed=params.seed,
                    snapshot_id="test",
                    created_at=date(2026, 9, 2),
                )
                for d in response.decisions
            ]
        )

    world.run(book, NoAskPolicy(), params, sink=sink)

    stats = ledger.verify()
    assert stats.entries == len(book) * params.horizon.weeks
    assert stats.asked == 0
    assert stats.not_asked == stats.entries
    assert stats.refusal_share == 1.0
    assert all("never contacts anyone" in e.decision.reason for e in ledger)


def test_the_sink_does_not_change_the_run(tmp_path):
    """A sink is an observer, not a branch. If ledgering changed a single decision, every
    number in results.md would be about a run nobody could reproduce without it."""
    from mandateguard.eval import world

    params = make_params()
    book = BOOK
    without = world.run(book, NoAskPolicy(), params)
    seen: list[int] = []
    with_sink = world.run(book, NoAskPolicy(), params, sink=lambda w, r: seen.append(w))
    assert without.model_dump() == with_sink.model_dump()
    assert seen == list(range(params.horizon.weeks))
