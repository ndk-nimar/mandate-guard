"""T1.1 entry point: KKBox CSVs -> typed parquet in data/interim/.

    uv run python scripts/ingest.py
    uv run python scripts/ingest.py --limit 100000   # smoke run, seconds not minutes

Prints the row counts and date ranges that T1.1 requires to be recorded in
docs/mapping.md, already formatted as markdown so the numbers get pasted rather
than retyped (retyped numbers drift from the data they claim to describe).
"""

from __future__ import annotations

import argparse

from mandateguard.data.ingest import format_report, ingest_all
from mandateguard.data.paths import interim_dir, raw_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="rows per table; omit for all")
    args = parser.parse_args()

    print(f"Reading from : {raw_dir()}")
    print(f"Writing to   : {interim_dir()}")
    if args.limit:
        print(f"LIMIT {args.limit:,} per table -- this is a smoke run, not the real thing")
    print()

    print(format_report(ingest_all(args.limit)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
