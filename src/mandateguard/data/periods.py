"""T1.4 -- expand the mandate book into one row per week alive.

A discrete-time survival model does not consume mandates, it consumes *person-periods*:
one row per subscriber per week they were still alive, carrying what was known at the
start of that week, and `event = 1` on the week they died. Fitting a logistic regression
on that frame gives a per-week hazard directly -- which is exactly the quantity the
allocator needs, because the allocator's question is "if I do nothing, what is the
chance this mandate dies before my next budget arrives?"

Why discrete-time and not Cox
-----------------------------
A Cox model returns a hazard *ratio* and leaves the baseline unspecified. The allocator
needs an absolute probability in [0,1] to multiply against rupees, so the baseline is
the part that matters most and Cox is the model that refuses to give it. The
person-period trick turns survival into ordinary binary classification, which also means
calibration (T1.8) is measurable with tools that already exist. `week_index` is a
covariate like any other, so the baseline hazard is estimated rather than assumed.

The one rule this module exists to enforce
------------------------------------------
**Features may only use what was known at the start of the week. Labels may use the
future.** That asymmetry is the whole discipline. Every feature here comes from the last
transaction at or before `week_start`, so a row in March 2015 cannot see a transaction in
April 2015. The mandate book's own columns are mostly *snapshot* facts
-- `amount_inr` and `debit_frequency_days` describe 2017-02-28 -- so this module
deliberately recomputes them as-of each week instead of copying them down. Only the
genuinely time-invariant ones (the assigned rail, demographics) are carried across.

Four decisions, each argued in `docs/mapping.md` 5:

1. **One spell per subscriber, ending at their first death.** A subscriber who lapsed,
   recovered, and lapsed again contributes only the first spell. The cost is real -- it
   discards about a quarter of the death events -- and the reason is that a returning
   customer is a different population from a first-time one, so pooling the two would
   estimate a hazard for neither.

2. **The week clock starts at the subscriber's first observed transaction**, so
   `week_index` is duration since origination, which is the covariate a survival model
   is built around. Subscribers whose mandate predates the log are flagged
   `left_truncated` rather than dropped.

3. **A death is the first confirmed coverage gap**, using T1.2's definitions and its
   7-day renewal tolerance, so `q` and the frame's labels cannot disagree. A gap too
   close to the horizon to confirm is censored, not counted as a death.

4. **Censored spells contribute only whole weeks; a dying spell keeps its partial last
   week.** A censored subscriber observed for three days of a week was not at risk for
   the week, and counting that as a survived week biases the hazard down. A subscriber
   who died three days into a week did die in it.

Reads `transactions.parquet` and `members.parquet`; writes `person_periods.parquet` to
`data/processed/`, which is gitignored -- a derived frame committed next to the sample
would go stale the first time this file changed and nobody would notice.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import duckdb
from pydantic import BaseModel

from mandateguard.data.cancel import EPOCH, RENEWAL_TOLERANCE_DAYS, build_lapse_events
from mandateguard.data.mandates import (
    MAX_PLAUSIBLE_CYCLE_DAYS,
    SAME_DAY_TIE_BREAK,
    FilterStep,
    build_book,
)
from mandateguard.data.paths import ensure, interim_dir, processed_dir, spill_dir
from mandateguard.models import DeathKind
from mandateguard.policy.loader import Params, load_params

HAZARD_BUCKETS: tuple[tuple[str, int, int], ...] = (
    ("weeks 0-3", 0, 3),
    ("weeks 4-7", 4, 7),
    ("weeks 8-12", 8, 12),
    ("weeks 13-25", 13, 25),
    ("weeks 26-51", 26, 51),
    ("weeks 52+", 52, 10_000),
)
"""Buckets for the empirical baseline hazard in the report.

Not a modelling choice -- T1.7 fits `week_index` itself. This is a sanity readout: if
the hazard is flat across all six buckets then duration carries no signal and the
survival framing has bought nothing, which is something to find out here rather than
after fitting."""


class HazardBucket(BaseModel):
    """Empirical per-week death rate over a range of `week_index`."""

    label: str
    person_weeks: int
    events: int

    @property
    def hazard(self) -> float:
        return self.events / self.person_weeks if self.person_weeks else 0.0

    @property
    def line(self) -> str:
        return f"| {self.label} | {self.person_weeks:,} | {self.events:,} | {self.hazard:.4f} |"


class PeriodReport(BaseModel):
    """Everything T1.4 has to answer about the frame it just wrote."""

    snapshot: date
    tolerance_days: int
    steps: list[FilterStep]
    spells: int
    person_weeks: int
    events: int
    events_by_kind: dict[str, int]
    censored_spells: int
    unconfirmed_deaths: int
    left_truncated: int
    median_weeks: float
    max_weeks: int
    hazard: list[HazardBucket]
    megabytes: float

    @property
    def event_rate(self) -> float:
        """Per-week death rate over the whole frame -- the intercept a model has to beat."""
        return self.events / self.person_weeks if self.person_weeks else 0.0

    @property
    def died_share(self) -> float:
        return self.events / self.spells if self.spells else 0.0


def build(
    params: Params | None = None,
    interim: Path | None = None,
    out_dir: Path | None = None,
    tolerance_days: int = RENEWAL_TOLERANCE_DAYS,
    write: bool = True,
) -> PeriodReport:
    """Expand the mandate book into person-weeks and report the frame's shape."""
    params = params or load_params()
    interim = interim or interim_dir()
    out_dir = out_dir or processed_dir()
    snapshot = params.india.snapshot_date

    transactions = interim / "transactions.parquet"
    members = (interim / "members.parquet").as_posix()

    con = duckdb.connect()
    try:
        # 46M person-weeks do not fit in this laptop's RAM while they sort. Without a
        # spill directory an in-memory DuckDB has nowhere to put the overflow and the
        # process dies with no file and no message -- see `paths.spill_dir`.
        con.execute(f"SET temp_directory = '{ensure(spill_dir()).as_posix()}'")
        # The frame's order is fixed by the ORDER BY in the COPY below, so DuckDB is free
        # to stop preserving it everywhere else. On the full run this is the difference
        # between spilling a few GB and spilling tens of them.
        con.execute("SET preserve_insertion_order = false")

        # Order matters. `build_lapse_events` creates its own unfiltered `tx` view, and
        # `build_book` then replaces it with the snapshot-bounded one. Labels are allowed
        # to see the whole table -- an outcome may use the future -- but every feature
        # below reads the snapshot-bounded view, so the ordering is what keeps that true.
        #
        # The asymmetry is smaller than it sounds, and the `confirmed` guard is why. A
        # post-snapshot transaction can only change a label by closing a gap it can
        # reach, and a gap it can reach is by definition within `tolerance_days` of the
        # snapshot -- which is exactly the gap `confirmed` refuses to call a death. So
        # the two readings agree on every event; they differ only in where a *censored*
        # spell is cut, by less than the tolerance. In the shipped config they do not
        # differ at all: `india.snapshot_date` is the transaction log's own last day.
        build_lapse_events(con, transactions, snapshot, tolerance_days)
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE first_gap AS
            -- `bool_or` rather than `arg_min(had_cancel, coverage_until)`: two days can
            -- share a coverage end (a cancel that backdates expiry does exactly that),
            -- and `arg_min` picks between tied rows arbitrarily -- the same coin flip
            -- `mandates.SAME_DAY_TIE_BREAK` exists to remove. It is also the better
            -- reading: if any transaction ending coverage on that day was a cancel,
            -- the mandate was revoked rather than left to expire.
            SELECT msno, gap_at,
                   bool_or(had_cancel)                       AS revoked,
                   gap_at + INTERVAL {tolerance_days} DAY
                       <= DATE '{snapshot}'                  AS confirmed
            FROM (SELECT *, min(coverage_until) OVER (PARTITION BY msno) AS gap_at
                  FROM lapse_events)
            WHERE coverage_until = gap_at
            GROUP BY msno, gap_at
            """
        )

        steps = build_book(con, params, interim)

        # Per-day state, then a running total over days. Collapsing to one row per day
        # first is what makes the week assignment below deterministic: a subscriber with two
        # transactions on one day has one state at the end of that day, and the row that
        # granted more coverage is the one that took effect -- the same tie-break
        # `mandates.latest` uses, for the same reason.
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE running AS
            WITH day_agg AS (
                SELECT msno, transaction_date AS day,
                       -- A cancel is a transaction but it is not a debit, and calling
                       -- it one would inflate `debits_so_far` for exactly the
                       -- subscribers whose history matters most.
                       count(*) FILTER (WHERE NOT is_cancel)       AS debits,
                       sum(actual_amount_paid)                     AS paid,
                       count(*) FILTER (WHERE is_cancel)           AS cancels
                FROM tx GROUP BY 1, 2
            ),
            day_state AS (
                SELECT * EXCLUDE (rn) FROM (
                    SELECT msno, transaction_date AS day,
                           actual_amount_paid, plan_list_price, payment_plan_days,
                           is_auto_renew, membership_expire_date,
                           row_number() OVER (
                               PARTITION BY msno, transaction_date
                               ORDER BY {SAME_DAY_TIE_BREAK}
                           ) AS rn
                    FROM tx
                ) WHERE rn = 1
            )
            SELECT s.msno, s.day,
                   s.actual_amount_paid, s.plan_list_price, s.payment_plan_days,
                   s.is_auto_renew,
                   last_value(nullif(s.membership_expire_date, DATE '{EPOCH}') IGNORE NULLS)
                       OVER w                                      AS coverage_until,
                   -- The as-of halves of T1.3's amount and cycle fallback chains (3.5).
                   -- `mandates.py` falls back to the subscriber's median payment and
                   -- modal cycle over their *whole* history, which at week 4 would be
                   -- reading week 40's plan. The last non-zero value seen so far is the
                   -- same idea with the future removed.
                   last_value(nullif(s.actual_amount_paid, 0) IGNORE NULLS)
                       OVER w                                      AS last_paid,
                   last_value(nullif(s.payment_plan_days, 0) IGNORE NULLS)
                       OVER w                                      AS last_cycle,
                   sum(a.debits)  OVER w                           AS debits_so_far,
                   sum(a.paid)    OVER w                           AS paid_so_far,
                   sum(a.cancels) OVER w                           AS cancels_so_far,
                   lead(s.day) OVER (PARTITION BY s.msno ORDER BY s.day)
                                                                   AS next_day
            FROM day_state s JOIN day_agg a USING (msno, day)
            WINDOW w AS (PARTITION BY s.msno ORDER BY s.day
                         ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
            """
        )

        # `data_start` is the log's own first day, which is what makes left-truncation
        # decidable: if a subscriber's first transaction is at least one billing cycle
        # after the log opens, any earlier cycle would have been inside the window and
        # we would have seen it. Its absence is then evidence of origination rather than
        # of a short log. Closer than that and we cannot tell, so the row is flagged.
        data_start = _scalar(con, "SELECT min(transaction_date) FROM tx")
        con.execute(
            f"""
            CREATE OR REPLACE TEMP TABLE spells AS
            SELECT b.mandate_id, b.first_seen AS start_date,
                   b.method, b.city, b.registered_via, b.gender, b.age_years,
                   b.member_record_found, m.registration_init_time,
                   b.first_seen < DATE '{data_start}' + INTERVAL 1 DAY
                                * b.debit_frequency_days       AS left_truncated,
                   g.gap_at IS NOT NULL AND g.confirmed        AS died,
                   CASE WHEN g.gap_at IS NOT NULL AND g.confirmed
                        THEN CASE WHEN g.revoked THEN '{DeathKind.REVOCATION.value}'
                                  ELSE '{DeathKind.LAPSE.value}' END END AS death_kind,
                   -- A dying spell keeps its partial final week; a censored one is cut
                   -- back to whole weeks, because a subscriber observed for three days
                   -- of a week was not at risk for that week.
                   CASE WHEN g.gap_at IS NOT NULL AND g.confirmed
                        THEN date_diff('day', b.first_seen, g.gap_at) // 7 + 1
                        ELSE date_diff('day', b.first_seen,
                                       coalesce(g.gap_at, DATE '{snapshot}')) // 7
                   END                                         AS weeks
            FROM book b
            LEFT JOIN first_gap g ON g.msno = b.mandate_id
            LEFT JOIN '{members}' m ON m.msno = b.mandate_id
            """
        )
        unconfirmed = _count(
            con,
            "SELECT count(*) FROM first_gap g SEMI JOIN book b ON b.mandate_id = g.msno "
            "WHERE NOT g.confirmed",
        )
        steps.append(
            FilterStep(
                step="with at least one observable week",
                why="a spell ending on or before its first transaction has no week to expand",
                subscribers=_count(con, "SELECT count(*) FROM spells WHERE weeks >= 1"),
            )
        )
        con.execute("CREATE OR REPLACE TEMP TABLE spells AS SELECT * FROM spells WHERE weeks >= 1")

        # One row per week alive, expanded from the *transaction* side rather than the
        # week side. Both give the same frame; only one of them finishes.
        #
        # The obvious shape is to generate 46M week rows and ASOF-join each to the last
        # transaction at or before its `week_start`. That is the definition, and it ran
        # for over twenty minutes on the full book without producing a file -- an ASOF
        # join over 46M probes and 1.4M partitions is not a cheap operation.
        #
        # The cheap shape inverts it. A transaction's state holds from its own day until
        # the next transaction, so each row of `running` already *owns* a contiguous run
        # of weeks: every week whose `week_start` falls in `[day, next_day)`. Computing
        # that run is arithmetic on two dates, and the expansion is then an ordinary hash
        # join of 15M rows against 1.4M spells followed by an `unnest`.
        #
        # The runs are contiguous and disjoint by construction -- one row's `hi` is the
        # next row's `lo` minus one -- so every week of every spell appears exactly once,
        # which the tests check directly. `lo > hi` drops transactions that govern no
        # week start at all: the ones landing after the spell's last `week_start`,
        # including everything after a subscriber died and came back.
        #
        # The leakage barrier survives the inversion. A week is assigned the transaction
        # whose interval contains its `week_start`, and that transaction happened at or
        # before it -- which is the same guarantee `r.day <= w.week_start` gave.
        con.execute(
            """
            CREATE OR REPLACE TEMP VIEW weeks AS
            SELECT * EXCLUDE (lo, hi),
                   start_date + INTERVAL 1 DAY * (7 * week_index) AS week_start
            FROM (
                SELECT *, unnest(range(lo, hi + 1)) AS week_index
                FROM (
                    SELECT s.* EXCLUDE (weeks), s.weeks AS spell_weeks,
                           r.* EXCLUDE (msno),
                           greatest(0, (date_diff('day', s.start_date, r.day) + 6) // 7)
                                                                          AS lo,
                           least(s.weeks - 1,
                                 coalesce((date_diff('day', s.start_date, r.next_day) + 6)
                                          // 7 - 1, s.weeks - 1))          AS hi
                    FROM spells s JOIN running r ON r.msno = s.mandate_id
                ) WHERE lo <= hi
            )
            """
        )

        con.execute(
            f"""
            CREATE OR REPLACE TEMP VIEW frame AS
            SELECT
                w.mandate_id,
                w.week_index::INTEGER                                AS week_index,
                w.week_start::DATE                                   AS week_start,
                (w.died AND w.week_index = w.spell_weeks - 1)        AS event,
                CASE WHEN w.died AND w.week_index = w.spell_weeks - 1
                     THEN w.death_kind END                           AS death_kind,
                date_diff('day', w.start_date, w.week_start)::INTEGER AS tenure_days,
                date_diff('day', w.week_start, w.coverage_until)::INTEGER
                                                                     AS days_to_coverage_end,
                date_diff('day', w.day, w.week_start)::INTEGER       AS days_since_last_txn,
                round(coalesce(nullif(w.actual_amount_paid, 0), nullif(w.plan_list_price, 0),
                               w.last_paid, 0)
                      * {params.india.ntd_to_inr}, 2)                AS amount_inr,
                coalesce(nullif(w.payment_plan_days, 0), w.last_cycle,
                         CASE WHEN date_diff('day', w.day, w.coverage_until)
                                   BETWEEN 1 AND {MAX_PLAUSIBLE_CYCLE_DAYS}
                              THEN date_diff('day', w.day, w.coverage_until) END,
                         {params.india.default_debit_frequency_days})::INTEGER
                                                                     AS debit_frequency_days,
                coalesce(nullif(w.payment_plan_days, 0), w.last_cycle) IS NULL
                                                                     AS frequency_imputed,
                w.is_auto_renew                                      AS auto_renew,
                round((w.plan_list_price - w.actual_amount_paid)
                      * {params.india.ntd_to_inr}, 2)                AS discount_inr,
                w.debits_so_far::INTEGER                             AS debits_so_far,
                w.cancels_so_far::INTEGER                            AS cancels_so_far,
                round(w.paid_so_far * {params.india.ntd_to_inr}, 2)  AS paid_so_far_inr,
                w.method, w.city, w.registered_via, w.gender, w.age_years,
                w.member_record_found,
                date_diff('day', w.registration_init_time, w.week_start)::INTEGER
                                                                     AS account_age_days,
                w.left_truncated
            FROM weeks w
            """
        )

        out_path = out_dir / "person_periods.parquet"
        if write:
            ensure(out_dir)
            con.execute(
                f"COPY (SELECT * FROM frame ORDER BY mandate_id, week_index) "
                f"TO '{out_path.as_posix()}' (FORMAT PARQUET, COMPRESSION ZSTD)"
            )

        # Every number below is measured on the file that was written, so the report
        # cannot describe something the parquet does not contain. On a dry run there is
        # no file, and the same queries fall back to the view that would have produced
        # it -- which is slow on the full book, and is why `--dry-run` is a sample tool.
        con.execute(
            "CREATE OR REPLACE TEMP VIEW written AS SELECT * FROM "
            + (f"'{out_path.as_posix()}'" if write else "frame")
        )

        totals = con.execute(
            "SELECT count(*), count(*) FILTER (WHERE event) FROM written"
        ).fetchone()
        assert totals is not None
        spans = con.execute(
            "SELECT count(*), median(weeks), max(weeks), "
            "count(*) FILTER (WHERE NOT died), count(*) FILTER (WHERE left_truncated) "
            "FROM spells"
        ).fetchone()
        assert spans is not None

        return PeriodReport(
            snapshot=snapshot,
            tolerance_days=tolerance_days,
            steps=steps,
            spells=int(spans[0]),
            person_weeks=int(totals[0]),
            events=int(totals[1]),
            events_by_kind=_tally(con),
            censored_spells=int(spans[3]),
            unconfirmed_deaths=unconfirmed,
            left_truncated=int(spans[4]),
            median_weeks=float(spans[1] or 0.0),
            max_weeks=int(spans[2] or 0),
            hazard=_hazard(con),
            megabytes=round(out_path.stat().st_size / 1e6, 1) if write else 0.0,
        )
    finally:
        con.close()


def _hazard(con: duckdb.DuckDBPyConnection) -> list[HazardBucket]:
    """Empirical per-week death rate by duration bucket -- one pass, not one per bucket.

    Six separate `SELECT ... WHERE week_index BETWEEN` queries would each re-scan the
    frame, and on a dry run the frame is a view over the whole expansion, so six scans
    means building 46M rows six times.
    """
    parts = []
    for index, (_, low, high) in enumerate(HAZARD_BUCKETS):
        window = f"week_index BETWEEN {low} AND {high}"
        parts.append(f"count(*) FILTER (WHERE {window}) AS n{index}")
        parts.append(f"count(*) FILTER (WHERE {window} AND event) AS e{index}")
    row = con.execute(f"SELECT {', '.join(parts)} FROM written").fetchone()
    assert row is not None
    return [
        HazardBucket(label=label, person_weeks=int(row[2 * i]), events=int(row[2 * i + 1]))
        for i, (label, _, _) in enumerate(HAZARD_BUCKETS)
    ]


def _tally(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    rows = con.execute(
        "SELECT death_kind, count(*) FROM written WHERE event GROUP BY 1 ORDER BY 2 DESC, 1"
    ).fetchall()
    return {str(kind): int(n) for kind, n in rows}


def _scalar(con: duckdb.DuckDBPyConnection, sql: str):
    row = con.execute(sql).fetchone()
    assert row is not None
    return row[0]


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    return int(_scalar(con, sql))


def format_report(report: PeriodReport) -> str:
    """Markdown, because these numbers are due in docs/mapping.md, not on a terminal."""
    lines = [
        f"Snapshot **{report.snapshot}**, renewal tolerance **{report.tolerance_days} days**.",
        "",
        "| step | why | subscribers |",
        "|---|---|---:|",
    ]
    lines += [step.line for step in report.steps]
    lines += [
        "",
        f"**{report.person_weeks:,} person-weeks** over {report.spells:,} spells "
        f"({report.median_weeks:.0f} weeks median, {report.max_weeks:,} max), "
        f"{report.megabytes} MB.",
        "",
        "| quantity | value |",
        "|---|---:|",
        f"| spells ending in a death | {report.events:,} ({report.died_share:.1%}) |",
        f"| spells censored at the snapshot | {report.censored_spells:,} |",
        f"| gaps too close to the horizon to confirm -- censored, not counted as deaths "
        f"| {report.unconfirmed_deaths:,} |",
        f"| spells whose mandate predates the log (`left_truncated`) | {report.left_truncated:,} |",
        f"| per-week death rate (the intercept to beat) | {report.event_rate:.4f} |",
        "",
        "| death | events |",
        "|---|---:|",
    ]
    lines += [f"| `{kind}` | {n:,} |" for kind, n in report.events_by_kind.items()]
    lines += [
        "",
        "| duration | person-weeks | deaths | hazard |",
        "|---|---:|---:|---:|",
    ]
    lines += [bucket.line for bucket in report.hazard]
    return "\n".join(lines)
