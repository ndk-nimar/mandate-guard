"""T5.2 -- replay. Re-run a historical decision and check it byte for byte.

The task's bar is "replaying a decision reproduces it byte-identically". Most of this file
is the other half: the four ways a replay can quietly become a *re-decision*, and the
refusal that each one earns. A replay that silently used today's policy, today's seed, or a
book it happened to have would return a plausible answer to a question nobody asked, and
would look exactly like a success.

These run against the three-mandate world from `test_world.py` rather than the sample book,
so the suite stays fast. The real thing is exercised by hand:

    uv run python scripts/make_ledger.py --sample
    uv run mandateguard replay --decision-id "<from that ledger>"

which reproduced 16,248 decisions over 12 weeks in about six seconds.
"""

from __future__ import annotations

from datetime import date

import pytest

from mandateguard.allocator.base import NoAskPolicy
from mandateguard.eval import world
from mandateguard.ledger.replay import ReplayRefused, build_arm, replay
from mandateguard.ledger.store import Ledger, build_entry
from mandateguard.models import AllocationResponse
from mandateguard.policy.loader import Params
from tests.test_world import BOOK, make_params

POLICY_HASH = "ce28096eeba5ad9d"
RUN_ID = "P0-test-s1-b1.00"


def ledger_for(tmp_path, params: Params, *, arm: str = "P0", run_id: str = RUN_ID) -> Ledger:
    """Run the arm over the tiny book and ledger every decision it makes."""
    store = Ledger(tmp_path / "run.jsonl")

    def sink(week: int, response: AllocationResponse) -> None:
        store.extend(
            [
                build_entry(
                    run_id=run_id,
                    arm=arm,
                    decision=d,
                    policy_hash=POLICY_HASH,
                    model_version="rules-only",
                    seed=params.seed,
                    snapshot_id="test",
                    created_at=date(2026, 9, 2),
                )
                for d in response.decisions
            ]
        )

    world.run(BOOK, build_arm(arm, params), params, budget_inr_per_week=1.0, sink=sink)
    return store


def first_id(store: Ledger) -> str:
    return next(iter(store)).decision_id


# --------------------------------------------------------------------------------
# The claim.
# --------------------------------------------------------------------------------


def test_a_recorded_decision_replays_byte_identically(tmp_path):
    params = make_params()
    store = ledger_for(tmp_path, params)
    result = replay(
        store, first_id(store), params=params, current_policy_hash=POLICY_HASH, book=BOOK
    )
    assert result.identical
    assert result.mismatches == []
    assert "reproduced byte-identically" in result.line()


def test_every_decision_in_the_ledger_replays(tmp_path):
    """Not just the first. A replay that only works on week 0 has not reproduced state."""
    params = make_params()
    store = ledger_for(tmp_path, params)
    for entry in store:
        result = replay(
            store,
            entry.decision_id,
            params=params,
            current_policy_hash=POLICY_HASH,
            book=BOOK,
        )
        assert result.identical, result.line()


def test_replay_re_runs_the_whole_run_not_one_mandate(tmp_path):
    """An allocation is not a function of one mandate: this one was not asked partly
    because others were, and week 3's state is the product of weeks 0 to 2."""
    params = make_params()
    store = ledger_for(tmp_path, params)
    result = replay(
        store, first_id(store), params=params, current_policy_hash=POLICY_HASH, book=BOOK
    )
    assert result.weeks_replayed == params.horizon.weeks
    assert result.decisions_replayed == len(BOOK) * params.horizon.weeks


def test_the_comparison_is_on_the_serialised_form(tmp_path):
    """A reason string that drifted by a rounding change is a real difference -- it is what
    the refusal ledger shows a regulator, and a replay tolerating it would certify a
    sentence nobody can reproduce."""
    params = make_params()
    store = ledger_for(tmp_path, params)
    entry = next(iter(store))
    tampered = Ledger(tmp_path / "tampered.jsonl")
    tampered.append(
        entry.model_copy(
            update={
                "prev_hash": "",
                "entry_hash": "",
                "decision": entry.decision.model_copy(update={"reason": "because I said so"}),
            }
        )
    )
    result = replay(
        tampered, entry.decision_id, params=params, current_policy_hash=POLICY_HASH, book=BOOK
    )
    assert not result.identical
    assert [m.field for m in result.mismatches] == ["reason"]
    assert "DIFFERS" in result.line()


# --------------------------------------------------------------------------------
# The four refusals. Each is a way a replay becomes a re-decision.
# --------------------------------------------------------------------------------


def test_a_changed_policy_refuses_rather_than_replaying_under_the_new_rules(tmp_path):
    """This project does not archive old policy files, so the rules that produced the
    decision are simply not in this checkout. T5.3's "policy-hash mismatch halts", early."""
    params = make_params()
    store = ledger_for(tmp_path, params)
    with pytest.raises(ReplayRefused, match="not in this working tree"):
        replay(
            store, first_id(store), params=params, current_policy_hash="0000000000000000", book=BOOK
        )


def test_a_changed_seed_refuses(tmp_path):
    """The hazard fit takes the seed, so a different seed is a different book -- and a
    replay against a different book is a new decision about the same mandate."""
    params = make_params()
    store = ledger_for(tmp_path, params)
    with pytest.raises(ReplayRefused, match="different seed is a different book"):
        replay(
            store,
            first_id(store),
            params=make_params(seed=99),
            current_policy_hash=POLICY_HASH,
            book=BOOK,
        )


def test_an_unknown_decision_id_refuses(tmp_path):
    params = make_params()
    store = ledger_for(tmp_path, params)
    with pytest.raises(ReplayRefused, match="wearing an old name"):
        replay(store, "nope:nope:w0", params=params, current_policy_hash=POLICY_HASH, book=BOOK)


def test_an_arm_this_build_does_not_have_refuses(tmp_path):
    params = make_params()
    store = ledger_for(tmp_path, params, arm="P0", run_id="P9-test-s1-b1.00")
    entry = next(iter(store))
    forged = Ledger(tmp_path / "forged.jsonl")
    forged.append(entry.model_copy(update={"prev_hash": "", "entry_hash": "", "arm": "P9"}))
    with pytest.raises(ReplayRefused, match="this build does not have"):
        replay(forged, entry.decision_id, params=params, current_policy_hash=POLICY_HASH, book=BOOK)


def test_a_run_id_without_a_budget_refuses(tmp_path):
    """The same arm on the same book at a different budget makes different decisions, so a
    replay that guessed the budget would be a guess wearing a byte-identical check."""
    params = make_params()
    store = ledger_for(tmp_path, params, run_id="P0-test-s1")
    with pytest.raises(ReplayRefused, match="does not encode a budget"):
        replay(store, first_id(store), params=params, current_policy_hash=POLICY_HASH, book=BOOK)


def test_a_mandate_missing_from_the_rebuilt_book_refuses(tmp_path):
    """The snapshot named is then not the snapshot it was decided against, and quietly
    returning "not asked" would be inventing a decision for a mandate that is not there."""
    params = make_params()
    store = ledger_for(tmp_path, params)
    with pytest.raises(ReplayRefused, match="not the snapshot"):
        replay(
            store,
            first_id(store),
            params=params,
            current_policy_hash=POLICY_HASH,
            book=[m for m in BOOK if m.mandate_id != next(iter(store)).decision.mandate_id],
        )


# --------------------------------------------------------------------------------
# Small pieces.
# --------------------------------------------------------------------------------


def test_the_budget_is_read_from_the_run_id():
    from mandateguard.ledger.replay import _budget_from_run_id

    assert _budget_from_run_id("P4-sample-s20260905-b500.00") == 500.0
    assert _budget_from_run_id("P4-sample-s1-b1234.50") == 1234.5


def test_every_ladder_arm_is_constructible_by_name():
    """The ledger records an arm as a string; a name the build cannot construct is a
    decision it cannot replay, and that has to fail at the name rather than later."""
    params = make_params()
    for name in ("P0", "P1", "P2", "P3", "P4", "P5"):
        assert build_arm(name, params) is not None
    assert isinstance(build_arm("P0", params), NoAskPolicy)
