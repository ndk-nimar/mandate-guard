"""T1.2 -- what `is_cancel` actually means, measured instead of assumed.

A cancellation is not a churn. In a recurring-mandate book the two are different
events with different prices: a cancelled subscriber who transacts again was never
lost, and counting them as lost inflates every saving this system later claims.

This module measures that difference on the KKBox transaction log and produces the
`q` in `config/params.yaml` (`recovery.after_lapse`) -- the probability that a lapsed
mandate comes back on its own, with no intervention. Until this ran, `q` was the
provisional 0.35 with nothing behind it.

Three definitional choices are made here, and each one moves the number:

1. **The unit is a cancel *event*, not a cancel row.** Rows are collapsed to
   `(msno, transaction_date)`. A subscriber with two cancel rows on one day cancelled
   once; counting rows would weight noisy accounts more heavily than quiet ones.

2. **"Came back" means a later non-cancel transaction.** A cancel followed by another
   cancel is not a recovery. `is_cancel = 0` is the only row shape that represents
   money actually moving.

3. **A repurchase inside `GRACE_DAYS` is a plan change, not a recovery.** Cancel and
   re-subscribe on the same day (or the next) is one administrative act -- an upgrade,
   a card swap, a term change -- and the subscriber never lapsed at all. Counting those
   as recoveries is the easiest way to overstate the rate, so they are excluded from it
   and reported separately as their own number.

The fourth choice is about honesty rather than semantics: **right-censoring**. A
subscriber who cancels five days before the data ends cannot be observed recovering
within twelve weeks. Including them puts a guaranteed non-recovery in the denominator
and drags the rate down for a reason that has nothing to do with subscriber behaviour.
Every rate here therefore counts only events with a full window of observation left
(`event_date + window <= horizon`), and reports how many events that discarded.

Two deaths, not one
-------------------

The first run of this module returned an answer and then invalidated the question. In
856,851 cancel rows, 856,841 also carry `is_auto_renew = 1`: a KKBox cancellation is
almost by definition a customer switching off a live auto-renewing subscription. That
is an *active* death -- the analogue of a mandate revocation (`r` in
`docs/problem.md` 6.2), not of a passive lapse (`q`).

Writing the cancel number into `recovery.after_lapse` would therefore have put a
revocation measurement into the lapse slot: the exact mis-calibration this project's
documentation discipline exists to catch. So this module measures both deaths:

* `analyse()` -- recovery after an **active cancel**, the upper bound on `r`.
* `analyse_lapses()` -- recovery after **coverage simply ran out** with no cancel on
  record, which is `q`.

The lapse side reconstructs each subscriber's coverage timeline: every transaction
restates `membership_expire_date`, so the most recent one carries the current end of
coverage. A gap is a subscriber-day where the next transaction lands more than
`RENEWAL_TOLERANCE_DAYS` after coverage ended -- the tolerance exists because a renewal
that posts a few days late is a late renewal, not a lapse and a win-back.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
from pydantic import BaseModel

from mandateguard.data.paths import interim_dir

GRACE_DAYS = 1
"""Repurchase within this many days of the cancel is a plan change, not a recovery."""

WINDOWS: tuple[int, ...] = (7, 28, 84, 168)
"""Follow-up windows in days, as multiples of a week: 1, 4, 12 and 24 weeks. 84 is the
headline because it is exactly `horizon.weeks: 12` from `config/params.yaml` -- the
window the evaluation harness rolls the world forward over. A recovery the harness
would never live long enough to see should not be counted in a parameter the harness
consumes."""

FAR_FUTURE_AFTER = date(2018, 12, 31)
"""Expiries past this are not plausible for 2015-2017 plans; they are the far-future
artifacts docs/mapping.md flagged after T1.1."""

EPOCH = date(1970, 1, 1)
"""Unix epoch: a missing value wearing a date's clothes."""

RENEWAL_TOLERANCE_DAYS = 7
"""A transaction landing within this many days of coverage ending is a late renewal,
not a lapse followed by a recovery.

This is the single most load-bearing definitional choice in the module: `q` at 84 days
moves from 0.789 at a zero-day tolerance to 0.248 at thirty, a factor of three, on data
that never changed. Seven days is chosen because it is this system's own decision
cadence -- `horizon.weeks` counts in weeks, the harness hands the policy one budget per
week, and a gap that opens and closes inside a single week is one the policy could not
have acted on even with perfect foresight. A lapse the system cannot act on is not a
lapse the system should be calibrated against. The full sensitivity table is in
`docs/mapping.md` 2 rather than hidden behind this constant."""


class SentinelReport(BaseModel):
    """How much of a table's `membership_expire_date` column is not a real expiry."""

    table: str
    rows: int
    horizon: date  # latest transaction_date -- the table's own end of observation
    epoch_rows: int
    epoch_subscribers: int
    far_future_rows: int
    far_future_subscribers: int
    far_future_max: date | None

    @property
    def epoch_share(self) -> float:
        return self.epoch_rows / self.rows if self.rows else 0.0

    @property
    def far_future_share(self) -> float:
        return self.far_future_rows / self.rows if self.rows else 0.0


class RecoveryRate(BaseModel):
    """Recovery within one window for one segment, with censored events excluded."""

    window_days: int
    segment: str  # "all" | "auto_renew" | "manual"
    eligible: int  # events with a full window of observation left
    censored: int  # events dropped because the window ran past the horizon
    recovered: int
    recovered_uncovered: int = 0
    """Recoveries where the return purchase landed *after* the cancelled membership's
    own expiry -- the subscriber was genuinely without service in between. The rest
    changed their mind while still covered, which is a save, not a win-back."""

    @property
    def rate(self) -> float:
        return self.recovered / self.eligible if self.eligible else 0.0

    @property
    def uncovered_rate(self) -> float:
        return self.recovered_uncovered / self.eligible if self.eligible else 0.0


class CancelReport(BaseModel):
    """Everything T1.2 has to answer, in one object."""

    total_rows: int
    cancel_rows: int
    cancel_events: int
    cancel_subscribers: int
    horizon: date
    plan_change_events: int  # repurchase inside the grace window
    never_returned: int  # no later non-cancel transaction at all, ever
    rates: list[RecoveryRate]
    sentinels: list[SentinelReport]

    @property
    def plan_change_share(self) -> float:
        return self.plan_change_events / self.cancel_events if self.cancel_events else 0.0

    def rate_for(self, window_days: int, segment: str = "all") -> RecoveryRate:
        return next(r for r in self.rates if r.window_days == window_days and r.segment == segment)


def _transactions_view(con: duckdb.DuckDBPyConnection, parquet: Path, name: str = "tx") -> None:
    """Rows with an unparseable transaction_date are excluded: an event with no date
    cannot be placed on a timeline, so it can neither recover nor fail to."""
    con.execute(
        f"CREATE OR REPLACE TEMP VIEW {name} AS "
        f"SELECT * FROM '{parquet.as_posix()}' WHERE transaction_date IS NOT NULL"
    )


def horizon_of(con: duckdb.DuckDBPyConnection, parquet: Path) -> date:
    """The table's own end of observation. Nothing after this date can be seen."""
    _transactions_view(con, parquet)
    row = con.execute("SELECT max(transaction_date) FROM tx").fetchone()
    assert row is not None
    return row[0]


def sentinel_report(con: duckdb.DuckDBPyConnection, parquet: Path, table: str) -> SentinelReport:
    """Count the two expiry values that are not expiries: the epoch and the far future."""
    _transactions_view(con, parquet, "sentinel_tx")
    epoch = f"membership_expire_date = DATE '{EPOCH}'"
    far = f"membership_expire_date > DATE '{FAR_FUTURE_AFTER}'"
    row = con.execute(
        f"""
        SELECT count(*),
               max(transaction_date),
               count(*) FILTER (WHERE {epoch}),
               count(DISTINCT msno) FILTER (WHERE {epoch}),
               count(*) FILTER (WHERE {far}),
               count(DISTINCT msno) FILTER (WHERE {far}),
               max(membership_expire_date)
        FROM sentinel_tx
        """
    ).fetchone()
    assert row is not None
    return SentinelReport(
        table=table,
        rows=row[0],
        horizon=row[1],
        epoch_rows=row[2],
        epoch_subscribers=row[3],
        far_future_rows=row[4],
        far_future_subscribers=row[5],
        far_future_max=row[6],
    )


def build_cancel_events(
    con: duckdb.DuckDBPyConnection, parquet: Path, grace_days: int = GRACE_DAYS
) -> None:
    """One row per cancel event, carrying the gap to the next purchase.

    Two ASOF joins rather than one. The first finds the very next purchase of any kind,
    which is what identifies a plan change. The second skips past the grace window to
    find the next purchase that could genuinely be a recovery -- necessary because a
    subscriber can switch plan on Tuesday and still lapse for real in April, and a
    single "next purchase" lookup would see only the Tuesday and call the case closed.
    """
    _transactions_view(con, parquet)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE cancel_events AS
        WITH cancels AS (
            SELECT msno,
                   transaction_date                              AS cancel_date,
                   bool_or(is_auto_renew)                        AS auto_renew,
                   max(nullif(membership_expire_date, DATE '{EPOCH}')) AS expiry
            FROM tx WHERE is_cancel
            GROUP BY msno, transaction_date
        ),
        purchases AS (
            SELECT msno, transaction_date AS purchase_date FROM tx WHERE NOT is_cancel
        ),
        next_any AS (
            SELECT c.msno, c.cancel_date, c.auto_renew, c.expiry,
                   date_diff('day', c.cancel_date, p.purchase_date) AS gap_any
            FROM cancels c ASOF LEFT JOIN purchases p
              ON c.msno = p.msno AND p.purchase_date > c.cancel_date
        ),
        next_real AS (
            SELECT c.msno, c.cancel_date,
                   p.purchase_date                                  AS recovery_date,
                   date_diff('day', c.cancel_date, p.purchase_date) AS gap_recovery
            FROM cancels c ASOF LEFT JOIN purchases p
              ON c.msno = p.msno
             AND p.purchase_date > c.cancel_date + INTERVAL {grace_days} DAY
        )
        SELECT a.msno, a.cancel_date, a.auto_renew, a.expiry, a.gap_any,
               r.recovery_date, r.gap_recovery
        FROM next_any a LEFT JOIN next_real r USING (msno, cancel_date)
        """
    )


SEGMENTS = {"all": "TRUE", "auto_renew": "auto_renew", "manual": "NOT auto_renew"}


def recovery_rates(
    con: duckdb.DuckDBPyConnection, horizon: date, windows: tuple[int, ...] = WINDOWS
) -> list[RecoveryRate]:
    """Recovery rate per window per segment, over events observed long enough to count."""
    rates: list[RecoveryRate] = []
    for window in windows:
        eligible = f"cancel_date + INTERVAL {window} DAY <= DATE '{horizon}'"
        recovered = f"gap_recovery IS NOT NULL AND gap_recovery <= {window}"
        uncovered = "(expiry IS NULL OR recovery_date > expiry)"
        for segment, predicate in SEGMENTS.items():
            row = con.execute(
                f"""
                SELECT count(*) FILTER (WHERE {eligible}),
                       count(*) FILTER (WHERE NOT {eligible}),
                       count(*) FILTER (WHERE {eligible} AND {recovered}),
                       count(*) FILTER (WHERE {eligible} AND {recovered} AND {uncovered})
                FROM cancel_events WHERE {predicate}
                """
            ).fetchone()
            assert row is not None
            rates.append(
                RecoveryRate(
                    window_days=window,
                    segment=segment,
                    eligible=row[0],
                    censored=row[1],
                    recovered=row[2],
                    recovered_uncovered=row[3],
                )
            )
    return rates


def analyse(
    parquet: Path | None = None,
    also: dict[str, Path] | None = None,
    grace_days: int = GRACE_DAYS,
    windows: tuple[int, ...] = WINDOWS,
) -> CancelReport:
    """Run the whole of T1.2 against `transactions.parquet` (or a small fixture)."""
    parquet = parquet or (interim_dir() / "transactions.parquet")
    con = duckdb.connect()
    try:
        horizon = horizon_of(con, parquet)
        build_cancel_events(con, parquet, grace_days)

        totals = con.execute(
            f"""
            SELECT (SELECT count(*) FROM tx),
                   (SELECT count(*) FROM tx WHERE is_cancel),
                   (SELECT count(*) FROM cancel_events),
                   (SELECT count(DISTINCT msno) FROM cancel_events),
                   (SELECT count(*) FROM cancel_events
                     WHERE gap_any IS NOT NULL AND gap_any <= {grace_days}),
                   (SELECT count(*) FROM cancel_events WHERE gap_any IS NULL)
            """
        ).fetchone()
        assert totals is not None

        sentinels = [sentinel_report(con, parquet, "transactions")]
        for name, path in (also or {}).items():
            sentinels.append(sentinel_report(con, path, name))

        return CancelReport(
            total_rows=totals[0],
            cancel_rows=totals[1],
            cancel_events=totals[2],
            cancel_subscribers=totals[3],
            plan_change_events=totals[4],
            never_returned=totals[5],
            horizon=horizon,
            rates=recovery_rates(con, horizon, windows),
            sentinels=sentinels,
        )
    finally:
        con.close()


class LapseRate(BaseModel):
    """Recovery after coverage ran out, within one window."""

    window_days: int
    eligible: int
    censored: int
    recovered: int

    @property
    def rate(self) -> float:
        return self.recovered / self.eligible if self.eligible else 0.0


class LapseReport(BaseModel):
    """The `q` side: subscribers whose coverage simply ended, with no cancel on record."""

    horizon: date
    tolerance_days: int
    coverage_gaps: int  # every gap, however it was caused
    active_gaps: int  # a cancel was recorded on the last transaction day before the gap
    passive_lapses: int  # no cancel -- coverage just ran out
    lapsed_subscribers: int
    rates: list[LapseRate]

    def rate_for(self, window_days: int) -> LapseRate:
        return next(r for r in self.rates if r.window_days == window_days)


def build_lapse_events(
    con: duckdb.DuckDBPyConnection,
    parquet: Path,
    horizon: date,
    tolerance_days: int = RENEWAL_TOLERANCE_DAYS,
) -> None:
    """Reconstruct every subscriber's coverage timeline and find the holes in it.

    `membership_expire_date` restates the membership end after each transaction, so the
    most recent transaction's value is the current coverage end -- not the maximum ever
    seen, because a cancellation legitimately *shortens* coverage and a running maximum
    would refuse to notice. `IGNORE NULLS` carries the last real expiry forward past the
    1970-01-01 rows, which state nothing rather than stating an end in 1970.

    A gap is a day where the next transaction lands more than `tolerance_days` after
    coverage ended, or never lands at all. Coverage still running at the horizon is not
    a gap: those subscribers are alive, not lost, and counting them as lapsed would be
    the same censoring mistake in a different costume.
    """
    _transactions_view(con, parquet)
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE lapse_events AS
        WITH days AS (
            SELECT msno,
                   transaction_date       AS day,
                   bool_or(is_cancel)     AS had_cancel,
                   max(nullif(membership_expire_date, DATE '{EPOCH}')) AS expiry
            FROM tx GROUP BY msno, transaction_date
        ),
        timeline AS (
            SELECT msno, day, had_cancel,
                   last_value(expiry IGNORE NULLS) OVER (
                       PARTITION BY msno ORDER BY day
                       ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS coverage_until,
                   lead(day) OVER (PARTITION BY msno ORDER BY day) AS next_day
            FROM days
        )
        SELECT msno, day, had_cancel, coverage_until, next_day,
               date_diff('day', coverage_until, next_day) AS gap
        FROM timeline
        WHERE coverage_until IS NOT NULL
          AND coverage_until <= DATE '{horizon}'
          AND (next_day IS NULL
               OR next_day > coverage_until + INTERVAL {tolerance_days} DAY)
        """
    )


def lapse_rates(
    con: duckdb.DuckDBPyConnection, horizon: date, windows: tuple[int, ...] = WINDOWS
) -> list[LapseRate]:
    """Recovery after a passive lapse. Cancel-caused gaps are excluded -- they are the
    other death, measured by `recovery_rates`."""
    rates: list[LapseRate] = []
    for window in windows:
        row = con.execute(
            f"""
            SELECT count(*) FILTER (WHERE eligible),
                   count(*) FILTER (WHERE NOT eligible),
                   count(*) FILTER (WHERE eligible AND gap IS NOT NULL AND gap <= {window})
            FROM (
                SELECT gap,
                       coverage_until + INTERVAL {window} DAY <= DATE '{horizon}' AS eligible
                FROM lapse_events WHERE NOT had_cancel
            )
            """
        ).fetchone()
        assert row is not None
        rates.append(
            LapseRate(window_days=window, eligible=row[0], censored=row[1], recovered=row[2])
        )
    return rates


def analyse_lapses(
    parquet: Path | None = None,
    tolerance_days: int = RENEWAL_TOLERANCE_DAYS,
    windows: tuple[int, ...] = WINDOWS,
) -> LapseReport:
    """Run the `q` half of T1.2: recovery after coverage ended without a cancellation."""
    parquet = parquet or (interim_dir() / "transactions.parquet")
    con = duckdb.connect()
    try:
        horizon = horizon_of(con, parquet)
        build_lapse_events(con, parquet, horizon, tolerance_days)
        totals = con.execute(
            """
            SELECT count(*),
                   count(*) FILTER (WHERE had_cancel),
                   count(*) FILTER (WHERE NOT had_cancel),
                   count(DISTINCT msno) FILTER (WHERE NOT had_cancel)
            FROM lapse_events
            """
        ).fetchone()
        assert totals is not None
        return LapseReport(
            horizon=horizon,
            tolerance_days=tolerance_days,
            coverage_gaps=totals[0],
            active_gaps=totals[1],
            passive_lapses=totals[2],
            lapsed_subscribers=totals[3],
            rates=lapse_rates(con, horizon, windows),
        )
    finally:
        con.close()


def format_lapse_report(report: LapseReport) -> str:
    lines = [
        f"Horizon **{report.horizon}**, late-renewal tolerance **{report.tolerance_days} days**.",
        "",
        "| quantity | value |",
        "|---|---:|",
        f"| coverage gaps (all causes) | {report.coverage_gaps:,} |",
        f"| gaps preceded by a cancellation (active death) | {report.active_gaps:,} |",
        f"| passive lapses -- coverage just ran out | {report.passive_lapses:,} |",
        f"| distinct subscribers who passively lapsed | {report.lapsed_subscribers:,} |",
        "",
        "| window | eligible | censored | recovered | rate (`q`) |",
        "|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {r.window_days}d | {r.eligible:,} | {r.censored:,} | {r.recovered:,} "
        f"| **{r.rate:.3f}** |"
        for r in report.rates
    ]
    return "\n".join(lines)


def format_report(report: CancelReport) -> str:
    """Markdown, because these numbers are due in docs/mapping.md, not on a terminal."""
    lines = [
        f"Horizon (latest `transaction_date`): **{report.horizon}**",
        "",
        "| quantity | value |",
        "|---|---:|",
        f"| transaction rows | {report.total_rows:,} |",
        f"| `is_cancel = 1` rows | {report.cancel_rows:,} |",
        f"| cancel events (msno x day) | {report.cancel_events:,} |",
        f"| subscribers who cancelled at least once | {report.cancel_subscribers:,} |",
        f"| repurchase within {GRACE_DAYS}d -- plan change, not recovery "
        f"| {report.plan_change_events:,} ({report.plan_change_share:.1%}) |",
        f"| cancel events with no later purchase at all | {report.never_returned:,} |",
        "",
        "| window | segment | eligible | censored | recovered | rate | after coverage ended |",
        "|---:|---|---:|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {r.window_days}d | {r.segment} | {r.eligible:,} | {r.censored:,} "
        f"| {r.recovered:,} | **{r.rate:.3f}** | {r.recovered_uncovered:,} "
        f"({r.uncovered_rate:.3f}) |"
        for r in report.rates
    ]
    lines += [
        "",
        "| table | rows | epoch expiry | far-future expiry | latest expiry |",
        "|---|---:|---:|---:|---|",
    ]
    lines += [
        f"| `{s.table}` | {s.rows:,} | {s.epoch_rows:,} ({s.epoch_share:.3%}) "
        f"| {s.far_future_rows:,} ({s.far_future_share:.3%}) | {s.far_future_max} |"
        for s in report.sentinels
    ]
    return "\n".join(lines)
