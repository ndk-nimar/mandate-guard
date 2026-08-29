"""Scoring and baseline tests (T1.6).

These are the numbers GATE 1 turns on, so the arithmetic is pinned against values that
can be checked by hand rather than against whatever the code produced first. Two rows at
p = 0.5, one of each class, must give Brier 0.25 and log loss ln 2 -- if that is not what
comes out, no comparison built on top of it means anything.

The fixture is a literal table, not a parquet file: the split, the bins, and the metrics
are all pure functions of columns, and building them from a VALUES list makes every
expected number visible in the test.
"""

from __future__ import annotations

from datetime import date
from math import log

import duckdb
import pytest

from mandateguard.risk import baseline, scoring

SNAPSHOT = date(2017, 2, 28)


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    return duckdb.connect()


def table(con: duckdb.DuckDBPyConnection, rows: list[tuple]) -> str:
    """(week_start, days_to_coverage_end, event) as a scoreable relation."""
    values = ", ".join(
        f"(DATE '{w}', {'NULL' if d is None else d}, {str(e).lower()})" for w, d, e in rows
    )
    con.execute(
        f"CREATE OR REPLACE TEMP VIEW frame AS "
        f"SELECT * FROM (VALUES {values}) t(week_start, days_to_coverage_end, event)"
    )
    return "frame"


# --------------------------------------------------------------------------------
# The split.
# --------------------------------------------------------------------------------


def test_the_split_holds_out_exactly_the_harness_horizon():
    """The held-out period is `horizon.weeks`, not a number picked here -- the harness
    rolls the world forward over exactly that many weeks, so it is the period the system
    will actually be asked to predict."""
    split = scoring.split_at(SNAPSHOT, 12, 7)
    assert split.cutoff == date(2016, 12, 6)
    assert (SNAPSHOT - split.cutoff).days == 84


def test_the_unobservable_tail_is_excluded_from_both_slices():
    """A week ending inside the last `7 + tolerance` days cannot contain a *confirmed*
    death, so its labels are all zero for a reason that has nothing to do with the
    subscribers. Leaving them in deflated the test base rate from 0.0083 to 0.0078 on
    the sample and read as a miscalibrated model rather than as an artefact."""
    split = scoring.split_at(SNAPSHOT, 12, 7)
    assert split.last_observable == date(2017, 2, 14)
    assert split.observable in split.train
    assert split.observable in split.test


def test_train_and_test_do_not_overlap(con):
    source = table(
        con,
        [("2016-01-04", 10, False), ("2017-01-02", 10, False), ("2017-02-20", 10, False)],
    )
    split = scoring.split_at(SNAPSHOT, 12, 7)
    counts = [
        int(con.execute(f"SELECT count(*) FROM {source} WHERE {w}").fetchone()[0])  # type: ignore[index]
        for w in (split.train, split.test)
    ]
    assert counts == [1, 1]  # the third row is past `last_observable`


# --------------------------------------------------------------------------------
# The metrics.
# --------------------------------------------------------------------------------


def test_brier_and_log_loss_match_hand_arithmetic(con):
    """Two rows at p = 0.5, one of each class. Brier is 0.25 and log loss is ln 2."""
    source = table(con, [("2016-01-04", 1, True), ("2016-01-11", 1, False)])
    result = scoring.score(con, source, "0.5", "TRUE", "half")
    assert result.brier == pytest.approx(0.25)
    assert result.log_loss == pytest.approx(log(2))
    assert result.base_rate == pytest.approx(0.5)
    assert result.calibration_in_the_large == pytest.approx(1.0)


def test_a_confident_wrong_prediction_is_clipped_rather_than_infinite(con):
    """`expiry_rule` predicts hard 0 and 1. Without the clip its log loss is infinite,
    which is a fact about the metric and not about the model -- and an infinity in the
    table would hide every other number in it."""
    source = table(con, [("2016-01-04", 1, True)])
    result = scoring.score(con, source, "0.0", "TRUE", "wrong")
    assert result.brier == pytest.approx(1.0)
    assert result.log_loss > 30
    assert result.log_loss < float("inf")


def test_skill_is_zero_against_itself_and_positive_when_better(con):
    """Brier skill, not raw Brier, is what "beats the baseline" means here: at a base
    rate near 1%, predicting 1% for everyone already scores a Brier of 0.01 and a model
    can look excellent while knowing nothing."""
    source = table(
        con, [("2016-01-04", 1, True), ("2016-01-11", 1, False), ("2016-01-18", 1, False)]
    )
    reference = scoring.score(con, source, "0.3333333333", "TRUE", "constant")
    perfect = scoring.score(
        con, source, "CASE WHEN event THEN 0.99 ELSE 0.01 END", "TRUE", "oracle"
    )
    assert reference.skill_against(reference) == pytest.approx(0.0, abs=1e-9)
    assert perfect.skill_against(reference) > 0.9


def test_calibration_in_the_large_catches_a_uniformly_inflated_model(con):
    """The full-run finding in one assertion: a model can discriminate well and still
    predict twice as many deaths as happened, and Brier will punish it for that alone."""
    source = table(con, [("2016-01-04", 1, True)] + [("2016-01-11", 1, False)] * 9)
    doubled = scoring.score(con, source, "0.2", "TRUE", "doubled")
    assert doubled.base_rate == pytest.approx(0.1)
    assert doubled.calibration_in_the_large == pytest.approx(2.0)


# --------------------------------------------------------------------------------
# The baselines.
# --------------------------------------------------------------------------------


def test_the_constant_baseline_is_the_training_death_rate(con):
    source = table(
        con,
        [("2016-01-04", 1, True), ("2016-01-11", 1, False), ("2017-01-10", 1, True)],
    )
    split = scoring.split_at(SNAPSHOT, 12, 7)
    assert baseline.constant(con, source, split.train) == pytest.approx(0.5)


def test_bins_are_ordered_and_cover_the_whole_line(con):
    """Every row must land in exactly one bin, including negative days and nulls -- a
    row that falls through silently would be scored by the fallback while looking like
    it had been binned."""
    rows = [
        ("2016-01-04", -5, True),  # already expired
        ("2016-01-11", 0, True),  # 0-3
        ("2016-01-18", 3, False),  # 0-3
        ("2016-01-25", 5, False),  # 4-7
        ("2016-02-01", 200, False),  # 120+
        ("2016-02-08", None, False),  # no bin: fallback
    ]
    source = table(con, rows)
    fitted = baseline.fit_bins(con, source, "TRUE")
    counted = {b.label: b.rows for b in fitted.bins}
    assert counted["already expired"] == 1
    assert counted["0-3 days"] == 2
    assert counted["4-7 days"] == 1
    assert counted["120+ days"] == 1
    assert sum(counted.values()) == len(rows) - 1  # the null is not in any bin


def test_an_empty_bin_predicts_the_fallback_not_zero(con):
    """A bin the training period never saw would otherwise predict exactly 0, and score
    an infinite log loss the first time a death lands in it."""
    source = table(con, [("2016-01-04", 1, True), ("2016-01-11", 1, False)])
    fitted = baseline.fit_bins(con, source, "TRUE")
    empty = next(b for b in fitted.bins if b.rows == 0)
    assert empty.rate == 0.0
    assert f"THEN {fitted.fallback}" in fitted.expression


def test_the_binned_prediction_reproduces_the_fitted_rates(con):
    """The expression is what gets scored, so it has to agree with the table that was
    fitted -- a lookup that disagrees with its own report is worse than no report."""
    rows = [("2016-01-04", 1, True), ("2016-01-11", 1, False), ("2016-01-18", 20, False)]
    source = table(con, rows)
    fitted = baseline.fit_bins(con, source, "TRUE")
    predicted = con.execute(
        f"SELECT days_to_coverage_end, {fitted.expression} FROM {source} ORDER BY 1"
    ).fetchall()
    assert dict(predicted) == {1: pytest.approx(0.5), 20: pytest.approx(0.0)}


def test_the_rule_baseline_is_a_hard_zero_or_one(con):
    source = table(con, [("2016-01-04", 3, True), ("2016-01-11", 30, False)])
    predicted = con.execute(
        f"SELECT {baseline.rule_expression()} FROM {source} ORDER BY days_to_coverage_end"
    ).fetchall()
    assert [p for (p,) in predicted] == [1.0, 0.0]


def test_a_null_coverage_end_is_not_filed_as_far_from_expiry(con):
    """ "We do not know when coverage ends" and "coverage ends in six months" are
    different statements. Filing the first as the second would give an unknown mandate
    the safest bin in the table."""
    source = table(con, [("2016-01-04", None, False)])
    predicted = con.execute(f"SELECT {baseline.rule_expression()} FROM {source}").fetchall()
    assert predicted == [(0.0,)]
    fitted = baseline.fit_bins(con, table(con, [("2016-01-04", 1, True)]), "TRUE")
    null_row = table(con, [("2016-01-04", None, False)])
    got = con.execute(f"SELECT {fitted.expression} FROM {null_row}").fetchone()
    assert got == (pytest.approx(fitted.fallback),)
