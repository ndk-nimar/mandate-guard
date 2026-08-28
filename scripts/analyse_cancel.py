"""T1.2 entry point: measure what `is_cancel` means, print it as markdown.

    uv run python scripts/analyse_cancel.py

Runs over `data/interim/transactions.parquet` and, for the sentinel counts only, also
over `transactions_v2.parquet` -- the two tables are never merged here, because merging
them is a T1.3 modelling decision and this script only measures.

Output is markdown so the numbers get pasted into docs/mapping.md rather than retyped.
Retyped numbers drift from the data they claim to describe.
"""

from __future__ import annotations

import argparse

from mandateguard.data.cancel import (
    RENEWAL_TOLERANCE_DAYS,
    analyse,
    analyse_lapses,
    format_lapse_report,
    format_report,
)
from mandateguard.data.paths import interim_dir


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--grace-days",
        type=int,
        default=None,
        help="repurchase within this many days counts as a plan change, not a recovery",
    )
    parser.add_argument(
        "--tolerance-days",
        type=int,
        default=RENEWAL_TOLERANCE_DAYS,
        help="a transaction this soon after coverage ends is a late renewal, not a lapse",
    )
    args = parser.parse_args()

    interim = interim_dir()
    print(f"Reading from : {interim}")
    print()

    transactions = interim / "transactions.parquet"

    kwargs = {"grace_days": args.grace_days} if args.grace_days is not None else {}
    print("## Active death -- recovery after a cancellation (upper bound on `r`)")
    print()
    print(
        format_report(
            analyse(
                transactions,
                also={"transactions_v2": interim / "transactions_v2.parquet"},
                **kwargs,
            )
        )
    )
    print()
    print("## Passive death -- recovery after coverage ran out (`q`)")
    print()
    print(format_lapse_report(analyse_lapses(transactions, tolerance_days=args.tolerance_days)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
