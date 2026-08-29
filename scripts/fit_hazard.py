"""T1.7 entry point: fit the discrete-time hazard and score it against the baselines.

    uv run python scripts/fit_hazard.py
    uv run python scripts/fit_hazard.py --sample
    uv run python scripts/fit_hazard.py --plot     # T1.8: reliability diagram as well

Fits a logistic regression on a deterministic subsample of the training slice, then
scores it -- and T1.6's three baselines -- over the whole held-out slice, through the same
`scoring.score`. One table, one code path, so "the model beats the baseline" is a claim
about the models rather than about two scripts.

Output is markdown so the numbers get pasted into docs/eval.md rather than retyped.
Retyped numbers drift from the data they claim to describe.
"""

from __future__ import annotations

import argparse

import duckdb

from mandateguard.data.cancel import RENEWAL_TOLERANCE_DAYS
from mandateguard.data.paths import ROOT, frame_dir, spill_dir
from mandateguard.policy.loader import load_params
from mandateguard.risk import baseline, calibration, hazard, scoring

IMAGES = ROOT / "docs" / "img"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sample",
        action="store_true",
        help="use the sample-derived frame (build_periods.py --sample writes it)",
    )
    parser.add_argument(
        "--plot",
        action="store_true",
        help="also write the T1.8 reliability diagram to docs/img/",
    )
    parser.add_argument(
        "--rows",
        type=int,
        default=hazard.FIT_ROWS,
        help="target size of the training subsample",
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
        model = hazard.fit(con, "frame", split.train, params.seed, args.rows)
        print(hazard.format_model(model))
        print()

        reference = scoring.score(con, "frame", str(rate), split.test, "`base_rate`")
        scores = [
            reference,
            scoring.score(con, "frame", baseline.rule_expression(), split.test, "`expiry_rule`"),
            scoring.score(con, "frame", bins.expression, split.test, "`expiry_bins`"),
            scoring.score(con, "frame", model.expression, split.test, "**`hazard`**"),
        ]
        print(scoring.format_scores(scores, reference))

        curves = [
            calibration.reliability(con, "frame", model.expression, split.test, "hazard"),
            calibration.reliability(con, "frame", bins.expression, split.test, "expiry_bins"),
        ]
        print()
        for curve in curves:
            print(calibration.format_reliability(curve))
            print()
        if args.plot:
            written = calibration.plot(curves, IMAGES / "reliability.png")
            print(f"Reliability diagram written to `{written.relative_to(ROOT)}`.")
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
