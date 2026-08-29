"""T1.4 entry point: expand the mandate book into one row per week alive.

    uv run python scripts/build_periods.py
    uv run python scripts/build_periods.py --sample     # the committed 5k CI slice
    uv run python scripts/build_periods.py --dry-run    # counts only, writes nothing

Reads `transactions.parquet` and `members.parquet` from the source directory, writes
`data/processed/person_periods.parquet` -- the frame T1.6's baseline and T1.7's hazard
model are both fit on.

Output is markdown so the numbers get pasted into docs/mapping.md rather than retyped.
Retyped numbers drift from the data they claim to describe.
"""

from __future__ import annotations

import argparse

from mandateguard.data.cancel import RENEWAL_TOLERANCE_DAYS
from mandateguard.data.paths import processed_dir, source_dir
from mandateguard.data.periods import build, format_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the frame's shape without writing person_periods.parquet",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="read the committed 5k-subscriber sample instead of the full tables",
    )
    parser.add_argument(
        "--tolerance-days",
        type=int,
        default=RENEWAL_TOLERANCE_DAYS,
        help="a transaction this soon after coverage ends is a late renewal, not a death",
    )
    args = parser.parse_args()

    interim = source_dir(args.sample)
    print(f"Reading from : {interim}")
    print(f"Writing to   : {'(nothing -- dry run)' if args.dry_run else processed_dir()}")
    print()

    report = build(interim=interim, tolerance_days=args.tolerance_days, write=not args.dry_run)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
