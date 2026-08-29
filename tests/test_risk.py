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
import numpy as np
import pytest

from mandateguard.risk import baseline, calibration, hazard, scoring

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


# --------------------------------------------------------------------------------
# The hazard model (T1.7).
# --------------------------------------------------------------------------------


def hazard_frame(con: duckdb.DuckDBPyConnection, rows: int = 4000) -> str:
    """A synthetic person-period frame with the real column set and a real signal.

    Death probability rises as coverage runs out, so a fitted model has something to
    find; a quarter of the rows carry no `members` record, so every nullable column is
    null somewhere. That second half is the point of the fixture -- nulls inside an
    indicator are what broke the first fit.
    """
    con.execute(f"""
        CREATE OR REPLACE TEMP TABLE hz AS
        SELECT
            'm' || (i % 200)::VARCHAR                            AS mandate_id,
            (i % 60)::INTEGER                                    AS week_index,
            DATE '2015-01-05' + INTERVAL 1 DAY * (7 * (i % 100)) AS week_start,
            (hash(i::VARCHAR) % 100) < (CASE WHEN i % 31 < 4 THEN 40 ELSE 2 END) AS event,
            CASE WHEN i % 31 < 4 THEN i % 4 ELSE 10 + i % 40 END AS days_to_coverage_end,
            (i % 29)::INTEGER                                    AS days_since_last_txn,
            (99 + (i % 3) * 30)::DECIMAL(12,1)                   AS amount_inr,
            (30 + (i % 2) * 60)::INTEGER                         AS debit_frequency_days,
            i % 97 = 0                                           AS frequency_imputed,
            i % 53 <> 0                                          AS auto_renew,
            (i % 7)::DECIMAL(12,1)                               AS discount_inr,
            (1 + i % 12)::INTEGER                                AS debits_so_far,
            (i % 3)::INTEGER                                     AS cancels_so_far,
            (149 * (1 + i % 12))::DECIMAL(38,1)                  AS paid_so_far_inr,
            'upi_autopay'                                        AS method,
            CASE WHEN i % 4 = 0 THEN NULL ELSE (1 + i % 22)::SMALLINT END AS city,
            CASE WHEN i % 4 = 0 THEN NULL ELSE (7 - (i % 3))::SMALLINT END AS registered_via,
            CASE WHEN i % 4 = 0 THEN NULL WHEN i % 2 = 0 THEN 'male' ELSE 'female' END AS gender,
            NULL::INTEGER                                        AS age_years,
            i % 4 <> 0                                           AS member_record_found,
            CASE WHEN i % 4 = 0 THEN NULL ELSE (100 + i)::INTEGER END AS account_age_days,
            i % 11 = 0                                           AS left_truncated,
            (7 * (i % 60))::INTEGER                              AS tenure_days
        FROM range({rows}) t(i)
    """)
    return "hz"


def test_every_feature_survives_a_row_with_nothing_in_it(con):
    """A null indicator has to be 0, not null. `sklearn` refuses a NaN, and it is right
    to: "no city on record" is not a missing value waiting to be imputed, it is a zero
    for every city dummy -- `member_known` is the column that carries the absence."""
    con.execute("""
        CREATE OR REPLACE TEMP VIEW empty_row AS SELECT
            0 AS week_index, NULL::INTEGER AS days_to_coverage_end,
            NULL::INTEGER AS days_since_last_txn, NULL::DOUBLE AS amount_inr,
            NULL::INTEGER AS debit_frequency_days, NULL::BOOLEAN AS frequency_imputed,
            NULL::BOOLEAN AS auto_renew, NULL::DOUBLE AS discount_inr,
            NULL::INTEGER AS debits_so_far, NULL::INTEGER AS cancels_so_far,
            NULL::DOUBLE AS paid_so_far_inr, NULL::SMALLINT AS city,
            NULL::SMALLINT AS registered_via, NULL::VARCHAR AS gender,
            NULL::BOOLEAN AS member_record_found, NULL::INTEGER AS account_age_days,
            NULL::BOOLEAN AS left_truncated
    """)
    features = hazard.feature_spec()
    columns = ", ".join(f"{f.sql} AS {f.name}" for f in features)
    values = con.execute(f"SELECT {columns} FROM empty_row").fetchone()
    assert values is not None
    assert all(v is not None for v in values)


def test_the_excluded_columns_are_nowhere_in_the_feature_set():
    """Five exclusions, each argued in the module docstring. A test rather than a comment
    because the cost of quietly re-adding one is a model that looks better and means
    less: `age_years` would learn signup channel through its own missingness, `method` is
    a hash so any coefficient is overfit, `week_start` is calendar time the out-of-time
    split cannot carry forward, and `tenure_days` is `7 * week_index` exactly."""
    everything = " ".join(f.sql for f in hazard.feature_spec())
    for banned in ("age_years", "method", "week_start", "tenure_days", "death_kind"):
        assert banned not in everything


def test_feature_names_are_unique():
    names = [f.name for f in hazard.feature_spec()]
    assert len(names) == len(set(names))


def test_the_fit_is_reproducible(con):
    """Same frame, same seed, same coefficients. The subsample is a hash of the row key
    rather than an RNG draw, and the matrix is ordered before it reaches sklearn."""
    source = hazard_frame(con)
    first = hazard.fit(con, source, "TRUE", seed=20260905, rows=4000)
    second = hazard.fit(con, source, "TRUE", seed=20260905, rows=4000)
    assert first.coefficients == second.coefficients
    assert first.intercept == second.intercept


def test_a_different_seed_draws_a_different_subsample(con):
    """The seed has to actually reach the draw, or `config/params.yaml`'s seed is
    decorative."""
    source = hazard_frame(con)
    a = hazard.fit(con, source, "TRUE", seed=1, rows=2000)
    b = hazard.fit(con, source, "TRUE", seed=2, rows=2000)
    assert a.coefficients != b.coefficients


def test_the_sql_expression_reproduces_sklearns_own_predictions(con):
    """The model is fitted in Python and scored in SQL, so the two have to agree to
    floating point. If they did not, every number in docs/eval.md would be describing a
    model that was never fitted -- and nothing would fail."""
    source = hazard_frame(con)
    model = hazard.fit(con, source, "TRUE", seed=20260905, rows=4000)

    columns = ", ".join(f.sql for f in model.features)
    matrix = con.execute(f"SELECT {columns} FROM {source} ORDER BY mandate_id, week_index").df()
    logits = matrix.to_numpy(dtype=float) @ np.array(model.coefficients) + model.intercept
    expected = 1.0 / (1.0 + np.exp(-logits))

    got = (
        con.execute(f"SELECT {model.expression} FROM {source} ORDER BY mandate_id, week_index")
        .df()
        .to_numpy(dtype=float)
        .ravel()
    )
    assert np.allclose(got, expected, atol=1e-9)


def test_predictions_are_probabilities(con):
    source = hazard_frame(con)
    model = hazard.fit(con, source, "TRUE", seed=20260905, rows=4000)
    row = con.execute(
        f"SELECT min({model.expression}), max({model.expression}), "
        f"count(*) FILTER (WHERE {model.expression} IS NULL) FROM {source}"
    ).fetchone()
    assert row is not None
    assert 0.0 < row[0] <= row[1] < 1.0
    assert row[2] == 0


def test_the_model_is_not_reweighted_into_miscalibration(con):
    """`class_weight="balanced"` is the reflex at a 1% base rate and it would multiply
    every prediction by the imbalance ratio. The allocator turns these probabilities into
    rupees, so a uniformly inflated one prices every decision too high -- and eval.md 1.4
    already showed a well-discriminating, badly-calibrated model losing on Brier."""
    source = hazard_frame(con)
    model = hazard.fit(con, source, "TRUE", seed=20260905, rows=4000)
    fitted = scoring.score(con, source, model.expression, "TRUE", "hazard")
    assert fitted.calibration_in_the_large == pytest.approx(1.0, abs=0.15)


def test_the_model_beats_the_constant_baseline_on_its_own_training_signal(con):
    """A smoke test, not a claim: on a fixture where risk really does rise with closeness
    to expiry, a model that could not beat a constant would be broken."""
    source = hazard_frame(con)
    model = hazard.fit(con, source, "TRUE", seed=20260905, rows=4000)
    rate = baseline.constant(con, source, "TRUE")
    reference = scoring.score(con, source, str(rate), "TRUE", "constant")
    fitted = scoring.score(con, source, model.expression, "TRUE", "hazard")
    assert fitted.skill_against(reference) > 0.1


# --------------------------------------------------------------------------------
# Calibration (T1.8).
# --------------------------------------------------------------------------------


def test_a_perfectly_calibrated_model_has_no_calibration_error(con):
    """The metric has to be zero when it should be zero, or its value on the real model
    is a number with no reference point."""
    source = table(con, [("2016-01-04", 1, i % 10 == 0) for i in range(1000)])
    curve = calibration.reliability(con, source, "0.1", "TRUE", "exact", buckets=4)
    assert curve.rows == 1000
    assert curve.expected_calibration_error == pytest.approx(0.0, abs=0.02)


def test_a_uniformly_doubled_model_shows_up_as_calibration_error(con):
    source = table(con, [("2016-01-04", 1, i % 10 == 0) for i in range(1000)])
    curve = calibration.reliability(con, source, "0.2", "TRUE", "doubled", buckets=4)
    assert curve.expected_calibration_error == pytest.approx(0.1, abs=0.02)


def test_buckets_hold_equal_numbers_of_person_weeks(con):
    """Equal-count, not equal-width. At a base rate near 0.7% equal-width bins put
    everything in the first bucket, and the curve becomes one point."""
    source = table(con, [("2016-01-04", i, i % 7 == 0) for i in range(100)])
    curve = calibration.reliability(
        con, source, "days_to_coverage_end / 1000.0", "TRUE", "spread", buckets=5
    )
    assert [b.rows for b in curve.buckets] == [20, 20, 20, 20, 20]


def test_a_bucket_with_no_deaths_reports_no_ratio_rather_than_infinity(con):
    source = table(con, [("2016-01-04", 1, False)] * 10)
    curve = calibration.reliability(con, source, "0.01", "TRUE", "quiet", buckets=2)
    assert all(b.line.endswith("-- |") for b in curve.buckets)


def test_the_diagram_is_byte_identical_across_runs(tmp_path, con):
    """The PNG is committed, so ADR 0003 applies to it too: matplotlib stamps its own
    version into every file it writes, and without suppressing that the diagram changes
    whenever matplotlib does."""
    source = table(con, [("2016-01-04", i, i % 5 == 0) for i in range(200)])
    curve = calibration.reliability(
        con, source, "0.001 + days_to_coverage_end / 1000.0", "TRUE", "curve", buckets=5
    )
    first = calibration.plot([curve], tmp_path / "a.png").read_bytes()
    second = calibration.plot([curve], tmp_path / "b.png").read_bytes()
    assert first == second
    assert b"matplotlib" not in first.lower()
