"""T2.1a -- roll each live mandate's features forward, and score the hazard on them.

The harness needs `h[i,t]`: the probability mandate `i` dies in week `t` of the horizon
if nobody touches it. That number has to come from the *same* model T1.7 fitted and the
*same* features it was fitted on, or the ladder is comparing policies against a hazard
nobody validated.

So this module does not invent a forward hazard. It projects each mandate's feature
vector forward week by week -- the billing clock ticks, coverage renews, debits
accumulate -- and scores the projection with the fitted model's own SQL expression. The
same `risk.hazard.FittedHazard.expression` that produced `docs/eval.md` §2 produces every
number the harness consumes.

Who is in the book
------------------
**Only mandates that were still alive at the snapshot.** A retention system cannot be run
on a mandate that already died, and including dead ones would let every arm claim credit
for saving them. In frame terms: spells that were *censored*, not spells that ended in an
event.

Why the projection is deterministic
-----------------------------------
Given "no intervention and no death", every feature the model uses evolves by arithmetic:
`week_index` increments, `days_to_coverage_end` counts down and resets on renewal,
`debits_so_far` and `paid_so_far_inr` step up on each renewal. Nothing is sampled, so the
whole horizon can be precomputed in one query and the harness becomes a small loop over a
small table.

The one thing this conditions on is survival. `h[i,t]` is the hazard *given the mandate
reached week t alive*, which is exactly the conditional a discrete-time hazard is, and
exactly what the harness needs -- it carries the survival weight itself.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
from pydantic import BaseModel

from mandateguard.risk.hazard import FittedHazard


class WeekHazard(BaseModel):
    """One mandate's projected hazard in one week of the horizon."""

    mandate_id: str
    week: int
    hazard: float
    days_to_coverage_end: int
    renewals: int
    """How many billing cycles the projection has it pay by the start of this week. It is
    in the report because it is the cheapest sanity check available: a 30-day mandate over
    a 12-week horizon should renew about three times, and a projection that says zero has
    a broken clock."""


def build(
    con: duckdb.DuckDBPyConnection,
    model: FittedHazard,
    frame_path: Path,
    book_path: Path,
    weeks: int,
) -> None:
    """Leave a `forecast` table on `con`: one row per (live mandate, week in horizon).

    Two inputs rather than one. The person-period frame carries the model's features and
    the mandate's clock; `mandates.parquet` carries the rupee columns (`L`, `R`, `q`, `r`)
    that the value layer needs and the frame deliberately does not duplicate.
    """
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE alive AS
        WITH ranked AS (
            SELECT *,
                   row_number() OVER (PARTITION BY mandate_id ORDER BY week_index DESC) AS rn,
                   bool_or(event) OVER (PARTITION BY mandate_id)                        AS died
            FROM '{frame_path.as_posix()}'
        )
        SELECT * EXCLUDE (rn, died, event, death_kind) FROM ranked WHERE rn = 1 AND NOT died
        """
    )

    # The coverage end as a real date, and the date of the last transaction. Everything
    # the projection does is arithmetic on these two anchors.
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE anchor AS
        SELECT a.* EXCLUDE (week_start),
               a.week_start                                                    AS last_week,
               a.week_start + INTERVAL 1 DAY * a.days_to_coverage_end          AS coverage_end,
               a.week_start - INTERVAL 1 DAY * a.days_since_last_txn           AS last_txn
        FROM alive a
        """
    )

    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE projected AS
        SELECT
            mandate_id,
            step                                                       AS week,
            week_index + step                                          AS week_index,
            last_week + INTERVAL 1 DAY * (7 * step)                    AS week_start,
            renewals,
            date_diff('day', last_week + INTERVAL 1 DAY * (7 * step),
                      coverage_end + INTERVAL 1 DAY
                                     * (debit_frequency_days * renewals))::INTEGER
                                                                       AS days_to_coverage_end,
            -- After a renewal the clock restarts at the renewal date, which is the
            -- coverage end it replaced; before the first one it is the real last
            -- transaction. Getting this wrong would make every mandate look like it had
            -- been silent for months by week 12.
            date_diff('day',
                      CASE WHEN renewals = 0 THEN last_txn
                           ELSE coverage_end + INTERVAL 1 DAY
                                * (debit_frequency_days * (renewals - 1)) END,
                      last_week + INTERVAL 1 DAY * (7 * step))::INTEGER
                                                                       AS days_since_last_txn,
            debits_so_far + renewals                                   AS debits_so_far,
            paid_so_far_inr + renewals * amount_inr                    AS paid_so_far_inr,
            account_age_days + 7 * step                                AS account_age_days,
            cancels_so_far, amount_inr, debit_frequency_days, frequency_imputed,
            auto_renew, discount_inr, method, city, registered_via, gender,
            age_years, member_record_found, left_truncated
        FROM (
            SELECT *, step,
                   greatest(0, ceil(date_diff('day', coverage_end,
                                              last_week + INTERVAL 1 DAY * (7 * step))
                                    / debit_frequency_days::DOUBLE))::INTEGER AS renewals
            FROM anchor, unnest(range(1, {weeks} + 1)) AS g(step)
        )
        """
    )

    # The hazard is scored on `projected` alone, *before* the book is joined in. The
    # model's expression names bare columns (`amount_inr` and friends) and the book
    # carries columns of the same name, so scoring after the join is ambiguous -- and an
    # ambiguity that resolved silently would score the model on the snapshot's amount
    # instead of the projected one.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE forecast AS
        SELECT p.mandate_id, p.week - 1 AS week, p.hazard,
               p.days_to_coverage_end, p.renewals,
               b.amount_inr AS debit_inr, b.ltv_remaining_inr, b.reachability_value_inr,
               b.recovery_after_lapse, b.recovery_after_revocation, b.method AS rail
        FROM (SELECT *, ({model.expression}) AS hazard FROM projected) p
        JOIN '{book_path.as_posix()}' b ON b.mandate_id = p.mandate_id
        ORDER BY p.mandate_id, p.week
        """
    )


def summary(con: duckdb.DuckDBPyConnection) -> list[WeekHazard]:
    """One representative row per week -- the median-hazard mandate. For the report."""
    rows = con.execute(
        """
        SELECT week, median(hazard), median(days_to_coverage_end), median(renewals),
               count(DISTINCT mandate_id)
        FROM forecast GROUP BY 1 ORDER BY 1
        """
    ).fetchall()
    return [
        WeekHazard(
            mandate_id=f"(median of {int(n):,})",
            week=int(w),
            hazard=float(h),
            days_to_coverage_end=int(d),
            renewals=int(r),
        )
        for w, h, d, r, n in rows
    ]


def format_summary(weeks: list[WeekHazard]) -> str:
    lines = [
        "| week | median hazard | median days to coverage end | median renewals paid |",
        "|---:|---:|---:|---:|",
    ]
    lines += [
        f"| {w.week} | {w.hazard:.5f} | {w.days_to_coverage_end} | {w.renewals} |" for w in weeks
    ]
    return "\n".join(lines)
