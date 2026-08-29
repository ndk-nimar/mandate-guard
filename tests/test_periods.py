"""Person-period tests (T1.4).

The frame this module builds is the input to every model in Phase 1, and it fails in a
particular way: silently, and in the direction that flatters the model. A feature that
peeks one week into the future makes the hazard look brilliant in cross-validation and
useless in production, and nothing crashes. So most of what is tested here is not "does
it run" but "does it refuse to know things it should not know yet".

The fixture is fifteen-odd rows with a snapshot of 2016-12-31, and each subscriber is
one named case: a lapse, a revocation, a survivor, a death too close to the horizon to
confirm, a mid-spell plan change (the leakage probe), a comped month, a one-off
purchase, and a spell too short to observe.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from mandateguard.data import periods
from mandateguard.data.ingest import SPECS, ingest_table
from mandateguard.data.paths import sample_dir
from mandateguard.models import DeathKind
from mandateguard.policy.loader import Params, load_params

SNAPSHOT = "2016-12-31"

TX_HEADER = (
    "msno,payment_method_id,payment_plan_days,plan_list_price,actual_amount_paid,"
    "is_auto_renew,transaction_date,membership_expire_date,is_cancel"
)

TX_ROWS = [
    # lapser: renewed once, then coverage ran out on 2016-03-01 and nothing followed.
    # 60 days from first_seen, so 9 weeks with the death in week 8.
    "lapser,41,30,149,149,1,20160101,20160131,0",
    "lapser,41,30,149,149,1,20160131,20160301,0",
    # revoker: cancelled on day 14, which also ends coverage that day. Same death, other
    # kind -- and the two prices differ (problem.md 6.2), so the label has to survive.
    "revoker,41,30,149,149,1,20160101,20160131,0",
    "revoker,41,30,149,149,1,20160115,20160115,1",
    # survivor: one long plan whose coverage outlives the snapshot. Never dies, and is
    # censored at the snapshot rather than counted as a survival to infinity.
    "survivor,41,410,1788,1788,1,20160101,20170301,0",
    # unconfirmed: coverage ended 2016-12-28, three days before the snapshot. Whether a
    # renewal would have landed inside the 7-day tolerance is unobservable, so this is
    # censored -- calling it a death would be counting the end of the data as an event.
    "unconfirmed,41,30,149,149,1,20161128,20161228,0",
    # plan_switch: 149/30d until March, then 449/90d, then a 410-day plan. The leakage
    # probe -- a week in February must not know about the March plan.
    "plan_switch,41,30,149,149,1,20160101,20160131,0",
    "plan_switch,41,30,149,149,1,20160131,20160301,0",
    "plan_switch,41,90,449,449,1,20160301,20160530,0",
    "plan_switch,41,90,449,449,1,20160530,20160828,0",
    "plan_switch,41,410,1788,1788,1,20160828,20171012,0",
    # comped: a free month with no list price either. The amount has to fall back to
    # what this subscriber last actually paid, not to a median over their whole history
    # -- a median would be reading months that have not happened yet.
    "comped,41,30,149,149,1,20160101,20160201,0",
    "comped,41,30,0,0,1,20160201,20160301,0",
    # manual: never auto-renewing, so never in the mandate book, so never in the frame.
    "manual,32,90,298,298,0,20160101,20160401,0",
    # too_short: first seen two days before the snapshot and still covered. Censored
    # with no whole week behind it, so there is no week to expand.
    "too_short,41,30,149,149,1,20161229,20170129,0",
]

MEMBERS_HEADER = "msno,city,bd,gender,registered_via,registration_init_time"

MEMBERS_ROWS = [
    "lapser,1,28,male,7,20150101",
    "revoker,5,31,female,9,20140601",
    "survivor,1,44,male,7,20150301",
    "unconfirmed,1,22,female,3,20160101",
    "comped,1,35,male,7,20150101",
    "too_short,1,30,male,7,20150101",
    # `plan_switch` and `manual` are deliberately absent.
]


@pytest.fixture
def params() -> Params:
    real = load_params()
    return real.model_copy(
        update={"india": real.india.model_copy(update={"snapshot_date": SNAPSHOT})}
    )


@pytest.fixture
def interim(tmp_path: Path) -> Path:
    (tmp_path / "transactions.csv").write_text(
        "\n".join([TX_HEADER, *TX_ROWS, ""]), encoding="utf-8"
    )
    (tmp_path / "members_v3.csv").write_text(
        "\n".join([MEMBERS_HEADER, *MEMBERS_ROWS, ""]), encoding="utf-8"
    )
    out = tmp_path / "interim"
    out.mkdir()
    con = duckdb.connect()
    try:
        for name in ("transactions", "members"):
            ingest_table(con, next(s for s in SPECS if s.name == name), tmp_path, out)
    finally:
        con.close()
    return out


@pytest.fixture
def built(params: Params, interim: Path, tmp_path: Path):
    """The written parquet, read back -- a test against the in-memory table would pass
    even if the COPY wrote the wrong columns."""
    processed = tmp_path / "processed"
    report = periods.build(params=params, interim=interim, out_dir=processed)
    frame = (
        duckdb.connect()
        .execute(
            f"SELECT * FROM '{(processed / 'person_periods.parquet').as_posix()}' "
            "ORDER BY mandate_id, week_index"
        )
        .df()
    )
    return report, frame


@pytest.fixture
def report(built):
    return built[0]


@pytest.fixture
def frame(built) -> pd.DataFrame:
    return built[1]


def spell(frame: pd.DataFrame, mandate_id: str) -> pd.DataFrame:
    return frame[frame.mandate_id == mandate_id].sort_values("week_index")


# --------------------------------------------------------------------------------
# The shape a survival model needs.
# --------------------------------------------------------------------------------


def test_no_subscriber_has_rows_after_their_event(frame):
    """T1.4's stated gate. A row after the death week is a row describing a mandate that
    does not exist, and a model fit on those learns that dead mandates are safe."""
    for mandate_id, rows in frame.groupby("mandate_id"):
        events = rows.loc[rows.event, "week_index"]
        if not events.empty:
            assert rows.week_index.max() == events.max(), mandate_id


def test_a_spell_carries_exactly_one_event_or_none(frame):
    counts = frame.groupby("mandate_id").event.sum()
    assert set(counts.unique()) <= {0, 1}


def test_weeks_are_contiguous_from_zero(frame):
    """`week_index` is the duration covariate, so a hole in it is a hole in the baseline
    hazard -- and it would not look like one, it would look like a shorter spell."""
    for mandate_id, rows in frame.groupby("mandate_id"):
        weeks = sorted(rows.week_index)
        assert weeks == list(range(len(weeks))), mandate_id


def test_week_start_advances_exactly_seven_days(frame):
    for mandate_id, rows in frame.groupby("mandate_id"):
        starts = pd.to_datetime(rows.sort_values("week_index").week_start)
        gaps = starts.diff().dropna().dt.days.unique()
        assert set(gaps) <= {7}, mandate_id


# --------------------------------------------------------------------------------
# Who dies, when, and of what.
# --------------------------------------------------------------------------------


def test_a_lapse_is_labelled_a_lapse_on_the_week_it_happened(frame):
    """`lapser`'s coverage ended 2016-03-01, 60 days after their first transaction --
    week 8, and the last week they have."""
    rows = spell(frame, "lapser")
    assert len(rows) == 9
    assert list(rows.event) == [False] * 8 + [True]
    assert rows.iloc[-1].death_kind == DeathKind.LAPSE.value


def test_a_revocation_is_not_filed_as_a_lapse(frame):
    """The two endings recover at different rates (`q > r`), so collapsing them would
    price a revoked mandate as though it were merely expired."""
    rows = spell(frame, "revoker")
    assert len(rows) == 3
    assert rows.iloc[-1].event
    assert rows.iloc[-1].death_kind == DeathKind.REVOCATION.value


def test_a_survivor_is_censored_not_recorded_as_immortal(frame, report):
    """`survivor`'s coverage runs to 2017-03-01, past the snapshot. Their spell ends
    because the data ends, which is a different fact from surviving forever."""
    rows = spell(frame, "survivor")
    assert not rows.event.any()
    assert rows.week_index.max() == 51  # 365 days of 2016 // 7, whole weeks only
    assert report.censored_spells >= 1


def test_a_death_too_close_to_the_horizon_is_censored_not_counted(frame, report):
    """`unconfirmed` lost coverage three days before the snapshot. Confirming a death
    needs the full 7-day renewal tolerance to elapse, and it has not. Counting it would
    turn the end of the observation window into an event -- the same censoring mistake
    T1.2 2.3 refuses to make, one layer down."""
    rows = spell(frame, "unconfirmed")
    assert not rows.event.any()
    assert report.unconfirmed_deaths == 1


def test_a_one_off_purchase_never_reaches_the_frame(frame):
    """The frame stands on the mandate book, so T1.3's population is T1.4's population.
    Two definitions of who is in the book would be two populations."""
    assert "manual" not in set(frame.mandate_id)


def test_a_spell_with_no_whole_week_is_dropped_and_counted(frame, report):
    """`too_short` was first seen two days before the snapshot. There is no week of
    exposure to record, and inventing a partial one would put a guaranteed survival in
    the denominator for a reason that has nothing to do with the subscriber."""
    assert "too_short" not in set(frame.mandate_id)
    assert report.steps[-1].step == "with at least one observable week"
    assert report.steps[-1].subscribers == report.spells


def test_a_censored_spell_keeps_only_whole_weeks_but_a_dying_one_keeps_its_last(frame):
    """The asymmetry is deliberate. A subscriber observed for three days of a week was
    not at risk for that week; a subscriber who died three days into a week did die in
    it. Treating the two the same biases the hazard in whichever direction was chosen."""
    survivor = spell(frame, "survivor")
    last_start = pd.Timestamp(survivor.iloc[-1].week_start)
    assert last_start + pd.Timedelta(days=7) <= pd.Timestamp(SNAPSHOT) + pd.Timedelta(days=1)

    lapser = spell(frame, "lapser")
    assert pd.Timestamp(lapser.iloc[-1].week_start) < pd.Timestamp("2016-03-01")


# --------------------------------------------------------------------------------
# The rule this module exists to enforce: features may not see the future.
# --------------------------------------------------------------------------------


def test_a_week_cannot_see_a_plan_the_subscriber_had_not_bought_yet(frame):
    """`plan_switch` moves from 149/30d to 449/90d on 2016-03-01. Week 8 opens on
    2016-02-26. If it reports 449, every feature in this frame is reading the future and
    the model fit on it is worthless -- while looking excellent in cross-validation."""
    rows = spell(frame, "plan_switch").set_index("week_index")
    assert str(rows.loc[8, "week_start"])[:10] == "2016-02-26"
    assert float(rows.loc[8, "amount_inr"]) == 149.0
    assert int(rows.loc[8, "debit_frequency_days"]) == 30
    # 2016-04-01 is week 13, and by then the switch really has happened.
    assert str(rows.loc[13, "week_start"])[:10] == "2016-04-01"
    assert float(rows.loc[13, "amount_inr"]) == 449.0
    assert int(rows.loc[13, "debit_frequency_days"]) == 90


def test_cumulative_counts_only_count_what_has_already_happened(frame):
    rows = spell(frame, "plan_switch").set_index("week_index")
    assert int(rows.loc[0, "debits_so_far"]) == 1
    assert int(rows.loc[8, "debits_so_far"]) == 2
    assert int(rows.loc[13, "debits_so_far"]) == 3
    assert rows.debits_so_far.is_monotonic_increasing


def test_the_amount_falls_back_to_what_was_last_actually_paid(frame):
    """`comped`'s February row is free and states no list price. The mandate is still
    worth 149 -- that is what the subscriber paid in January. Falling back to a median
    over their whole history, as the snapshot book does, would be reading forward."""
    rows = spell(frame, "comped").set_index("week_index")
    assert float(rows.loc[5, "amount_inr"]) == 149.0
    assert float(rows.loc[0, "amount_inr"]) == 149.0


def test_the_coverage_clock_counts_down_and_resets_on_renewal(frame):
    """`days_to_coverage_end` is the whole of T1.6's naive baseline -- "risk is closeness
    to expiry". If it does not actually track the coverage end, the baseline the real
    model has to beat is not the baseline anybody claimed."""
    rows = spell(frame, "lapser").set_index("week_index")
    assert int(rows.loc[0, "days_to_coverage_end"]) == 30  # 2016-01-01 -> 2016-01-31
    assert int(rows.loc[4, "days_to_coverage_end"]) == 2  # 2016-01-29, two days left
    assert int(rows.loc[5, "days_to_coverage_end"]) == 25  # renewed: 2016-02-05 -> 03-01


def test_a_missing_member_row_leaves_a_null_rather_than_dropping_the_spell(frame):
    rows = spell(frame, "plan_switch")
    assert len(rows) > 0
    assert not rows.member_record_found.any()
    assert rows.account_age_days.isna().all()


def test_left_truncation_is_flagged_rather_than_assumed_away(frame):
    """A subscriber whose first observed transaction falls inside the first billing
    cycle of the log may have originated before the log did, so their `week_index` is
    weeks since observation, not weeks since origination. One is decidable, the other is
    not, and the flag is what keeps the difference visible."""
    assert bool(spell(frame, "lapser").left_truncated.all())
    assert not bool(spell(frame, "unconfirmed").left_truncated.any())


# --------------------------------------------------------------------------------
# Reproducibility.
# --------------------------------------------------------------------------------


def test_the_same_input_writes_the_same_bytes(params, interim, tmp_path):
    written = []
    for run in ("a", "b", "c"):
        out = tmp_path / run
        periods.build(params=params, interim=interim, out_dir=out)
        written.append((out / "person_periods.parquet").read_bytes())
    assert written[0] == written[1] == written[2]


def test_the_written_file_is_actually_in_the_order_it_claims(params, interim, tmp_path):
    """`preserve_insertion_order = false` lets DuckDB reorder almost everything, and the
    only thing standing between that and a shuffled file is the explicit ORDER BY in the
    COPY. This asserts the file that lands is sorted -- byte-identity alone would not
    notice a consistently-shuffled write."""
    out = tmp_path / "sorted"
    periods.build(params=params, interim=interim, out_dir=out)
    rows = (
        duckdb.connect()
        .execute(
            f"SELECT mandate_id, week_index FROM '{(out / 'person_periods.parquet').as_posix()}'"
        )
        .fetchall()
    )
    assert rows == sorted(rows)


def test_nothing_is_written_when_writing_is_off(params, interim, tmp_path):
    out = tmp_path / "dry"
    report = periods.build(params=params, interim=interim, out_dir=out, write=False)
    assert report.person_weeks > 0
    assert not out.exists()


def test_the_report_renders_every_number_it_holds(report):
    text = periods.format_report(report)
    assert f"{report.person_weeks:,}" in text
    assert f"{report.spells:,}" in text
    for bucket in report.hazard:
        assert bucket.label in text


# --------------------------------------------------------------------------------
# The committed sample, run through the real expansion.
# --------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def committed() -> Path:
    path = sample_dir()
    if not (path / "transactions.parquet").exists():
        pytest.skip("data/sample/ is not built -- run scripts/build_sample.py")
    return path


def test_the_committed_sample_expands_into_a_usable_frame(committed, tmp_path):
    """The T1.5 claim, one stage further on: CI regenerates the model's input frame from
    the committed slice, with no 1 GB download."""
    report = periods.build(interim=committed, out_dir=tmp_path, write=False)
    assert report.person_weeks > 50_000
    assert report.events > 0
    assert set(report.events_by_kind) == {DeathKind.LAPSE.value, DeathKind.REVOCATION.value}
    assert 0.0 < report.event_rate < 0.2


def test_the_hazard_is_not_flat_across_duration(committed, tmp_path):
    """If every duration bucket carried the same risk, `week_index` would be worthless
    as a covariate and the survival framing would have bought nothing over a plain
    cross-section. Worth finding out here rather than after fitting."""
    report = periods.build(interim=committed, out_dir=tmp_path, write=False)
    rates = [bucket.hazard for bucket in report.hazard if bucket.person_weeks]
    assert max(rates) > 2 * min(rates)
