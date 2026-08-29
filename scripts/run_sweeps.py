"""T2.7 and T2.8 entry point: the budget curve and the sensitivity plane.

    uv run python scripts/run_sweeps.py --sample --plot

Both sweeps share one expensive setup -- fit the hazard, project it forward, load the
book -- so they live in one script rather than paying for it twice.

Output is markdown so the numbers get pasted into results.md rather than retyped.
Retyped numbers drift from the data they claim to describe.
"""

from __future__ import annotations

import argparse

import duckdb

from mandateguard.allocator.baselines import bulk_channel
from mandateguard.data.cancel import RENEWAL_TOLERANCE_DAYS
from mandateguard.data.paths import ROOT, frame_dir, spill_dir
from mandateguard.eval import forecast, sweep, world
from mandateguard.policy.loader import load_params
from mandateguard.risk import hazard, scoring

IMAGES = ROOT / "docs" / "img"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sample", action="store_true", help="use the sample-derived frame")
    parser.add_argument("--plot", action="store_true", help="write docs/img/sweeps.png")
    parser.add_argument(
        "--grid-budget",
        type=float,
        default=None,
        help="weekly budget the grid runs at (default: enough for one ask per mandate)",
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
            raise SystemExit(f"{path} does not exist -- build it first (see CLAUDE.md §5).")

    con = duckdb.connect()
    try:
        con.execute(f"SET temp_directory = '{spill_dir().as_posix()}'")
        con.execute(f"CREATE OR REPLACE TEMP VIEW frame AS SELECT * FROM '{frame.as_posix()}'")
        model = hazard.fit(con, "frame", split.train, params.seed, hazard.FIT_ROWS)
        forecast.build(con, model, frame, book_path, params.horizon.weeks)
        book = world.load_book(con)
    finally:
        con.close()

    channel = bulk_channel(params.channels)
    saturation = channel.cost_inr * len(book)
    grid_budget = args.grid_budget if args.grid_budget is not None else saturation

    print(f"Book         : {len(book):,} live mandates, {params.horizon.weeks}-week horizon")
    print(f"Bulk channel : {channel.name} at INR {channel.cost_inr}")
    print(f"Saturation   : INR {saturation:,.2f}/week buys one ask per mandate")
    print()

    print("## T2.7 Budget sweep")
    print()
    budgets = sweep.budget_ladder(channel.cost_inr, len(book))
    sweeps = sweep.budget_sweep(book, params, budgets)
    print(sweep.format_sweep(sweeps))
    print()
    print("| budget | " + " | ".join(s.arm for s in sweeps) + " |")
    print("|---:|" + "---:|" * len(sweeps))
    for index, budget in enumerate(budgets):
        cells = " | ".join(f"{s.points[index].metrics.profit_inr:,.0f}" for s in sweeps)
        print(f"| {budget:,.2f} | {cells} |")
    print()

    print("## T2.8 Sensitivity grid")
    print()
    grid = sweep.sensitivity_grid(
        book, params, list(sweep.UPLIFT_SCALES), list(sweep.BACKFIRE_RATES), grid_budget
    )
    print(sweep.format_grid(grid))
    print()

    if args.plot:
        written = sweep.plot(sweeps, grid, IMAGES / "sweeps.png")
        print(f"Written to `{written.relative_to(ROOT)}`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
