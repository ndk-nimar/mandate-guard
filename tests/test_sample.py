"""CI-sample tests (T1.5).

Two jobs here, and they are different in kind.

The first half tests the sampler on a synthetic interim: that the unit is the subscriber,
that membership is deterministic, and that the tables stay consistent with each other.

The second half runs the *real committed sample* through the *real pipeline*. That is the
only test in the repo that proves the claim `data/sample/` exists to make -- that CI can
regenerate this project's numbers without the 1 GB download. Without it the sample is a
committed blob nobody executes, and it would rot the first time the pipeline changed.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from mandateguard.data import mandates, sample
from mandateguard.data.cancel import analyse, analyse_lapses
from mandateguard.data.ingest import SPECS, ingest_table
from mandateguard.data.paths import sample_dir
from mandateguard.models import Rail

TX_HEADER = (
    "msno,payment_method_id,payment_plan_days,plan_list_price,actual_amount_paid,"
    "is_auto_renew,transaction_date,membership_expire_date,is_cancel"
)

MEMBERS_HEADER = "msno,city,bd,gender,registered_via,registration_init_time"

# 40 subscribers with uneven history lengths. Uneven is the point: a row-level sample
# would cut the long histories in half and leave the short ones whole.
SUBSCRIBERS = [f"sub{n:03d}" for n in range(40)]


def _tx_rows() -> list[str]:
    rows = []
    for index, msno in enumerate(SUBSCRIBERS):
        for cycle in range(1 + index % 5):  # 1 to 5 transactions each
            month = 1 + cycle
            rows.append(f"{msno},41,30,149,149,1,2016{month:02d}01,2016{month + 1:02d}01,0")
    return rows


@pytest.fixture
def interim(tmp_path: Path) -> Path:
    (tmp_path / "transactions.csv").write_text(
        "\n".join([TX_HEADER, *_tx_rows(), ""]), encoding="utf-8"
    )
    # transactions_v2 is a strict subset here; the sampler must not assume otherwise.
    (tmp_path / "transactions_v2.csv").write_text(
        "\n".join([TX_HEADER, *_tx_rows()[:10], ""]), encoding="utf-8"
    )
    (tmp_path / "members_v3.csv").write_text(
        "\n".join([MEMBERS_HEADER, *(f"{m},1,30,male,7,20150101" for m in SUBSCRIBERS[:30]), ""]),
        encoding="utf-8",
    )
    (tmp_path / "train_v2.csv").write_text(
        "\n".join(["msno,is_churn", *(f"{m},0" for m in SUBSCRIBERS[:20]), ""]),
        encoding="utf-8",
    )
    out = tmp_path / "interim"
    out.mkdir()
    con = duckdb.connect()
    try:
        for spec in SPECS:
            ingest_table(con, spec, tmp_path, out)
    finally:
        con.close()
    return out


@pytest.fixture
def report(interim: Path, tmp_path: Path):
    return sample.build(interim=interim, out_dir=tmp_path / "sample", target=10)


def _read(path: Path, table: str) -> set[str]:
    rows = (
        duckdb.connect()
        .execute(f"SELECT DISTINCT msno FROM '{(path / f'{table}.parquet').as_posix()}'")
        .fetchall()
    )
    return {r[0] for r in rows}


# --------------------------------------------------------------------------------
# The sampler.
# --------------------------------------------------------------------------------


def test_the_unit_is_the_subscriber_not_the_row(report, interim, tmp_path):
    """A subscriber with three of their five transactions is not a smaller subscriber,
    it is a corrupted one: their coverage timeline gains a hole that never existed, so
    they lapse in the frame and did not lapse in life."""
    con = duckdb.connect()
    full = dict(
        con.execute(
            f"SELECT msno, count(*) FROM '{(interim / 'transactions.parquet').as_posix()}' "
            "GROUP BY 1"
        ).fetchall()
    )
    sampled = dict(
        con.execute(
            f"SELECT msno, count(*) FROM "
            f"'{(tmp_path / 'sample' / 'transactions.parquet').as_posix()}' GROUP BY 1"
        ).fetchall()
    )
    assert sampled, "the sample is empty"
    for msno, rows in sampled.items():
        assert rows == full[msno]


def test_membership_is_deterministic(interim, tmp_path):
    """Two runs must choose the same subscribers. A committed sample that moved between
    runs would make every committed number unreproducible by construction."""
    first = sample.build(interim=interim, out_dir=tmp_path / "a", target=10)
    second = sample.build(interim=interim, out_dir=tmp_path / "b", target=10)
    assert first.subscribers == second.subscribers
    assert _read(tmp_path / "a", "transactions") == _read(tmp_path / "b", "transactions")


def test_every_table_holds_the_same_subscriber_set_or_a_subset(report, tmp_path):
    """`members` and `labels` cover fewer subscribers than `transactions` does -- that
    gap is real and 3.7 depends on it surviving. What must never happen is a table
    holding a subscriber the sample did not choose."""
    chosen = _read(tmp_path / "sample", "transactions")
    for table in ("transactions_v2", "members", "labels"):
        assert _read(tmp_path / "sample", table) <= chosen


def test_the_sample_is_a_strict_subset_of_the_source(report, interim, tmp_path):
    assert _read(tmp_path / "sample", "transactions") < _read(interim, "transactions")


def test_the_realised_count_is_reported_not_rounded_to_the_target(report):
    """Hash buckets do not divide evenly, so the realised count is near the target and
    not on it. Claiming the target would be claiming a number nobody measured."""
    assert report.target == 10
    assert 0 < report.subscribers <= len(SUBSCRIBERS)


def test_missing_cases_are_reported_rather_than_topped_up(report):
    """A branch the sample never enters has to be visible. Topping the sample up to
    cover it would bias every rate computed on the sample, invisibly."""
    assert report.coverage
    for case in report.coverage:
        assert case.rows >= 0
    assert all(c.rows == 0 for c in report.missing)
    assert "**none**" in "\n".join(c.line for c in report.missing) or not report.missing


# --------------------------------------------------------------------------------
# The bug this file exists to keep fixed.
# --------------------------------------------------------------------------------


def test_the_sample_hash_is_independent_of_the_rail_hash(interim, tmp_path):
    """Two hashes of one key are not two independent draws.

    Both the sampler and the rail assignment hash `msno`. When both used a bare
    `hash(msno)`, the sample kept exactly the subscribers in the lowest hash buckets --
    which are precisely the buckets the rail ladder gives to its first rail. The full
    book came out at the configured mix, the CI sample came out **100% UPI AutoPay**,
    and nothing failed. Every per-rail number CI produced would have been degenerate.

    This test builds the mandate book from a sample and demands more than one rail.
    """
    sample.build(interim=interim, out_dir=tmp_path / "s", target=40)
    book = mandates.build(interim=tmp_path / "s", out_dir=tmp_path / "p", write=False)
    assert len(book.by_rail) > 1, (
        f"every sampled mandate landed on one rail ({book.by_rail}) -- the sample hash "
        "and the rail hash are correlated again"
    )


def test_the_two_salts_are_different(interim):
    """The salts are what keep the two hashes apart. Equal salts would silently
    reintroduce the correlation above."""
    assert sample.SAMPLE_SALT != mandates.RAIL_SALT


# --------------------------------------------------------------------------------
# The committed sample, run through the real pipeline. This is the T1.5 claim.
# --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def committed() -> Path:
    path = sample_dir()
    if not (path / "transactions.parquet").exists():
        pytest.skip("data/sample/ is not built -- run scripts/build_sample.py")
    return path


def test_the_committed_sample_builds_a_mandate_book(committed, tmp_path):
    """The whole reason `data/sample/` is committed: CI regenerates this project's
    numbers from it, with no 1 GB download. If T1.3 ever stops working on the sample,
    this fails here rather than in a results table nobody re-ran."""
    book = mandates.build(interim=committed, out_dir=tmp_path, write=False)
    assert book.mandates > 1_000
    assert set(book.by_rail) == {rail.value for rail in Rail}
    assert sum(book.by_status.values()) == book.mandates


def test_the_sample_reproduces_the_full_books_shape(committed, tmp_path):
    """Not its numbers -- its shape. The sample is uniform over subscribers, so the
    retention rate and the rail mix should land near the full run's, and a sample that
    drifted far from them would be a sample of something else.

    Full run (mapping.md 3.7): 58.9% retention, mix 0.550 / 0.250 / 0.150 / 0.050.
    """
    book = mandates.build(interim=committed, out_dir=tmp_path, write=False)
    assert book.retention == pytest.approx(0.589, abs=0.05)
    assert book.by_rail[Rail.UPI_AUTOPAY.value] / book.mandates == pytest.approx(0.55, abs=0.05)


def test_the_committed_sample_supports_the_cancel_analysis(committed):
    """T1.2's two measurements have to run on the sample too, or `q` and `r` are numbers
    CI can never re-derive. The rates will differ from the full run -- 5k subscribers is
    not 2.36M -- but the analysis must produce them at all."""
    transactions = committed / "transactions.parquet"
    cancels = analyse(transactions)
    lapses = analyse_lapses(transactions)
    assert cancels.cancel_events > 0
    assert lapses.passive_lapses > 0
    assert 0.0 <= cancels.rate_for(84).rate <= 1.0
    assert 0.0 <= lapses.rate_for(84).rate <= 1.0


def test_the_committed_sample_stays_small_enough_to_commit(committed):
    """A sample that grew past a few MB would stop being reviewable in a diff and start
    being a binary blob in the history."""
    total = sum(p.stat().st_size for p in committed.glob("*.parquet"))
    assert total < 5_000_000, f"data/sample/ is {total / 1e6:.1f} MB"
