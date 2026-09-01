"""T5.2 -- re-run a historical decision from its ledger entry.

    uv run mandateguard replay --decision-id "P4-sample-s20260905-b500.00:mg_1:w3"

### What "replay one decision" has to mean here

A single allocation is not a function of a single mandate. `P4` solves a knapsack over the
whole week's book at a budget, so *this* mandate was not asked partly because *those* ones
were, and re-deciding it in isolation would produce a different answer for a correct reason.
Worse, week 3's state -- how many asks each customer has already had, how much of each
mandate is still alive -- is the product of weeks 0 to 2.

So replay re-runs **the whole run**, deterministically, from the six fields the entry
carries, and extracts the decision that was asked for. That is slower and it is the only
version that is actually a replay. Re-deciding one mandate against today's book would be a
new decision that happens to concern the same mandate.

### The four things checked before anything is re-run

Each is a way a replay can silently become a re-decision, and each fails loudly instead:

1. **Policy hash.** If `policy/mandate_policy.yaml` has changed since the decision, the
   rules that produced it no longer exist in this checkout. This project does not archive
   old policy files, so the honest answer is refusal rather than a replay under a different
   rulebook. That is also T5.3's "policy-hash mismatch halts and alerts", arriving early.
2. **Snapshot.** The entry names the book it was decided against; `eval/snapshot.py`
   rebuilds it. An id this build cannot rebuild is a decision it cannot replay.
3. **Seed.** The hazard fit takes it. A different seed is a different book.
4. **Arm.** The allocator that made the decision, by name.

### The comparison is byte-for-byte

Not "the same verdict" and not "close enough on the rupee number": the recomputed
`Decision` is serialised and compared to the stored one as bytes. A reason string that
drifted by a rounding change is a real difference, because the reason string is what the
refusal ledger shows a regulator, and a replay that tolerated it would be certifying a
sentence nobody can reproduce.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mandateguard.allocator.base import NoAskPolicy, Policy
from mandateguard.allocator.baselines import ChronologicalCap, GreedyEV, RoundRobin
from mandateguard.allocator.mckp import MCKPPolicy
from mandateguard.allocator.whittle import WhittleIndex
from mandateguard.eval import snapshot, world
from mandateguard.ledger.store import Ledger
from mandateguard.models import AllocationResponse, Decision, LedgerEntry
from mandateguard.policy.loader import Params, load_params, policy_hash

__all__ = ["ReplayMismatch", "ReplayRefused", "ReplayResult", "build_arm", "replay"]


class ReplayRefused(RuntimeError):
    """The decision cannot be replayed, and saying so beats replaying something else."""


class ReplayMismatch(BaseModel):
    model_config = ConfigDict(frozen=True)

    field: str
    recorded: str
    recomputed: str


class ReplayResult(BaseModel):
    """What came back, and whether it is the same thing."""

    model_config = ConfigDict(frozen=True)

    decision_id: str
    recorded: Decision
    recomputed: Decision
    identical: bool
    mismatches: list[ReplayMismatch] = Field(default_factory=list)
    weeks_replayed: int = Field(ge=0)
    decisions_replayed: int = Field(ge=0)

    def line(self) -> str:
        if self.identical:
            return (
                f"{self.decision_id}: reproduced byte-identically "
                f"({self.decisions_replayed:,} decisions over {self.weeks_replayed} weeks)"
            )
        detail = "; ".join(
            f"{m.field}: {m.recorded!r} -> {m.recomputed!r}" for m in self.mismatches
        )
        return f"{self.decision_id}: DIFFERS -- {detail}"


ARMS: dict[str, type[Policy]] = {
    "P0": NoAskPolicy,
    "P1": ChronologicalCap,
    "P2": RoundRobin,
    "P3": GreedyEV,
    "P4": MCKPPolicy,
    "P5": WhittleIndex,
}


def build_arm(name: str, params: Params) -> Policy:
    if name not in ARMS:
        raise ReplayRefused(
            f"the ledger names arm {name!r}, which this build does not have. Known: "
            f"{', '.join(sorted(ARMS))}."
        )
    return ARMS[name]() if name == "P0" else ARMS[name](params)  # type: ignore[call-arg]


def _budget_from_run_id(run_id: str) -> float:
    """The budget is encoded in the run id by `scripts/make_ledger.py`.

    Parsed rather than stored as its own field because a second copy of a number is a second
    thing that can disagree with the first, and the run id already has to be unique across
    budgets for the decision id to mean anything.
    """
    for part in reversed(run_id.split("-")):
        if part.startswith("b"):
            try:
                return float(part[1:])
            except ValueError:
                break
    raise ReplayRefused(
        f"run id {run_id!r} does not encode a budget (expected a `-b<amount>` part). The "
        "same arm on the same book at a different budget makes different decisions, so a "
        "replay without one would be a guess."
    )


def replay(
    ledger: Ledger,
    decision_id: str,
    *,
    params: Params | None = None,
    current_policy_hash: str | None = None,
    book: list[world.BookMandate] | None = None,
) -> ReplayResult:
    """Re-run the decision's whole run and compare the one decision, byte for byte.

    `book` is an injection point for tests and for a caller that already holds the snapshot.
    Passing one skips the rebuild -- and skips the snapshot-id check with it, because the
    caller has asserted which book this is. That is a real trapdoor and it is why the
    parameter is keyword-only and not exposed on the CLI: the command line always rebuilds
    from the id, so a replay a reviewer runs cannot be handed the wrong book.
    """
    entry = ledger.find(decision_id)
    if entry is None:
        raise ReplayRefused(
            f"no entry with decision id {decision_id!r} in {ledger.path.name}. A replay of a "
            "decision that was never recorded is a new decision wearing an old name."
        )

    params = params or load_params()
    _check_replayable(entry, params, current_policy_hash)

    book = snapshot.load_snapshot(entry.snapshot_id, params) if book is None else book
    arm = build_arm(entry.arm, params)
    budget = _budget_from_run_id(entry.run_id)

    found: list[Decision] = []
    counted = {"weeks": 0, "decisions": 0}

    def sink(week: int, response: AllocationResponse) -> None:
        counted["weeks"] += 1
        counted["decisions"] += len(response.decisions)
        if week != entry.decision.week:
            return
        for decision in response.decisions:
            if decision.mandate_id == entry.decision.mandate_id:
                found.append(decision)

    world.run(book, arm, params, budget_inr_per_week=budget, sink=sink)

    if not found:
        raise ReplayRefused(
            f"{decision_id}: the replayed run produced no decision for mandate "
            f"{entry.decision.mandate_id!r} in week {entry.decision.week}. The mandate is "
            "not in the rebuilt book, so the snapshot this entry names is not the snapshot "
            "it was decided against."
        )

    recomputed = found[0]
    mismatches = _compare(entry.decision, recomputed)
    return ReplayResult(
        decision_id=decision_id,
        recorded=entry.decision,
        recomputed=recomputed,
        identical=not mismatches,
        mismatches=mismatches,
        weeks_replayed=counted["weeks"],
        decisions_replayed=counted["decisions"],
    )


def _check_replayable(entry: LedgerEntry, params: Params, current_policy_hash: str | None) -> None:
    current = policy_hash() if current_policy_hash is None else current_policy_hash
    if entry.policy_hash and entry.policy_hash != current:
        raise ReplayRefused(
            f"{entry.decision_id}: recorded under policy {entry.policy_hash}, but this "
            f"checkout carries {current}. The rules that produced this decision are not in "
            "this working tree, and no archive of old policy files exists here. Replaying "
            "under a different rulebook would produce a plausible answer to a question "
            "nobody asked."
        )
    if entry.seed != params.seed:
        raise ReplayRefused(
            f"{entry.decision_id}: recorded at seed {entry.seed}, but config/params.yaml "
            f"now carries {params.seed}. The hazard fit takes the seed, so a different seed "
            "is a different book."
        )


def _compare(recorded: Decision, recomputed: Decision) -> list[ReplayMismatch]:
    """Field by field, on the serialised form.

    Compared as JSON rather than as Python objects so that a float which prints differently
    is caught: the reason string is what a regulator is shown, and 'INR 0.25' against
    'INR 0.250000000001' is a real difference even though the floats compare equal to a
    tolerance nobody wrote down.
    """
    left = recorded.model_dump(mode="json")
    right = recomputed.model_dump(mode="json")
    return [
        ReplayMismatch(field=field, recorded=repr(left[field]), recomputed=repr(right[field]))
        for field in sorted(left)
        if left[field] != right[field]
    ]
