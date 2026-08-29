"""T1.3 entry point: build the mandate book and print who survived into it.

    uv run python scripts/build_mandates.py
    uv run python scripts/build_mandates.py --sample    # the committed 5k CI slice
    uv run python scripts/build_mandates.py --dry-run   # counts only, writes nothing

Reads `data/interim/transactions.parquet` and `members.parquet`, writes
`data/processed/mandates.parquet`. `transactions_v2` is deliberately not read -- see
docs/mapping.md 3.2 for why the two tables are not merged.

Output is markdown so the numbers get pasted into docs/mapping.md rather than retyped.
Retyped numbers drift from the data they claim to describe.
"""

from __future__ import annotations

import argparse

from mandateguard.data.mandates import build, format_report
from mandateguard.data.paths import processed_dir, source_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the filter chain without writing mandates.parquet",
    )
    parser.add_argument(
        "--sample",
        action="store_true",
        help="read the committed 5k-subscriber sample instead of the full tables",
    )
    args = parser.parse_args()

    interim = source_dir(args.sample)
    print(f"Reading from : {interim}")
    print(f"Writing to   : {'(nothing -- dry run)' if args.dry_run else processed_dir()}")
    print()

    print(format_report(build(interim=interim, write=not args.dry_run)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
