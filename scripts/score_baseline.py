"""T1.6 entry point: score the naive baselines, before the real model exists.

    uv run python scripts/score_baseline.py
    uv run python scripts/score_baseline.py --sample

Reads `person_periods.parquet` from `data/processed/` (built by `build_periods.py`, or
built on the fly from the committed sample when `--sample` is passed), splits it out of
time, fits the baselines on the training slice, and scores all three on the held-out one.

Output is markdown so the numbers get pasted into docs/eval.md rather than retyped.
Retyped numbers drift from the data they claim to describe.
"""

from __future__ import annotations

import argparse

import duckdb

from mandateguard.data.cancel import RENEWAL_TOLERANCE_DAYS
from mandateguard.data.paths import frame_dir, spill_dir
from mandateguard.policy.loader import load_params
from mandateguard.risk import baseline, scoring


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        action="store_true",
        help="score the sample-derived frame (build_periods.py --sample writes it)",
    )
    args = parser.parse_args()

    params = load_params()
    split = scoring.split_at(
        params.india.snapshot_date, params.horizon.weeks, RENEWAL_TOLERANCE_DAYS
    )
    frame = frame_dir(args.sample) / "person_periods.parquet"
    if not frame.exists():
        raise SystemExit(
            f"{frame} does not exist -- run scripts/build_periods.py"
            f"{' --sample' if args.sample else ''} first."
        )

    print(f"Reading from : {frame}")
    print(f"Split        : train < {split.cutoff} <= test ({split.weeks} weeks held out)")
    print(f"Scoreable    : week_start <= {split.last_observable}")
    print()

    con = duckdb.connect()
    try:
        con.execute(f"SET temp_directory = '{spill_dir().as_posix()}'")
        con.execute(f"CREATE OR REPLACE TEMP VIEW frame AS SELECT * FROM '{frame.as_posix()}'")

        rate = baseline.constant(con, "frame", split.train)
        bins = baseline.fit_bins(con, "frame", split.train)
        print(f"Training death rate per person-week: **{rate:.4f}**")
        print()
        print(baseline.format_bins(bins))
        print()

        reference = scoring.score(con, "frame", str(rate), split.test, "`base_rate`")
        scores = [
            reference,
            scoring.score(con, "frame", baseline.rule_expression(), split.test, "`expiry_rule`"),
            scoring.score(con, "frame", bins.expression, split.test, "`expiry_bins`"),
        ]
        print(scoring.format_scores(scores, reference))
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
