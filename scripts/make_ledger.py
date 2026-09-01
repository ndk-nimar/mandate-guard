"""T5.1 entry point: run one arm over the sample book and write its decision ledger.

    uv run python scripts/make_ledger.py                 # P4 on the committed sample
    uv run python scripts/make_ledger.py --arm P2
    uv run python scripts/make_ledger.py --verify-only   # re-check an existing chain

The output is `data/ledger/<run_id>.jsonl`, gitignored -- a ledger is a record of a run, not
a derived document, and committing one would make every rerun a diff. What CI checks is not
this file but the property: `tests/test_ledger.py` builds a ledger, verifies the chain, and
proves an edit is detected.

The run id is deterministic (`arm`, `snapshot_id`, `seed`, `budget`), so the same run
overwrites nothing and a second invocation *appends to the same chain* rather than starting
a new one beside it. That is the intended behaviour and it is why the script prints the
entry count: a doubled count means the run was recorded twice, which a ledger should show
rather than hide.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from pathlib import Path

import duckdb

from mandateguard.agent.explainer import RefusalExplainer, RefusalFacts, RefusalKind
from mandateguard.allocator.base import NoAskPolicy
from mandateguard.allocator.baselines import ChronologicalCap, GreedyEV, RoundRobin
from mandateguard.allocator.mckp import MCKPPolicy
from mandateguard.allocator.whittle import WhittleIndex
from mandateguard.data.cancel import RENEWAL_TOLERANCE_DAYS
from mandateguard.data.paths import ROOT, frame_dir, spill_dir
from mandateguard.eval import forecast, world
from mandateguard.ledger.store import Ledger, LedgerBroken, build_entry
from mandateguard.models import AllocationResponse, DecisionKind
from mandateguard.policy.loader import load_params, policy_hash
from mandateguard.risk import hazard, scoring

LEDGER_DIR = ROOT / "data" / "ledger"
MODEL_VERSION = "rules-only"
"""No model runs in this path. Recorded as a version rather than left blank, because
"which model decided this" is a question a ledger must answer even when the answer is
"none" -- and blank reads as missing data rather than as an answer."""


def build_arm(name: str, params):
    """The ladder's arms, by name -- the same constructors `run_ladder.py` uses."""
    arms = {
        "P0": lambda: NoAskPolicy(),
        "P1": lambda: ChronologicalCap(params),
        "P2": lambda: RoundRobin(params),
        "P3": lambda: GreedyEV(params),
        "P4": lambda: MCKPPolicy(params),
        "P5": lambda: WhittleIndex(params),
    }
    if name not in arms:
        raise SystemExit(f"unknown arm {name!r}. Known: {', '.join(sorted(arms))}")
    return arms[name]()


def run_id_for(arm: str, snapshot_id: str, seed: int, budget: float) -> str:
    """Deterministic, and it names everything that changes the decisions.

    Budget is in it because the same arm on the same book at a different budget makes
    genuinely different decisions, and a ledger that filed both under one run id could not
    answer which one a replay reproduced.
    """
    return f"{arm}-{snapshot_id}-s{seed}-b{budget:.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", default="P4")
    parser.add_argument("--budget", type=float, default=None)
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument(
        "--sample", action="store_true", help="use the committed 5,079-subscriber slice"
    )
    args = parser.parse_args()

    params = load_params()
    budget = params.horizon.budget_inr_per_week if args.budget is None else args.budget
    snapshot_id = "sample" if args.sample else "full"
    run_id = run_id_for(args.arm, snapshot_id, params.seed, budget)
    path = LEDGER_DIR / f"{run_id}.jsonl"
    ledger = Ledger(path)

    if args.verify_only:
        return _report(ledger, path)

    frame = frame_dir(args.sample) / "person_periods.parquet"
    book_path = frame_dir(args.sample) / "mandates.parquet"
    for needed in (frame, book_path):
        if not needed.exists():
            raise SystemExit(
                f"{needed} does not exist -- run scripts/build_periods.py and "
                f"scripts/build_mandates.py{' --sample' if args.sample else ''} first."
            )
    split = scoring.split_at(
        params.india.snapshot_date, params.horizon.weeks, RENEWAL_TOLERANCE_DAYS
    )
    con = duckdb.connect()
    try:
        con.execute(f"SET temp_directory = '{spill_dir().as_posix()}'")
        con.execute(f"CREATE OR REPLACE TEMP VIEW frame AS SELECT * FROM '{frame.as_posix()}'")
        model = hazard.fit(con, "frame", split.train, params.seed, hazard.FIT_ROWS)
        forecast.build(con, model, frame, book_path, params.horizon.weeks)
        book = world.load_book(con)
    finally:
        con.close()

    policy = build_arm(args.arm, params)
    explainer = RefusalExplainer()  # deterministic; no model, no network
    created_at = datetime.now(UTC).date()
    written = 0

    def sink(week: int, response: AllocationResponse) -> None:
        nonlocal written
        entries = []
        for decision in response.decisions:
            # The explanation goes in its OWN field. Overwriting `decision.reason` with it
            # -- which this script did first -- makes every not-asked row unreplayable:
            # the recorded reason is then a sentence the allocator never produced, and a
            # correct replay reports a mismatch on every one of them.
            explanation = ""
            if decision.kind is DecisionKind.NOT_ASKED:
                explanation = explainer.explain(
                    RefusalFacts(
                        mandate_id=decision.mandate_id,
                        week=week,
                        kind=RefusalKind.FLOOR
                        if args.arm == "P0"
                        else RefusalKind.NOT_WORTH_ASKING,
                    )
                ).text
            entries.append(
                build_entry(
                    run_id=run_id,
                    arm=args.arm,
                    decision=decision,
                    explanation=explanation,
                    policy_hash=policy_hash(),
                    model_version=MODEL_VERSION,
                    seed=params.seed,
                    snapshot_id=snapshot_id,
                    created_at=created_at,
                )
            )
        ledger.extend(entries)
        written += len(entries)

    metrics = world.run(book, policy, params, budget_inr_per_week=budget, sink=sink)
    print(f"{args.arm}: {metrics.asks_spent:,} asks over {metrics.weeks} weeks")
    print(f"wrote {written:,} entries to {path.relative_to(ROOT)}")
    return _report(ledger, path)


def _report(ledger: Ledger, path: Path) -> int:
    try:
        stats = ledger.verify()
    except LedgerBroken as exc:
        print(f"LEDGER BROKEN: {exc}")
        return 1
    except FileNotFoundError:
        print(f"no ledger at {path}")
        return 1
    print(
        f"chain verified: {stats.entries:,} entries, {stats.asked:,} asked, "
        f"{stats.not_asked:,} not asked ({stats.refusal_share:.1%} refusals)"
    )
    print(f"head: {stats.head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
