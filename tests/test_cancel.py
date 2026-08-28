"""Cancel- and lapse-semantics tests (T1.2).

Every subscriber in the fixture below exists to pin one definitional decision. Neither
`q` nor `r` is a measurement of the data alone -- each is a measurement of the data
*under a definition*, and the definition is where a number gets quietly inflated. These
tests are what stops the definitions from drifting later.

Horizon is 2016-12-31 (the anchor subscriber's last purchase), so right-censoring is
testable without a 21M-row download.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from mandateguard.data.cancel import analyse, analyse_lapses
from mandateguard.data.ingest import SPECS, ingest_table

HEADER = (
    "msno,payment_method_id,payment_plan_days,plan_list_price,actual_amount_paid,"
    "is_auto_renew,transaction_date,membership_expire_date,is_cancel"
)

ROWS = [
    # anchor: never cancels; its last purchase defines the horizon.
    "anchor,41,30,129,129,1,20160101,20160201,0",
    "anchor,41,30,129,129,1,20161231,20170131,0",
    # plan_change: cancels and repurchases the next day. Not a lapse at all.
    "plan_change,41,30,129,129,1,20160110,20160210,1",
    "plan_change,41,30,149,149,1,20160111,20160211,0",
    # real_recovery: gone 40 days, then back after coverage had ended.
    "real_recovery,41,30,129,129,1,20160110,20160210,1",
    "real_recovery,41,30,129,129,1,20160219,20160320,0",
    # both: a next-day plan change AND a genuine 82-day return. The case that a single
    # "next purchase" lookup would misread as a plan change and stop at.
    "both,41,30,129,129,1,20160110,20160210,1",
    "both,41,30,149,149,1,20160111,20160211,0",
    "both,41,30,149,149,1,20160401,20160501,0",
    # gone: cancels, never returns.
    "gone,32,90,298,298,0,20160110,20160210,1",
    # cancel_twice: a cancel after a cancel is not a recovery.
    "cancel_twice,32,90,298,298,0,20160110,20160210,1",
    "cancel_twice,32,90,298,298,0,20160120,20160220,1",
    # dupe: two cancel rows on one day are one cancel. The second also carries the
    # 1970-01-01 epoch sentinel, which must not become this subscriber's expiry.
    "dupe,41,30,129,129,1,20160110,20160210,1",
    "dupe,41,30,129,129,1,20160110,19700101,1",
    # censored: cancels 11 days before the horizon, so 4w/12w/24w cannot see it.
    # Also carries the far-future expiry sentinel.
    "censored,41,30,129,129,1,20161220,20361015,1",
    # changed_mind: cancels at period end, then repurchases 15 days later -- while
    # still covered. A save, not a win-back: the service never actually stopped.
    "changed_mind,41,30,129,129,1,20160510,20160610,1",
    "changed_mind,41,30,129,129,1,20160525,20160610,0",
    # late_lapse: coverage ends 21 days before the horizon. Passively lapsed, and too
    # late to observe a four-week recovery.
    "late_lapse,41,30,129,129,1,20161201,20161210,0",
    # late_renewal: renews two days after expiry. A slow settlement, not a churn and
    # a win-back.
    "late_renewal,41,30,129,129,1,20160301,20160401,0",
    "late_renewal,41,30,129,129,1,20160403,20160503,0",
]


@pytest.fixture
def parquet(tmp_path: Path) -> Path:
    (tmp_path / "transactions.csv").write_text("\n".join([HEADER, *ROWS, ""]), encoding="utf-8")
    out = tmp_path / "interim"
    out.mkdir()
    con = duckdb.connect()
    try:
        ingest_table(con, next(s for s in SPECS if s.name == "transactions"), tmp_path, out)
    finally:
        con.close()
    return out / "transactions.parquet"


@pytest.fixture
def report(parquet: Path):
    return analyse(parquet)


@pytest.fixture
def lapses(parquet: Path):
    return analyse_lapses(parquet)


# --------------------------------------------------------------------------------
# Active death: recovery after a cancellation.
# --------------------------------------------------------------------------------


def test_horizon_is_the_last_transaction_not_today(report):
    """Every censoring decision hangs off this date. If it silently became `today`,
    nothing would ever be censored and both rates would look better than they are."""
    assert str(report.horizon) == "2016-12-31"


def test_same_day_cancel_rows_collapse_to_one_event(report):
    """10 cancel rows, 9 cancels: `dupe` cancelled once, on two rows."""
    assert report.cancel_rows == 10
    assert report.cancel_events == 9
    assert report.cancel_subscribers == 8


def test_next_day_repurchase_is_a_plan_change_not_a_recovery(report):
    """`plan_change` and `both` both repurchase inside the grace window. `plan_change`
    never returns after that, so it must not appear in any recovered count."""
    assert report.plan_change_events == 2
    assert report.rate_for(84).recovered == 3  # real_recovery, both, changed_mind


def test_a_plan_change_does_not_hide_a_later_real_lapse(report):
    """`both` changes plan on day 1 and then lapses for real until day 82. It has to be
    counted in *both* places -- this is the reason there are two ASOF joins."""
    assert report.plan_change_events == 2  # includes `both`
    assert report.rate_for(84).recovered == 3  # also includes `both`
    assert report.rate_for(28).recovered == 1  # but `both` is not back within 4 weeks


def test_a_cancel_after_a_cancel_is_not_a_return(report):
    """`cancel_twice` transacts again -- but the transaction is another cancellation.
    Only `is_cancel = 0` moves money, so only that counts as coming back."""
    assert report.never_returned == 5  # gone, cancel_twice x2, dupe, censored


def test_returning_while_still_covered_is_not_a_win_back(report):
    """`changed_mind` cancels at period end and repurchases before that period ends.
    It recovered, but it was never actually without service -- so it belongs in
    `recovered` and not in `recovered_uncovered`. Conflating the two would let a
    retention system claim credit for a customer who never left."""
    horizon_window = report.rate_for(84)
    assert horizon_window.recovered == 3
    assert horizon_window.recovered_uncovered == 2  # real_recovery and both only
    assert horizon_window.uncovered_rate < horizon_window.rate


def test_events_too_close_to_the_horizon_are_censored_not_counted_as_failures(report):
    """`censored` cancels on 2016-12-20. At 7 days it is observable; past that it is
    not, and putting it in the denominator would understate the rate for a reason that
    has nothing to do with the subscriber."""
    assert report.rate_for(7).censored == 0
    assert report.rate_for(7).eligible == 9
    for window in (28, 84, 168):
        assert report.rate_for(window).censored == 1
        assert report.rate_for(window).eligible == 8


def test_recovery_rate_grows_with_the_window(report):
    rates = [report.rate_for(w).rate for w in (7, 28, 84, 168)]
    assert rates == sorted(rates)
    assert report.rate_for(84).rate == pytest.approx(3 / 8)


def test_segments_partition_the_events(report):
    """auto_renew and manual must add up to all, or a segment is silently dropping rows."""
    for window in (7, 28, 84, 168):
        every = report.rate_for(window)
        auto = report.rate_for(window, "auto_renew")
        manual = report.rate_for(window, "manual")
        assert auto.eligible + manual.eligible == every.eligible
        assert auto.recovered + manual.recovered == every.recovered


def test_auto_renew_cancels_are_reported_separately(report):
    """A mandate cancelled while auto-renew was on is a different animal from one that
    lapsed with auto-renew already off -- the first is a live mandate being revoked."""
    assert report.rate_for(84, "auto_renew").eligible == 5  # 6 events, 1 censored
    assert report.rate_for(84, "manual").eligible == 3
    assert report.rate_for(84, "manual").recovered == 0


def test_expiry_sentinels_are_counted(report):
    sentinel = report.sentinels[0]
    assert sentinel.epoch_rows == 1
    assert sentinel.epoch_subscribers == 1
    assert sentinel.far_future_rows == 1
    assert str(sentinel.far_future_max) == "2036-10-15"


def test_grace_window_is_a_parameter_not_a_hard_coded_truth(parquet, report):
    """With no grace window at all, the next-day repurchases become "recoveries" and the
    rate jumps. The number is only meaningful next to the definition that produced it."""
    strict = analyse(parquet, grace_days=0)
    assert strict.plan_change_events == 0
    assert strict.rate_for(84).recovered == 4  # plan_change and both now count
    assert strict.rate_for(84).rate > report.rate_for(84).rate


# --------------------------------------------------------------------------------
# Passive death: recovery after coverage simply ran out.
# --------------------------------------------------------------------------------


def test_lapses_and_cancels_are_different_populations(lapses):
    """12 coverage gaps, of which 4 follow a cancellation. Only the other 8 calibrate
    `q`; feeding all 12 into one number would merge the two deaths back together."""
    assert lapses.coverage_gaps == 12
    assert lapses.active_gaps == 4
    assert lapses.passive_lapses == 8
    assert lapses.lapsed_subscribers == 7


def test_still_covered_at_the_horizon_is_not_a_lapse(lapses):
    """`anchor`'s last purchase runs to 2017-01-31 and `censored`'s to 2036-10-15. Both
    are alive when the data stops. Counting them as lapsed would be censoring bias
    wearing a different costume."""
    assert lapses.lapsed_subscribers == 7  # every subscriber except `gone` and `censored`
    assert lapses.rate_for(84).eligible == 7


def test_a_late_renewal_is_not_a_lapse_and_a_recovery(parquet, lapses):
    """`late_renewal` renews two days after expiry. Inside the tolerance that is one
    continuous subscription; with the tolerance set to zero it splits into a lapse and
    a same-week win-back, and `q` inflates for a purely clerical reason."""
    assert lapses.rate_for(7).recovered == 0

    impatient = analyse_lapses(parquet, tolerance_days=0)
    assert impatient.passive_lapses == 9  # one more than with the 3-day tolerance
    assert impatient.rate_for(7).recovered == 1


def test_lapse_events_are_censored_against_the_horizon(lapses):
    """`late_lapse` loses coverage on 2016-12-10, 21 days before the data ends."""
    assert lapses.rate_for(7).censored == 0
    assert lapses.rate_for(7).eligible == 8
    for window in (28, 84, 168):
        assert lapses.rate_for(window).censored == 1
        assert lapses.rate_for(window).eligible == 7


def test_lapse_recovery_rate_grows_with_the_window(lapses):
    rates = [lapses.rate_for(w).rate for w in (7, 28, 84, 168)]
    assert rates == sorted(rates)
    assert lapses.rate_for(84).rate == pytest.approx(1 / 7)


def test_the_epoch_sentinel_does_not_become_a_coverage_end(lapses):
    """`dupe`'s second row claims expiry 1970-01-01. If that were taken literally the
    subscriber would appear to have lapsed 46 years before subscribing."""
    assert lapses.coverage_gaps == 12  # dupe contributes exactly one gap, at 2016-02-10
