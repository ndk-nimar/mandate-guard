"""T1.1 -- re-encode the KKBox CSVs as typed parquet, using DuckDB rather than pandas.

`transactions.csv` is 21.5M rows / 1.7 GB. `pandas.read_csv` would hold the whole thing
in memory (the 44-character `msno` column alone is roughly 2 GB as Python strings) and
every notebook restart would pay that cost again. DuckDB streams the CSV from disk and
never materialises it, so this step runs in bounded memory on a laptop.

What this step is allowed to do: choose types, parse the `YYYYMMDD` integers into real
dates, and turn empty strings into NULL. That is re-encoding, not interpretation.

What it deliberately does NOT do: drop the absurd `bd` (age) values, merge
`transactions` with `transactions_v2`, or decide what `is_cancel` means. Those are
modelling decisions that belong to T1.2/T1.3 and must be argued in `docs/mapping.md`,
not buried in an ingestion script. This step only measures how bad the data is.

    uv run python scripts/ingest.py
    uv run python scripts/ingest.py --limit 100000   # quick smoke run
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from pydantic import BaseModel

from mandateguard.data.paths import ensure, interim_dir, raw_dir

# `msno` stays VARCHAR; the numeric widths are chosen to be comfortably larger than
# anything the 2017 competition data contains, because a cast failure kills the run.
TRANSACTION_COLUMNS = """
    msno,
    payment_method_id::SMALLINT                                    AS payment_method_id,
    payment_plan_days::SMALLINT                                    AS payment_plan_days,
    plan_list_price::INTEGER                                       AS plan_list_price,
    actual_amount_paid::INTEGER                                    AS actual_amount_paid,
    is_auto_renew::BOOLEAN                                         AS is_auto_renew,
    try_strptime(transaction_date::VARCHAR, '%Y%m%d')::DATE        AS transaction_date,
    try_strptime(membership_expire_date::VARCHAR, '%Y%m%d')::DATE  AS membership_expire_date,
    is_cancel::BOOLEAN                                             AS is_cancel
"""

MEMBER_COLUMNS = """
    msno,
    city::SMALLINT                                                     AS city,
    bd::INTEGER                                                        AS bd,
    nullif(trim(gender), '')                                           AS gender,
    registered_via::SMALLINT                                           AS registered_via,
    try_strptime(registration_init_time::VARCHAR, '%Y%m%d')::DATE      AS registration_init_time
"""

LABEL_COLUMNS = """
    msno,
    is_churn::BOOLEAN AS is_churn
"""


class TableSpec(BaseModel):
    name: str
    csv: str
    columns: str
    date_columns: list[str] = []


SPECS: list[TableSpec] = [
    TableSpec(
        name="transactions",
        csv="transactions.csv",
        columns=TRANSACTION_COLUMNS,
        date_columns=["transaction_date", "membership_expire_date"],
    ),
    TableSpec(
        name="transactions_v2",
        csv="transactions_v2.csv",
        columns=TRANSACTION_COLUMNS,
        date_columns=["transaction_date", "membership_expire_date"],
    ),
    TableSpec(
        name="members",
        csv="members_v3.csv",
        columns=MEMBER_COLUMNS,
        date_columns=["registration_init_time"],
    ),
    TableSpec(name="labels", csv="train_v2.csv", columns=LABEL_COLUMNS),
]


class DateRange(BaseModel):
    column: str
    minimum: str | None
    maximum: str | None
    unparsed: int  # rows whose YYYYMMDD integer was not a real date


class TableSummary(BaseModel):
    name: str
    rows: int
    subscribers: int
    megabytes: float
    dates: list[DateRange] = []


def ingest_table(
    con: duckdb.DuckDBPyConnection,
    spec: TableSpec,
    source_dir: Path,
    target_dir: Path,
    limit: int | None = None,
) -> TableSummary:
    csv_path = source_dir / spec.csv
    if not csv_path.exists():
        raise FileNotFoundError(f"{csv_path} is missing. Run scripts/fetch_data.py first.")
    out_path = target_dir / f"{spec.name}.parquet"

    tail = f"LIMIT {limit}" if limit else ""
    select = f"SELECT {spec.columns} FROM read_csv('{csv_path.as_posix()}', header = true) {tail}"
    con.execute(f"COPY ({select}) TO '{out_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)")

    source = f"'{out_path.as_posix()}'"
    rows, subscribers = con.execute(
        f"SELECT count(*), count(DISTINCT msno) FROM {source}"
    ).fetchone()  # type: ignore[misc]

    dates = []
    for column in spec.date_columns:
        low, high, unparsed = con.execute(
            f"SELECT min({column}), max({column}), "
            f"count(*) FILTER (WHERE {column} IS NULL) FROM {source}"
        ).fetchone()  # type: ignore[misc]
        dates.append(
            DateRange(
                column=column,
                minimum=str(low) if low else None,
                maximum=str(high) if high else None,
                unparsed=unparsed,
            )
        )

    return TableSummary(
        name=spec.name,
        rows=rows,
        subscribers=subscribers,
        megabytes=round(out_path.stat().st_size / 1e6, 1),
        dates=dates,
    )


def ingest_all(limit: int | None = None) -> list[TableSummary]:
    source_dir, target_dir = raw_dir(), ensure(interim_dir())
    con = duckdb.connect()
    try:
        return [ingest_table(con, spec, source_dir, target_dir, limit) for spec in SPECS]
    finally:
        con.close()


def format_report(summaries: list[TableSummary]) -> str:
    """Markdown, because these numbers are due in docs/mapping.md, not just on a terminal."""
    lines = ["| table | rows | subscribers | parquet |", "|---|---:|---:|---:|"]
    lines += [
        f"| `{s.name}` | {s.rows:,} | {s.subscribers:,} | {s.megabytes:,.1f} MB |"
        for s in summaries
    ]
    lines += ["", "| table | column | from | to | unparsed |", "|---|---|---|---|---:|"]
    lines += [
        f"| `{s.name}` | `{d.column}` | {d.minimum} | {d.maximum} | {d.unparsed:,} |"
        for s in summaries
        for d in s.dates
    ]
    return "\n".join(lines)
