"""T5.1 -- the append-only decision ledger. Every decision, asked and **not** asked.

### Why not-asked decisions are the point

A message scheduler logs what it sent. This logs what it *declined to send*, with the rupee
number that decided it and the clause or budget that produced that number. `docs/problem.md`
§3 makes the case: the interesting output of a rationing system is the rationing, and a
system that cannot show its refusals cannot be audited for the harm it avoided or the
customers it quietly abandoned.

`eval/world.py` already enforces the totality that makes this possible -- an arm returning
fewer decisions than mandates is a contract violation, not a shorthand.

### Append-only, and what that actually costs to mean

"Append-only" is easy to claim: open the file in `"a"` mode and never truncate. That stops
this process from rewriting history and stops nothing else. A ledger a payments engineer
takes seriously has to make tampering *detectable*, so each entry carries:

    entry_hash = sha256(prev_hash + canonical_json(entry without its own hashes))

which chains every row to the one before it. Editing row 40 of a 16,000-row file changes
row 40's hash, which breaks row 41's `prev_hash`, and `verify()` names the first row where
the chain parts. Deleting a row does the same. Appending a forged row at the end requires
the previous hash, which is in the file -- so the chain is not tamper-*proof* against
someone who rewrites the whole file, and it is not claimed to be. It is tamper-*evident*
against exactly the edit someone actually makes: the quiet one, to one row, later.

Three properties this deliberately does not have, so nobody expects them:

* **It is not signed.** Anyone who can rewrite the file can rewrite the chain. Signing needs
  a key this project does not have and would not know where to keep.
* **It is not durable against a crash mid-write.** One `write` per entry with a flush is
  what a hackathon can honestly claim; a torn last line shows up as a verification failure
  at the tail rather than as silent corruption, which is the right way round.
* **It is not concurrent.** One writer. Two processes appending interleave lines and the
  chain breaks, loudly, on the next verify.

### Replay (T5.2) needs more than the decision

An entry therefore also carries `policy_hash`, `model_version`, `seed`, `snapshot_id`, the
arm, and the week. Those six are what let a decision be re-run under the policy that
produced it rather than under today's policy -- which is the difference between a replay and
a re-decision.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mandateguard.models import Decision, DecisionKind, LedgerEntry

GENESIS = "0" * 64
"""What the first entry's `prev_hash` is. A fixed, obvious value rather than an empty
string, so a truncated file cannot pass verification by looking like a fresh one."""

__all__ = ["GENESIS", "Ledger", "LedgerBroken", "entry_hash"]


class LedgerBroken(RuntimeError):
    """The chain does not hold. Carries the row and what disagreed."""


def _canonical(entry: LedgerEntry) -> str:
    """The bytes a hash is taken over: the entry minus its own two hash fields.

    `sort_keys=True` for the same reason every other serialisation here sorts (ADR 0003):
    a dict rendered in a different field order hashes differently, and the chain would
    break on a Python version change rather than on an edit.
    """
    payload = json.loads(entry.model_dump_json(exclude={"prev_hash", "entry_hash"}))
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def entry_hash(entry: LedgerEntry, prev_hash: str) -> str:
    return hashlib.sha256((prev_hash + _canonical(entry)).encode("utf-8")).hexdigest()


class LedgerStats(BaseModel):
    """What a run produced, for the eval documents to quote."""

    model_config = ConfigDict(frozen=True)

    entries: int = Field(ge=0)
    asked: int = Field(ge=0)
    not_asked: int = Field(ge=0)
    head: str = Field(description="the last entry's hash; the ledger's identity")

    @property
    def refusal_share(self) -> float:
        return self.not_asked / self.entries if self.entries else 0.0


class Ledger:
    """One append-only JSONL file, chained.

    Opened lazily and in `"a"` mode only. There is no method on this class that truncates,
    rewrites or deletes, and that absence is the API: a caller who wants to start over
    deletes the file themselves, visibly, rather than calling something that sounds routine.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._head: str | None = None

    # ---------------------------------------------------------------- reading

    def __iter__(self) -> Iterator[LedgerEntry]:
        if not self.path.is_file():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield LedgerEntry.model_validate_json(line)

    def __len__(self) -> int:
        return sum(1 for _ in self)

    @property
    def head(self) -> str:
        """The last entry's hash, or GENESIS for an empty ledger.

        Read from the file on first use rather than cached at construction, so that a
        `Ledger` pointed at a file another process already wrote continues its chain
        instead of starting a second one inside the same file.
        """
        if self._head is None:
            last = GENESIS
            for entry in self:
                last = entry.entry_hash
            self._head = last
        return self._head

    def find(self, decision_id: str) -> LedgerEntry | None:
        """The entry for one decision. Linear: a ledger is a log, not an index.

        Named `find` rather than `get` because it can legitimately return nothing -- a
        decision id from another run is a miss, not an error.
        """
        for entry in self:
            if entry.decision_id == decision_id:
                return entry
        return None

    # ---------------------------------------------------------------- writing

    def append(self, entry: LedgerEntry) -> LedgerEntry:
        """Chain the entry to the current head and write it. Returns the stored entry.

        The returned object is not the one passed in: `prev_hash` and `entry_hash` are
        filled here, and a caller holding the original would hold a record that is not what
        is on disk.
        """
        prev = self.head
        chained = entry.model_copy(update={"prev_hash": prev})
        chained = chained.model_copy(update={"entry_hash": entry_hash(chained, prev)})

        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(chained.model_dump_json() + "\n")
            handle.flush()
        self._head = chained.entry_hash
        return chained

    def extend(self, entries: list[LedgerEntry]) -> list[LedgerEntry]:
        """Append many, one open. Same chain, same order, no batching semantics."""
        return [self.append(entry) for entry in entries]

    # ---------------------------------------------------------------- checking

    def verify(self) -> LedgerStats:
        """Walk the chain. Raises `LedgerBroken` at the first row that does not hold.

        Returns stats rather than a bare bool so that the one call a caller makes anyway
        also answers "how many refusals did this run record", which is the number the
        refusal ledger exists to produce.
        """
        prev = GENESIS
        asked = 0
        not_asked = 0
        for index, entry in enumerate(self):
            if entry.prev_hash != prev:
                raise LedgerBroken(
                    f"{self.path.name} line {index + 1} ({entry.decision_id!r}) follows "
                    f"{entry.prev_hash[:12]}... but the previous entry hashes to "
                    f"{prev[:12]}.... The chain parts here: a row before this one was "
                    "edited or removed."
                )
            recomputed = entry_hash(entry, prev)
            if recomputed != entry.entry_hash:
                raise LedgerBroken(
                    f"{self.path.name} line {index + 1} ({entry.decision_id!r}) carries "
                    f"hash {entry.entry_hash[:12]}... but its contents hash to "
                    f"{recomputed[:12]}.... This row was edited after it was written."
                )
            prev = entry.entry_hash
            if entry.decision.kind is DecisionKind.ASKED:
                asked += 1
            else:
                not_asked += 1
        return LedgerStats(entries=asked + not_asked, asked=asked, not_asked=not_asked, head=prev)


def decision_id(run_id: str, mandate_id: str, week: int) -> str:
    """The identity `replay --decision-id` takes.

    `run_id` is in it because the same mandate in the same week under a different arm or a
    different budget is a different decision, and a ledger holding both under one id could
    not answer which one was replayed.
    """
    return f"{run_id}:{mandate_id}:w{week}"


def build_entry(
    *,
    run_id: str,
    arm: str,
    decision: Decision,
    policy_hash: str,
    model_version: str,
    seed: int,
    snapshot_id: str,
    created_at,
    explanation: str = "",
) -> LedgerEntry:
    """Assemble one entry. The hashes are left to `Ledger.append`."""
    return LedgerEntry(
        decision_id=decision_id(run_id, decision.mandate_id, decision.week),
        run_id=run_id,
        arm=arm,
        decision=decision,
        explanation=explanation,
        policy_hash=policy_hash,
        model_version=model_version,
        seed=seed,
        snapshot_id=snapshot_id,
        created_at=created_at,
    )
