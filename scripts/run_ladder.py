"""T2.1-T2.6 entry point: run every arm of the ladder over the same book.

    uv run python scripts/run_ladder.py --sample
    uv run python scripts/run_ladder.py --budget 5 --budget 50 --budget 500

Fits the hazard (T1.7), projects it forward over the horizon (T2.1a), then runs each arm
through the same harness at each budget. Nothing here decides anything -- every arm sees
the identical book and the identical hazard path, which is the only way "arm X beats arm
Y" is a statement about the arms.

Output is markdown so the numbers get pasted into results.md rather than retyped.
Retyped numbers drift from the data they claim to describe.
"""

from __future__ import annotations

import argparse

import duckdb

from mandateguard.allocator.base import NoAskPolicy
from mandateguard.allocator.baselines import ChronologicalCap, GreedyEV, RoundRobin, bulk_channel
from mandateguard.allocator.mckp import MCKPPolicy
from mandateguard.data.cancel import RENEWAL_TOLERANCE_DAYS
from mandateguard.data.paths import frame_dir, spill_dir
from mandateguard.eval import forecast, world
from mandateguard.policy.loader import load_params
from mandateguard.risk import hazard, scoring


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        action="store_true",
        help="use the sample-derived frame and book",
    )
    parser.add_argument(
        "--budget",
        type=float,
        action="append",
        help="weekly budget in rupees; repeatable. Defaults to horizon.budget_inr_per_week",
    )
    args = parser.parse_args()

    params = load_params()
    split = scoring.split_at(
        params.india.snapshot_date, params.horizon.weeks, RENEWAL_TOLERANCE_DAYS
    )
    frame = frame_dir(args.sample) / "person_periods.parquet"
    book_path = frame_dir(args.sample) / "mandates.parquet"
    for path in (frame, book_path):
        if not path.exists():
            raise SystemExit(
                f"{path} does not exist -- run scripts/build_periods.py and "
                f"scripts/build_mandates.py{' --sample' if args.sample else ''} first."
            )

    channel = bulk_channel(params.channels)
    budgets = args.budget or [params.horizon.budget_inr_per_week]

    print(f"Reading from : {frame.parent}")
    print(f"Horizon      : {params.horizon.weeks} weeks")
    print(
        f"Bulk channel : {channel.name} at INR {channel.cost_inr} (efficacy prior "
        f"{channel.efficacy_prior})"
    )
    print()

    con = duckdb.connect()
    try:
        con.execute(f"SET temp_directory = '{spill_dir().as_posix()}'")
        con.execute(f"CREATE OR REPLACE TEMP VIEW frame AS SELECT * FROM '{frame.as_posix()}'")
        model = hazard.fit(con, "frame", split.train, params.seed, hazard.FIT_ROWS)
        forecast.build(con, model, frame, book_path, params.horizon.weeks)
        print(forecast.format_summary(forecast.summary(con)))
        print()
        book = world.load_book(con)
    finally:
        con.close()

    for budget in budgets:
        arms = [
            NoAskPolicy(),
            ChronologicalCap(params),
            RoundRobin(params),
            GreedyEV(params),
            MCKPPolicy(params),
        ]
        results = [world.run(book, arm, params, budget) for arm in arms]
        slots = int(budget // channel.cost_inr)
        print(f"### Budget INR {budget:,.2f} per week ({slots:,} asks)")
        print()
        print(world.format_metrics(results))
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
