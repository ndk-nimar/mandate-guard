"""T1.5 entry point: cut the committed CI sample out of the full interim tables.

    uv run python scripts/build_sample.py

Reads `data/interim/*.parquet` (which lives outside the repo -- see
`src/mandateguard/data/paths.py`) and writes `data/sample/*.parquet`, which is committed.
The file names match, which is the whole trick: every downstream script takes `--sample`
and swaps the directory rather than taking a second code path.

Output is markdown so the numbers get pasted into docs/mapping.md rather than retyped.
Retyped numbers drift from the data they claim to describe.
"""

from __future__ import annotations

import argparse

from mandateguard.data.paths import interim_dir, sample_dir
from mandateguard.data.sample import TARGET_SUBSCRIBERS, build, format_report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target",
        type=int,
        default=TARGET_SUBSCRIBERS,
        help="how many subscribers to keep (the realised count lands near it, not on it)",
    )
    args = parser.parse_args()

    print(f"Reading from : {interim_dir()}")
    print(f"Writing to   : {sample_dir()}")
    print()

    report = build(target=args.target)
    print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
