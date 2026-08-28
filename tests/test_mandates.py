"""Mandate-book tests (T1.3).

Section 2 measured the data. Section 3 *decides* things about it, and a decision is
exactly the kind of thing that rots quietly: nobody notices when a filter starts
dropping twice as many subscribers, or when an imputed billing cycle silently becomes
the global default for everyone. Every subscriber in the fixture below exists to pin one
such decision, so that changing it breaks a named test instead of moving a headline
number nobody re-derives.

The fixture is 20-odd rows, not the 21.5M-row download, so this runs in CI.

Snapshot is 2016-12-31 rather than the configured 2017-02-28, because the snapshot
itself is a decision -- transactions after it must be invisible -- and that is only
testable when the fixture has rows on both sides of it.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pandas as pd
import pytest

from mandateguard.data.ingest import SPECS, ingest_table
from mandateguard.data.mandates import build
from mandateguard.models import Rail
from mandateguard.policy.loader import Params, load_params

SNAPSHOT = "2016-12-31"

TX_HEADER = (
    "msno,payment_method_id,payment_plan_days,plan_list_price,actual_amount_paid,"
    "is_auto_renew,transaction_date,membership_expire_date,is_cancel"
)

TX_ROWS = [
    # active: covered past the snapshot. The ordinary case everything else deviates from.
    "active,41,30,149,149,1,20161201,20170101,0",
    "active,41,30,149,149,1,20161101,20161201,0",
    # expired: coverage ran out before the snapshot. Still a mandate, still in the book --
    # an expired mandate is precisely what this system exists to act on.
    "expired,41,30,129,129,1,20161001,20161101,0",
    # cancelled: the latest transaction revokes. Status comes from the latest row, not
    # from whether a cancel ever appeared in the history.
    "cancelled,41,30,129,129,1,20160801,20160901,0",
    "cancelled,41,30,129,129,1,20161201,20170101,1",
    # renewed_after_cancel: cancelled once, then renewed. Its *current* state is active,
    # so a history-wide `any(is_cancel)` would misclassify it.
    "renewed_after_cancel,41,30,129,129,1,20160801,20160901,1",
    "renewed_after_cancel,41,30,129,129,1,20161215,20170115,0",
    # manual: never auto-renewing. A one-off purchase is not a standing authorisation.
    "manual,32,90,298,298,0,20161201,20170301,0",
    # epoch: the 1970-01-01 sentinel as its latest expiry. No real coverage end.
    "epoch,41,30,129,129,1,20161201,19700101,0",
    # no_amount: every row free. Mandate.amount_inr requires gt=0, so a zero would be a
    # fiction; this subscriber must be filtered, not defaulted.
    "no_amount,41,30,0,0,1,20161201,20170101,0",
    # list_price_fallback: latest row paid nothing (a comped month) but states a list
    # price. The list price is what the next debit will be worth.
    "list_price_fallback,41,30,129,0,1,20161201,20170101,0",
    # typical_fallback: latest row has neither paid amount nor list price, but the
    # subscriber has a payment history to fall back on.
    "typical_fallback,41,30,99,99,1,20160901,20161001,0",
    "typical_fallback,41,30,99,99,1,20161001,20161101,0",
    "typical_fallback,41,30,0,0,1,20161201,20170101,0",
    # modal_cycle: latest row states no plan length, but the subscriber's own history
    # does. Their modal cycle beats the global default.
    "modal_cycle,41,90,447,447,1,20160901,20161130,0",
    "modal_cycle,41,90,447,447,1,20161130,20170228,0",
    "modal_cycle,41,0,447,447,1,20161201,20170301,0",
    # span_cycle: no plan length anywhere in the history, but the expiry span states one.
    "span_cycle,41,0,398,398,1,20161201,20170301,0",
    # default_cycle: no plan length, and an expiry span too long to be a billing cycle.
    # Only here does the configured global default apply.
    "default_cycle,41,0,129,129,1,20161201,20360101,0",
    # future_only_row: its December row is the latest *before* the snapshot; the January
    # row is after it and must not be read at all.
    "future_only_row,41,30,129,129,1,20161210,20170110,0",
    "future_only_row,41,30,999,999,1,20170115,20170215,1",
    # same_day: two rows written the same day. The one granting more coverage is the one
    # that took effect.
    "same_day,41,30,129,129,1,20161220,20170120,0",
    "same_day,41,30,129,129,1,20161220,20170220,0",
]

MEMBERS_HEADER = "msno,city,bd,gender,registered_via,registration_init_time"

MEMBERS_ROWS = [
    "active,1,28,male,7,20150101",
    "expired,5,0,,9,20150301",  # bd = 0: not an age
    "cancelled,13,1051,female,3,20140101",  # bd = 1051: not an age
    "renewed_after_cancel,1,-7,,7,20150601",  # bd negative: not an age
    "manual,1,35,male,7,20150101",
    "epoch,1,35,male,7,20150101",
    "no_amount,1,35,male,7,20150101",
    "list_price_fallback,1,44,female,9,20150101",
    "typical_fallback,1,90,male,7,20150101",  # exactly the upper bound: kept
    "modal_cycle,1,13,female,7,20150101",  # exactly the lower bound: kept
    "span_cycle,1,30,male,7,20150101",
    "default_cycle,1,30,male,7,20150101",
    "same_day,1,30,male,7,20150101",
    # `future_only_row` is deliberately absent: a missing demographic row must not
    # delete a mandate that the transaction log says exists.
]


@pytest.fixture
def params() -> Params:
    """The real config, with only the snapshot moved back to the fixture's horizon.

    Everything else stays as shipped, so these tests fail if a shipped constant changes
    -- which is the point: the constants are the thing under test.
    """
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
def book(params: Params, interim: Path, tmp_path: Path):
    """The written parquet, read back. Reading back is deliberate: a test against the
    in-memory table would pass even if the COPY wrote the wrong columns."""
    processed = tmp_path / "processed"
    report = build(params=params, interim=interim, out_dir=processed)
    rows = (
        duckdb.connect()
        .execute(f"SELECT * FROM '{(processed / 'mandates.parquet').as_posix()}'")
        .df()
    )
    return report, rows.set_index("mandate_id")


@pytest.fixture
def report(book):
    return book[0]


@pytest.fixture
def rows(book):
    return book[1]


# --------------------------------------------------------------------------------
# Who is in the book, and why the others are not.
# --------------------------------------------------------------------------------


def test_the_filter_chain_is_reported_step_by_step(report):
    """docs/mapping.md 1 promised that whichever join T1.4 uses would be stated with its
    surviving row count. The counts have to come out of the code that does the
    filtering, or they drift from it the first time a filter changes."""
    assert [step.subscribers for step in report.steps] == [14, 13, 12, 11, 11]
    assert report.mandates == 11


def test_a_one_off_purchase_is_not_a_mandate(rows):
    """`manual` never had auto-renew on. There is no standing authorisation to protect,
    so there is nothing for this system to decide about it."""
    assert "manual" not in rows.index


def test_a_mandate_with_no_debit_amount_is_dropped_not_defaulted(rows):
    """`no_amount` is free on every row. Mandate.amount_inr requires gt=0, and inventing
    an amount here would put a fabricated rupee number into every downstream ranking."""
    assert "no_amount" not in rows.index


def test_the_epoch_expiry_is_not_a_coverage_end(rows):
    """1970-01-01 is what a system writes when it has no date. Kept, it would produce a
    mandate whose cycle ended 46 years before it started."""
    assert "epoch" not in rows.index


def test_a_missing_member_row_does_not_delete_a_mandate(report, rows):
    """`future_only_row` has no demographic record. The members join is LEFT precisely
    so that a missing city cannot silently shrink the book."""
    assert "future_only_row" in rows.index
    assert not bool(rows.loc["future_only_row", "member_record_found"])
    assert report.members_matched == report.mandates - 1


def test_one_row_per_subscriber_not_per_transaction(rows):
    """A mandate is a standing authorisation; the transaction log is its history."""
    assert rows.index.is_unique


# --------------------------------------------------------------------------------
# The snapshot, and status as of it.
# --------------------------------------------------------------------------------


def test_transactions_after_the_snapshot_are_invisible(rows):
    """`future_only_row` cancels in January 2017. Read from a 2016-12-31 snapshot it is
    an active mandate paying 129 -- the January row has not happened yet. Leaking it
    would be look-ahead: the book would know an outcome the policy could not have."""
    assert rows.loc["future_only_row", "status"] == "active"
    assert rows.loc["future_only_row", "amount_inr"] == pytest.approx(129.0)


def test_status_comes_from_the_latest_row_not_from_the_history(rows):
    """`renewed_after_cancel` cancelled in August and renewed in December. It is a live
    mandate today; a history-wide `any(is_cancel)` would write it off."""
    assert rows.loc["renewed_after_cancel", "status"] == "active"
    assert rows.loc["cancelled", "status"] == "cancelled"


def test_coverage_ending_before_the_snapshot_is_expired(rows):
    assert rows.loc["expired", "status"] == "expired"
    assert rows.loc["active", "status"] == "active"


def test_a_same_day_tie_is_broken_by_the_longer_coverage(rows):
    """Two rows written 2016-12-20. Of two rows on one day, the one granting more
    coverage is the one that took effect."""
    assert str(rows.loc["same_day", "current_end"])[:10] == "2017-02-20"


def test_status_counts_partition_the_book(report):
    assert sum(report.by_status.values()) == report.mandates


# --------------------------------------------------------------------------------
# Amount, cycle, and what gets imputed.
# --------------------------------------------------------------------------------


def test_amount_falls_back_to_list_price_then_to_the_subscribers_own_history(rows):
    """A comped month should not price the mandate at zero, and a subscriber with no
    list price still has a payment history that says what they normally pay."""
    assert rows.loc["active", "amount_inr"] == pytest.approx(149.0)
    assert rows.loc["list_price_fallback", "amount_inr"] == pytest.approx(129.0)
    assert rows.loc["typical_fallback", "amount_inr"] == pytest.approx(99.0)


def test_the_ntd_to_inr_scale_is_uniform(params, interim, tmp_path):
    """Doubling the scale doubles every rupee number and changes nothing else. That is
    the whole claim behind `ntd_to_inr: 1.0` -- it moves the headline, not the ranking."""
    doubled = params.model_copy(
        update={"india": params.india.model_copy(update={"ntd_to_inr": 2.0})}
    )
    base = build(params=params, interim=interim, out_dir=tmp_path / "a")
    twice = build(params=doubled, interim=interim, out_dir=tmp_path / "b")
    assert twice.mandates == base.mandates
    # Not exact: every amount is rounded to paise per row, so doubling the scale and
    # doubling the total can disagree by at most one paisa per mandate. The claim being
    # made is that the scale is linear, not that it is paise-exact.
    assert twice.total_ltv_inr == pytest.approx(base.total_ltv_inr * 2, abs=0.01 * base.mandates)


def test_a_stated_plan_length_is_used_as_is(rows):
    assert int(rows.loc["active", "debit_frequency_days"]) == 30
    assert not bool(rows.loc["active", "frequency_imputed"])


def test_a_missing_plan_length_falls_back_to_the_subscribers_modal_cycle(rows):
    """`modal_cycle` states 0 days on its latest row but bought 90-day plans twice
    before. Their own history beats the global default."""
    assert int(rows.loc["modal_cycle", "debit_frequency_days"]) == 90
    assert bool(rows.loc["modal_cycle", "frequency_imputed"])


def test_then_to_the_observed_expiry_span(rows):
    """`span_cycle` has no plan length anywhere, but bought coverage to 2017-03-01 on
    2016-12-01 -- the span states the cycle even when the column does not."""
    assert int(rows.loc["span_cycle", "debit_frequency_days"]) == 90


def test_an_implausible_span_is_not_a_billing_cycle(params, rows):
    """`default_cycle` bought coverage to 2036. A 19-year "billing cycle" is an artifact,
    and using it would price the mandate at one debit per two decades."""
    assert (
        int(rows.loc["default_cycle", "debit_frequency_days"])
        == params.india.default_debit_frequency_days
    )


def test_imputation_is_flagged_wherever_it_happened(report, rows):
    """Three subscribers had no stated plan length. Downstream has to be able to tell an
    imputed cycle from a measured one, or an assumption becomes a fact."""
    assert report.imputed_frequency == 3
    assert set(rows.index[rows["frequency_imputed"].astype(bool)]) == {
        "modal_cycle",
        "span_cycle",
        "default_cycle",
    }


# --------------------------------------------------------------------------------
# The overlays: rail, validity, L and R.
# --------------------------------------------------------------------------------


def test_the_rail_is_assigned_deterministically(params, interim, tmp_path):
    """The rail is invented, not recovered -- KKBox never published what
    `payment_method_id` means. Invented is tolerable; *unstable* is not, because then
    two runs of the same config would price the same mandate differently."""
    first = build(params=params, interim=interim, out_dir=tmp_path / "a")
    second = build(params=params, interim=interim, out_dir=tmp_path / "b")
    assert first.by_rail == second.by_rail


def test_every_assigned_rail_is_a_real_rail(report):
    assert set(report.by_rail) <= {rail.value for rail in Rail}
    assert sum(report.by_rail.values()) == report.mandates


def test_a_rail_mix_of_one_puts_everything_on_that_rail(params, interim, tmp_path):
    """The mix is config, not code. With the whole book on cards, no hash bucket may
    leak a mandate onto another rail."""
    on_cards = params.model_copy(
        update={"india": params.india.model_copy(update={"rail_mix": {Rail.CARD: 1.0}})}
    )
    report = build(params=on_cards, interim=interim, out_dir=tmp_path / "cards")
    assert report.by_rail == {Rail.CARD.value: report.mandates}


def test_upi_autopay_is_withheld_above_the_afa_threshold(params, interim, tmp_path):
    """UPI AutoPay needs additional-factor authentication above a per-debit ceiling, so
    a mandate above it cannot be on that rail. At the shipped `ntd_to_inr: 1.0` this
    binds on nothing, which is exactly why it needs a test that forces it to bind."""
    strict = params.model_copy(
        update={"india": params.india.model_copy(update={"upi_autopay_afa_threshold_inr": 100.0})}
    )
    report = build(params=strict, interim=interim, out_dir=tmp_path / "afa")
    rows = (
        duckdb.connect()
        .execute(
            f"SELECT method, amount_inr FROM '{(tmp_path / 'afa' / 'mandates.parquet').as_posix()}'"
        )
        .df()
    )
    above = rows[rows["amount_inr"] > 100.0]
    assert len(above) > 0
    assert Rail.UPI_AUTOPAY.value not in set(above["method"])
    assert report.mandates == len(rows)  # withholding a rail must not drop a mandate


def test_mandate_validity_extends_past_the_cycle_end(params, rows):
    """`expire_by` is an overlay -- KKBox has no mandate-validity column at all. What is
    testable is that it is derived from the cycle end and never precedes it."""
    delta = (rows["expire_by"] - rows["current_end"]).dt.days
    assert set(delta) == {params.india.mandate_validity_days}


def test_l_is_horizon_bounded_not_lifetime(params, rows):
    """L is revenue over `horizon.weeks`, not over the mandate's whole life. A lifetime L
    would price decisions against revenue the 12-week simulation never observes."""
    horizon_days = params.horizon.weeks * 7
    active = rows.loc["active"]
    expected = active["amount_inr"] * (horizon_days / active["debit_frequency_days"])
    assert active["ltv_remaining_inr"] == pytest.approx(expected, abs=0.01)


def test_r_is_a_configured_fraction_of_l(params, rows):
    """R has no public measurement behind it -- it is `swept: true`. Pinning it to L is
    the honest version: one knob, visibly derived, rather than a second invented number."""
    ratio = rows["reachability_value_inr"] / rows["ltv_remaining_inr"]
    assert ratio.max() == pytest.approx(params.india.reachability_fraction_of_ltv, abs=0.01)


def test_the_recovery_parameters_travel_with_the_mandate(params, rows):
    """q and r come from T1.2 and are copied onto every row, so a mandate read back out
    of the parquet reconstructs a valid `models.Mandate` without needing the config."""
    assert set(rows["recovery_after_lapse"]) == {params.recovery.after_lapse}
    assert set(rows["recovery_after_revocation"]) == {params.recovery.after_revocation}
    assert (rows["recovery_after_lapse"] > rows["recovery_after_revocation"]).all()


# --------------------------------------------------------------------------------
# Age: kept as missing, not repaired.
# --------------------------------------------------------------------------------


def test_implausible_ages_become_null_rather_than_deleting_the_subscriber(report, rows):
    """`bd` holds 0, 1051 and -7 in the fixture. None is an age; all three subscribers
    are still real mandates. Nulling the field keeps the mandate and loses only the
    claim -- dropping the row would throw away a mandate over a demographic typo."""
    for bad in ("expired", "cancelled", "renewed_after_cancel"):
        assert bad in rows.index
        assert pd.isna(rows.loc[bad, "age_years"])
    assert report.age_known == 7


def test_the_plausible_range_is_inclusive_at_both_ends(params, rows):
    """`modal_cycle` is 13 and `typical_fallback` is 90 -- exactly the configured bounds.
    An off-by-one here would silently discard the edges of the age distribution."""
    low, high = params.india.plausible_age_years
    assert rows.loc["modal_cycle", "age_years"] == low
    assert rows.loc["typical_fallback", "age_years"] == high


# --------------------------------------------------------------------------------
# The report itself.
# --------------------------------------------------------------------------------


def test_the_report_is_markdown_ready_to_paste(report):
    """The numbers are due in docs/mapping.md. Retyped numbers drift from the data they
    claim to describe, so the report renders itself."""
    from mandateguard.data.mandates import format_report

    text = format_report(report)
    assert f"{report.mandates:,} mandates" in text
    assert str(report.snapshot) in text
    for rail in report.by_rail:
        assert f"`{rail}`" in text


def test_nothing_is_written_when_writing_is_off(params, interim, tmp_path):
    """`docs/mapping.md` 3 gates the parquet on the prose being written. A dry run has to
    actually be dry, or the gate is decorative."""
    out = tmp_path / "dry"
    report = build(params=params, interim=interim, out_dir=out, write=False)
    assert report.mandates == 11
    assert not out.exists()


def test_an_all_upi_mix_refuses_rather_than_quietly_breaking_the_afa_rule(
    params, interim, tmp_path
):
    """If the entire book is on UPI AutoPay, a debit above the AFA ceiling has nowhere to
    go. Silently leaving it on UPI would break the rule the threshold exists to enforce,
    so the build refuses and names the two ways out."""
    all_upi = params.model_copy(
        update={"india": params.india.model_copy(update={"rail_mix": {Rail.UPI_AUTOPAY: 1.0}})}
    )
    with pytest.raises(ValueError, match="no rail to fall back to"):
        build(params=all_upi, interim=interim, out_dir=tmp_path / "upi")
