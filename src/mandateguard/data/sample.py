"""T1.5 -- cut a small, committed slice of the data that CI can reproduce results from.

The full download is ~1 GB of parquet and GitHub Actions is never going to fetch it. So
every number this project regenerates in CI comes from `data/sample/`, which is committed
and small enough to read in a diff.

Three decisions make this a sample rather than a truncation:

**The unit is the subscriber, not the row.** Taking the first N rows, or a random N% of
rows, would hand back subscribers with three of their nine transactions -- and a mandate
with a partial history is not a smaller mandate, it is a corrupted one. Its coverage
timeline has holes that never existed, so it lapses in the frame and did not lapse in
life. Every row of every sampled subscriber comes along.

**Membership is a salted hash of the id, not a random draw.** `hash(msno || salt)` is a
property of the key, so the sample is stable without an RNG whose state would have to be
threaded through every caller to stay reproducible. Re-running this script on the same
download reproduces the same 5,000 subscribers, byte for byte.

**The draw is uniform, and deliberately not stratified.** Topping the sample up with
subscribers carrying rare sentinels would exercise more code paths, at the cost of making
every rate computed on the sample wrong in a way nobody could see. The sample is uniform;
`SampleReport` states which rare cases it happened to catch and which it missed, and the
branches it misses are covered by the hand-built fixtures in `tests/` instead. A sample
is for reproducing the pipeline, not for re-deriving the population.

    uv run python scripts/build_sample.py
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from pydantic import BaseModel

from mandateguard.data.cancel import EPOCH, FAR_FUTURE_AFTER
from mandateguard.data.paths import ensure, interim_dir, sample_dir

TARGET_SUBSCRIBERS = 5_000
"""How many subscribers to keep.

Not in `config/params.yaml`: that file holds calibration parameters that a reader is
invited to sweep, and this is not one. Changing it changes committed data, so it should
be a deliberate code change with a diff, not a config tweak that silently invalidates
every committed result."""

HASH_BUCKETS = 1_000_000
"""Resolution of the membership test. At a ~0.2% sampling rate, 100k buckets would
quantise the rate to the nearest 0.001 and miss the target by hundreds of subscribers."""

SAMPLE_SALT = "sample"
"""Salt mixed into the id before hashing, so membership here is independent of every
other hash of the same key -- see `mandates.RAIL_SALT` for the bug that made this
necessary. A sample selected by the low buckets of a bare `hash(msno)` is not a random
subscriber sample: it is the set of subscribers that every *other* bare hash of `msno`
also puts in its lowest bucket."""

TABLES = ("transactions", "transactions_v2", "members", "labels")
"""Every interim table, filtered to the sampled subscribers and written under the same
file names. That is what lets `--sample` be a directory swap rather than a second code
path: `build(interim=sample_dir())` reads `transactions.parquet` either way."""


class TableSlice(BaseModel):
    name: str
    rows: int
    subscribers: int
    kilobytes: float
    share_of_source: float

    @property
    def line(self) -> str:
        return (
            f"| `{self.name}` | {self.rows:,} | {self.subscribers:,} | "
            f"{self.kilobytes:,.0f} KB | {self.share_of_source:.3%} |"
        )


class Coverage(BaseModel):
    """One thing the pipeline can do, and whether the sample contains a case of it.

    A sample that silently lacks a case is worse than one that lacks it loudly: the CI
    run stays green and nobody learns that the branch was never entered.
    """

    case: str
    matters_for: str
    rows: int

    @property
    def line(self) -> str:
        found = f"{self.rows:,}" if self.rows else "**none**"
        return f"| {self.case} | {self.matters_for} | {found} |"


class SampleReport(BaseModel):
    target: int
    subscribers: int
    source_subscribers: int
    slices: list[TableSlice]
    coverage: list[Coverage]

    @property
    def megabytes(self) -> float:
        return round(sum(s.kilobytes for s in self.slices) / 1000, 2)

    @property
    def missing(self) -> list[Coverage]:
        """Cases the uniform draw did not catch. Reported, not fixed by topping up."""
        return [c for c in self.coverage if c.rows == 0]


def _cutoff(source_subscribers: int, target: int) -> int:
    """Hash-bucket threshold that lands near `target` subscribers.

    Derived from the population rather than hard-coded, so that the same target keeps
    meaning the same thing if the download is ever refreshed. The realised count will not
    be exactly `target` -- hash buckets do not divide evenly -- and the report states what
    it actually was rather than rounding the claim.
    """
    return max(1, round(target / source_subscribers * HASH_BUCKETS))


# Each probe names a branch the pipeline actually has, so that a zero here reads as
# "CI never enters that branch" rather than as a curiosity about the data. The two
# sentinel thresholds are imported from `cancel.py` rather than restated: a second
# definition of "far future" living here would drift from 2.7's the first time either
# moved, and the coverage table would then be answering a different question than the
# one it names.
PROBES: list[tuple[str, str, str]] = [
    (
        "cancellations (`is_cancel`)",
        "the active-death rate `r` (mapping.md 2.3)",
        "SELECT count(*) FROM tx WHERE is_cancel",
    ),
    (
        "one-off purchases (`is_auto_renew = 0`)",
        "the auto-renew filter, which drops 41% of the book (3.7)",
        "SELECT count(*) FROM tx WHERE NOT is_auto_renew",
    ),
    (
        "zero-day plans (`payment_plan_days = 0`)",
        "the debit-frequency imputation chain (3.5)",
        "SELECT count(*) FROM tx WHERE payment_plan_days = 0",
    ),
    (
        "free rows (`actual_amount_paid = 0`)",
        "the amount fallback to list price and typical payment (3.5)",
        "SELECT count(*) FROM tx WHERE actual_amount_paid = 0",
    ),
    (
        "epoch expiry (1970-01-01)",
        "the missing-coverage-end filter (2.7, 3.7)",
        f"SELECT count(*) FROM tx WHERE membership_expire_date = DATE '{EPOCH}'",
    ),
    (
        # Deliberately against `tx_v2`: 2.7 measured that the far-future expiries live
        # only in `transactions_v2`. Probing `transactions` for them would report a
        # confident zero and blame the sample for a property of the source table.
        f"far-future expiry (past {FAR_FUTURE_AFTER}, `transactions_v2`)",
        "the implausible-cycle bound on imputation (3.5)",
        f"SELECT count(*) FROM tx_v2 WHERE membership_expire_date > DATE '{FAR_FUTURE_AFTER}'",
    ),
    (
        "subscribers with no `members` row",
        "the LEFT join that keeps a quarter of the book alive (3.7)",
        "SELECT count(*) FROM (SELECT DISTINCT msno FROM tx) t ANTI JOIN members m USING (msno)",
    ),
    (
        "implausible `bd` (age)",
        "the age-nulling rule (3.6)",
        "SELECT count(*) FROM members WHERE bd IS NOT NULL AND bd NOT BETWEEN 13 AND 90",
    ),
]


def build(
    interim: Path | None = None,
    out_dir: Path | None = None,
    target: int = TARGET_SUBSCRIBERS,
) -> SampleReport:
    """Write every interim table, filtered to a deterministic subscriber sample."""
    interim = interim or interim_dir()
    out_dir = ensure(out_dir or sample_dir())

    con = duckdb.connect()
    try:
        source = (interim / "transactions.parquet").as_posix()
        source_subscribers = _count(con, f"SELECT count(DISTINCT msno) FROM '{source}'")
        cutoff = _cutoff(source_subscribers, target)

        # The membership test lives in one place and every table reuses it, so a table
        # cannot end up holding a subscriber the others do not.
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE chosen AS
            SELECT DISTINCT msno FROM '{source}'
            WHERE hash(msno || '{SAMPLE_SALT}') % {HASH_BUCKETS} < {cutoff}
            """
        )
        subscribers = _count(con, "SELECT count(*) FROM chosen")

        slices = []
        for name in TABLES:
            src = (interim / f"{name}.parquet").as_posix()
            out = out_dir / f"{name}.parquet"
            con.execute(
                f"COPY (SELECT s.* FROM '{src}' s SEMI JOIN chosen USING (msno)) "
                f"TO '{out.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )
            rows, subs = con.execute(
                f"SELECT count(*), count(DISTINCT msno) FROM '{out.as_posix()}'"
            ).fetchone()  # type: ignore[misc]
            source_rows = _count(con, f"SELECT count(*) FROM '{src}'")
            slices.append(
                TableSlice(
                    name=name,
                    rows=rows,
                    subscribers=subs,
                    kilobytes=round(out.stat().st_size / 1000, 1),
                    share_of_source=rows / source_rows if source_rows else 0.0,
                )
            )

        con.execute(
            f"CREATE OR REPLACE TEMP VIEW tx AS "
            f"SELECT * FROM '{(out_dir / 'transactions.parquet').as_posix()}'"
        )
        con.execute(
            f"CREATE OR REPLACE TEMP VIEW tx_v2 AS "
            f"SELECT * FROM '{(out_dir / 'transactions_v2.parquet').as_posix()}'"
        )
        con.execute(
            f"CREATE OR REPLACE TEMP VIEW members AS "
            f"SELECT * FROM '{(out_dir / 'members.parquet').as_posix()}'"
        )
        coverage = [
            Coverage(case=case, matters_for=why, rows=_count(con, sql)) for case, why, sql in PROBES
        ]

        return SampleReport(
            target=target,
            subscribers=subscribers,
            source_subscribers=source_subscribers,
            slices=slices,
            coverage=coverage,
        )
    finally:
        con.close()


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    row = con.execute(sql).fetchone()
    assert row is not None
    return int(row[0])


def format_report(report: SampleReport) -> str:
    """Markdown, because these numbers are due in docs/mapping.md, not on a terminal."""
    lines = [
        f"**{report.subscribers:,} subscribers** sampled from {report.source_subscribers:,} "
        f"(target {report.target:,}), {report.megabytes} MB total.",
        "",
        "| table | rows | subscribers | size | share of source |",
        "|---|---:|---:|---:|---:|",
    ]
    lines += [s.line for s in report.slices]
    lines += [
        "",
        "| case the pipeline handles | why it matters | rows in the sample |",
        "|---|---|---:|",
    ]
    lines += [c.line for c in report.coverage]
    if report.missing:
        lines += [
            "",
            "**Not in the sample:** "
            + "; ".join(c.case for c in report.missing)
            + ". These branches are covered by the fixtures in `tests/`, not by CI's "
            "sample run. The sample is left uniform rather than topped up, because a "
            "stratified sample would make every rate computed on it wrong invisibly.",
        ]
    return "\n".join(lines)
