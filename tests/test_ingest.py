"""Ingestion tests (T1.1).

These run on hand-written three-row CSVs, not on the 21M-row download. The point is to
pin the contract -- what types come out, and what happens to malformed input -- so that
a broken cast fails here in a second rather than forty minutes into a real ingest.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from mandateguard.data.ingest import SPECS, TableSpec, ingest_table

TRANSACTIONS_CSV = "\n".join(
    [
        "msno,payment_method_id,payment_plan_days,plan_list_price,actual_amount_paid,"
        "is_auto_renew,transaction_date,membership_expire_date,is_cancel",
        "aaa,41,30,129,129,1,20150930,20151101,0",
        "bbb,32,90,298,298,0,20170131,20170504,1",
        "ccc,41,30,129,0,1,20170231,20170401,0",  # 31 February: not a real date
        "",
    ]
)

MEMBERS_CSV = "\n".join(
    [
        "msno,city,bd,gender,registered_via,registration_init_time",
        "aaa,1,0,,11,20110911",
        "bbb,13,24,male,9,20150406",
        "",
    ]
)


def spec(name: str) -> TableSpec:
    return next(s for s in SPECS if s.name == name)


@pytest.fixture
def con():
    connection = duckdb.connect()
    yield connection
    connection.close()


@pytest.fixture
def out(tmp_path: Path) -> Path:
    target = tmp_path / "interim"
    target.mkdir()
    return target


def write(directory: Path, filename: str, body: str) -> None:
    (directory / filename).write_text(body, encoding="utf-8")


def column_types(con, parquet: Path) -> dict[str, str]:
    rows = con.execute(f"DESCRIBE SELECT * FROM '{parquet.as_posix()}'").fetchall()
    return {name: type_ for name, type_, *_ in rows}


def test_transactions_are_typed_not_left_as_text(con, tmp_path, out):
    write(tmp_path, "transactions.csv", TRANSACTIONS_CSV)

    summary = ingest_table(con, spec("transactions"), tmp_path, out)

    assert summary.rows == 3
    assert summary.subscribers == 3
    types = column_types(con, out / "transactions.parquet")
    assert types["transaction_date"] == "DATE"
    assert types["membership_expire_date"] == "DATE"
    assert types["is_auto_renew"] == "BOOLEAN"
    assert types["is_cancel"] == "BOOLEAN"


def test_impossible_date_becomes_null_and_is_counted(con, tmp_path, out):
    """20170231 does not exist. It must surface as a counted NULL, never as a silent
    fallback date -- an invented 2017-03-03 would corrupt every survival week downstream."""
    write(tmp_path, "transactions.csv", TRANSACTIONS_CSV)

    summary = ingest_table(con, spec("transactions"), tmp_path, out)

    transaction_date = next(d for d in summary.dates if d.column == "transaction_date")
    assert transaction_date.unparsed == 1
    assert transaction_date.minimum == "2015-09-30"
    assert transaction_date.maximum == "2017-01-31"


def test_blank_gender_becomes_null(con, tmp_path, out):
    """Empty string and NULL both mean "unknown", but only NULL is countable in SQL."""
    write(tmp_path, "members_v3.csv", MEMBERS_CSV)

    ingest_table(con, spec("members"), tmp_path, out)

    parquet = (out / "members.parquet").as_posix()
    missing = con.execute(
        f"SELECT count(*) FILTER (WHERE gender IS NULL) FROM '{parquet}'"
    ).fetchone()[0]
    assert missing == 1


def test_absurd_age_is_preserved_not_silently_dropped(con, tmp_path, out):
    """`bd` is famously full of nonsense. Deciding what to do about it is T1.3's job,
    argued in docs/mapping.md. Ingestion must not quietly pre-empt that decision."""
    write(tmp_path, "members_v3.csv", MEMBERS_CSV)

    summary = ingest_table(con, spec("members"), tmp_path, out)

    assert summary.rows == 2  # the bd=0 row survives


def test_missing_source_file_says_what_to_run(con, tmp_path, out):
    with pytest.raises(FileNotFoundError, match="fetch_data"):
        ingest_table(con, spec("transactions"), tmp_path, out)


def test_limit_is_honoured(con, tmp_path, out):
    write(tmp_path, "transactions.csv", TRANSACTIONS_CSV)

    assert ingest_table(con, spec("transactions"), tmp_path, out, limit=2).rows == 2
