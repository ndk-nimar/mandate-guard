"""T1.3 -- turn the KKBox transaction log into the mandate book this system reasons about.

KKBox is a Taiwanese music service in 2017. This system is about Indian recurring
mandates in 2026. Everything in this module is that bridge, and the bridge is made of
*decisions*, not measurements. Each one is argued in `docs/mapping.md` 3 and configured
in `config/params.yaml` under `india:` -- nothing is hard-coded here, so a reader can
change any of them and re-run without touching Python.

What is recovered from the data
-------------------------------
The billing facts: who is subscribed, on what cycle, for how much, until when, and
whether they cancelled. These come from the transaction log and nothing is invented.

What is assigned rather than recovered
--------------------------------------
The **rail** (UPI AutoPay / card / e-NACH / PPI). KKBox never published what
`payment_method_id` means -- 40 opaque integers with no legend -- so claiming "method 41
is UPI AutoPay" would be invention dressed as data. Instead the rail is assigned
deterministically from a hash of the customer id to match a configured mix. It is stable
across runs, it is reproducible, and it is flagged as synthetic everywhere it surfaces.
The alternative -- inventing a legend -- would have been worse and less honest.

Mandate validity (`expire_by`) and the reachability value `R` are likewise overlays:
KKBox has no column for either, and both are marked `swept: true`.

One row per subscriber, not per transaction
-------------------------------------------
A mandate is a standing authorisation, so the unit is the subscriber and the state is
whatever their most recent transaction says as of `india.snapshot_date`. The transaction
log is the history of that mandate, not a list of mandates.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from pathlib import Path

import duckdb
from pydantic import BaseModel

from mandateguard.data.paths import ensure, interim_dir, processed_dir
from mandateguard.models import MandateStatus, Rail
from mandateguard.policy.loader import IndiaParams, Params, load_params

HASH_BUCKETS = 100_000
"""Resolution of the deterministic rail assignment. 100k buckets keeps the realised mix
within ~0.1% of the configured one at this book size, without a real RNG -- an RNG would
need its state threaded through every caller to stay reproducible."""

RAIL_SALT = "rail"
"""Salt mixed into the id before hashing, so this hash is independent of every other
hash of the same key.

Learned the hard way. `sample.py` also selects subscribers by hashing `msno`, and with
both using a bare `hash(msno)` the two were perfectly correlated: the sample kept exactly
the subscribers in the lowest hash buckets, and those are precisely the buckets the rail
ladder assigns to its first rail. The CI sample came out 100% UPI AutoPay while the full
book came out at the configured mix, and nothing failed -- every per-rail number computed
in CI would have been silently degenerate.

Two hashes of one key are not two independent draws. Anything hashing `msno` for a new
purpose needs its own salt."""

MAX_PLAUSIBLE_CYCLE_DAYS = 400
"""An expiry span longer than this is not a billing cycle, so it is not usable to impute
one. A 410-day plan does exist in the data, which is why this is a bound on *imputation*
only: a stated `payment_plan_days` of 410 is still used as-is, and one mandate in the
built book has exactly that cycle."""

SAME_DAY_TIE_BREAK = (
    "membership_expire_date DESC NULLS LAST, is_cancel DESC, is_auto_renew DESC, "
    "actual_amount_paid DESC, plan_list_price DESC, payment_plan_days DESC, "
    "payment_method_id DESC"
)
"""How to choose between two transactions written on the same day. A **total** order.

A mandate's state is whatever its most recent transaction says, and 7 subscribers in the
5,000-subscriber CI sample -- about 0.14%, so roughly 2,000 of the full book -- have two
transactions on their last day. `ORDER BY membership_expire_date DESC` alone leaves those
tied, and a tied `row_number()` is resolved by whatever order DuckDB's parallel scan
happened to produce. The observable effect: the same input built `cancelled: 1,053` on
one run and `cancelled: 1,054` on the next, with nothing changed. T2.9 asks that a
stranger's fork regenerate `results.md` byte for byte, and a coin-flip in the book makes
that impossible -- quietly, because both runs look fine.

Every column the book later reads off this row is therefore in the sort, so two rows that
still tie are identical rows and the choice between them cannot matter.

The first tie-break carries a decision rather than just breaking a tie. Of two
same-day rows granting the same coverage, one a cancel and one not, `is_cancel DESC`
takes the cancel. Which one "really" happened is not recoverable from the data, so the
rule is chosen for its direction: treating an ambiguous customer as cancelled
under-states retention, and this system exists to claim retention. A rule that resolves
ambiguity in favour of its own headline is not a rule, it is a thumb on the scale."""


class FilterStep(BaseModel):
    """One narrowing of the subscriber population, with its survivors.

    `docs/mapping.md` 1 committed to this: "any join is therefore a filter, and the
    direction of that filter changes who the model is fit on. Whichever join T1.4 uses
    must be stated in this document with its surviving row count." This type is how that
    promise is kept -- the counts are produced by the code that does the filtering, so
    they cannot drift from it.
    """

    step: str
    why: str
    subscribers: int

    @property
    def line(self) -> str:
        return f"| {self.step} | {self.why} | {self.subscribers:,} |"


class MandateBookReport(BaseModel):
    snapshot: date
    steps: list[FilterStep]
    mandates: int
    by_status: dict[str, int]
    by_rail: dict[str, int]
    imputed_frequency: int
    age_known: int
    members_matched: int
    total_ltv_inr: float
    megabytes: float

    @property
    def retention(self) -> float:
        """Share of the starting population that survives into the book."""
        return self.mandates / self.steps[0].subscribers if self.steps else 0.0


def _rail_assignment_sql(india: IndiaParams) -> str:
    """A CASE expression assigning a rail from a stable hash of the customer id.

    UPI AutoPay is skipped for debits above the additional-factor-authentication
    threshold, and the remaining rails are renormalised over what is left, so the mix
    still sums to 1 for those mandates. At `ntd_to_inr: 1.0` the largest per-debit amount
    in the built book is 210 against a 15,000 threshold, so this branch currently applies
    to zero mandates -- it is kept because `ntd_to_inr` is a configured knob, and a rule
    that only holds at today's scale is a bug waiting for someone to change the scale.
    `tests/test_mandates.py` forces the branch to bind by lowering the threshold.
    """
    ordered = [(rail, share) for rail, share in india.rail_mix.items() if share > 0]

    def ladder(pairs: Sequence[tuple[Rail, float]]) -> str:
        # A one-rail mix is a legitimate config (a sweep, or a test), and `CASE ELSE x
        # END` is not valid SQL -- so a single rail is emitted as a bare literal.
        if len(pairs) == 1:
            return f"'{pairs[0][0].value}'"
        total = sum(share for _, share in pairs)
        arms, cumulative = [], 0.0
        for rail, share in pairs[:-1]:
            cumulative += share / total
            arms.append(f"WHEN bucket < {cumulative:.9f} THEN '{rail.value}'")
        arms.append(f"ELSE '{pairs[-1][0].value}'")
        return "CASE " + " ".join(arms) + " END"

    eligible = [(rail, share) for rail, share in ordered if rail is not Rail.UPI_AUTOPAY]
    if not eligible:
        raise ValueError(
            "india.rail_mix puts the whole book on UPI AutoPay, so a debit above "
            f"india.upi_autopay_afa_threshold_inr ({india.upi_autopay_afa_threshold_inr}) "
            "has no rail to fall back to. Either give the mix a second rail, or raise "
            "the threshold above the largest debit in the book."
        )
    return (
        f"CASE WHEN amount_inr > {india.upi_autopay_afa_threshold_inr} "
        f"THEN {ladder(eligible)} ELSE {ladder(ordered)} END"
    )


def build_book(
    con: duckdb.DuckDBPyConnection,
    params: Params | None = None,
    interim: Path | None = None,
) -> list[FilterStep]:
    """Leave a `book` temp table on `con`, and return the filter chain that produced it.

    Split out of `build()` so that T1.4's person-period expansion can stand on exactly
    this population rather than re-deriving it. Two definitions of "who is in the book"
    would be two populations the moment either one changed, and every downstream number
    would then be computed over a set nobody could name.

    The connection is the caller's on purpose: `periods.py` needs the `tx` view and the
    `book` table alive in the same session so it can join them without a round trip
    through parquet.
    """
    params = params or load_params()
    india = params.india
    interim = interim or interim_dir()

    transactions = (interim / "transactions.parquet").as_posix()
    members = (interim / "members.parquet").as_posix()
    snapshot = india.snapshot_date
    low_age, high_age = india.plausible_age_years
    horizon_days = params.horizon.weeks * 7

    con.execute(
        f"""
        CREATE OR REPLACE TEMP VIEW tx AS
        SELECT * FROM '{transactions}'
        WHERE transaction_date IS NOT NULL AND transaction_date <= DATE '{snapshot}'
        """
    )
    steps = [
        FilterStep(
            step="subscribers in `transactions`",
            why="every msno with at least one dated transaction at or before the snapshot",
            subscribers=_count(con, "SELECT count(DISTINCT msno) FROM tx"),
        )
    ]

    # Per-subscriber aggregates first: the modal cycle length and the typical paid
    # amount both need the subscriber's whole history, not just their latest row.
    #
    # `modal_cycle` is spelled out rather than written `mode(payment_plan_days)` because
    # `mode` has no defined answer when two values are equally common, and an undefined
    # answer is a coin flip -- the same failure `SAME_DAY_TIE_BREAK` exists to prevent.
    # A tie takes the *longer* cycle, which is the choice that shrinks the imputed
    # `ltv_remaining_inr` rather than inflating it.
    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE history AS
        WITH totals AS (
            SELECT msno,
                   min(transaction_date)                                 AS first_seen,
                   count(*)                                              AS transactions,
                   sum(actual_amount_paid)                               AS lifetime_paid,
                   median(actual_amount_paid) FILTER (WHERE actual_amount_paid > 0)
                                                                         AS typical_paid
            FROM tx GROUP BY msno
        ),
        modal AS (
            SELECT msno, payment_plan_days AS modal_cycle FROM (
                SELECT msno, payment_plan_days,
                       row_number() OVER (PARTITION BY msno
                                          ORDER BY count(*) DESC, payment_plan_days DESC)
                                                                         AS rn
                FROM tx WHERE payment_plan_days > 0 GROUP BY msno, payment_plan_days
            ) WHERE rn = 1
        )
        SELECT t.*, m.modal_cycle FROM totals t LEFT JOIN modal m USING (msno)
        """
    )

    # The mandate's current state is whatever its most recent transaction says. Ties on
    # the same day are broken by `SAME_DAY_TIE_BREAK`, which is total -- see its
    # docstring for the coin flip this replaced.
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE latest AS
        SELECT * EXCLUDE (rn) FROM (
            SELECT *, row_number() OVER (
                PARTITION BY msno
                ORDER BY transaction_date DESC, {SAME_DAY_TIE_BREAK}
            ) AS rn
            FROM tx
        ) WHERE rn = 1
        """
    )

    con.execute(
        """
        CREATE OR REPLACE TEMP TABLE standing AS
        SELECT l.*, h.first_seen, h.transactions, h.lifetime_paid,
               h.modal_cycle, h.typical_paid
        FROM latest l JOIN history h USING (msno)
        WHERE l.is_auto_renew
        """
    )
    steps.append(
        FilterStep(
            step="on an auto-renewing instrument",
            why="a standing authorisation, not a one-off purchase or a free trial",
            subscribers=_count(con, "SELECT count(*) FROM standing"),
        )
    )

    # Amount: the latest paid amount, falling back to the list price, then to the
    # subscriber's own typical payment. A mandate with no amount at all is not a
    # mandate -- Mandate.amount_inr requires gt=0 and a zero would be a fiction.
    amount = (
        f"round(coalesce(nullif(actual_amount_paid, 0), nullif(plan_list_price, 0), "
        f"typical_paid) * {india.ntd_to_inr}, 2)"
    )
    # Cycle: the stated plan length, else the subscriber's modal plan length, else
    # the observed expiry span, else the global mode. See docs/mapping.md 3.5.
    span = "date_diff('day', transaction_date, membership_expire_date)"
    cycle = (
        f"coalesce(nullif(payment_plan_days, 0), modal_cycle, "
        f"CASE WHEN {span} BETWEEN 1 AND {MAX_PLAUSIBLE_CYCLE_DAYS} THEN {span} END, "
        f"{india.default_debit_frequency_days})"
    )
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE priced AS
        SELECT *, {amount} AS amount_inr, {cycle} AS debit_frequency_days,
               payment_plan_days = 0 OR payment_plan_days IS NULL AS frequency_imputed
        FROM standing
        """
    )
    con.execute("CREATE OR REPLACE TEMP TABLE priced AS SELECT * FROM priced WHERE amount_inr > 0")
    steps.append(
        FilterStep(
            step="with a recoverable debit amount",
            why="paid amount, else list price, else the subscriber's typical payment",
            subscribers=_count(con, "SELECT count(*) FROM priced"),
        )
    )

    con.execute(
        "CREATE OR REPLACE TEMP TABLE priced AS SELECT * FROM priced "
        "WHERE membership_expire_date IS NOT NULL "
        "AND membership_expire_date <> DATE '1970-01-01'"
    )
    steps.append(
        FilterStep(
            step="with a real coverage end",
            why="the 1970-01-01 epoch is a missing value, and a mandate needs a cycle end",
            subscribers=_count(con, "SELECT count(*) FROM priced"),
        )
    )

    status = f"""
        CASE WHEN is_cancel THEN '{MandateStatus.CANCELLED.value}'
             WHEN membership_expire_date < DATE '{snapshot}'
                  THEN '{MandateStatus.EXPIRED.value}'
             ELSE '{MandateStatus.ACTIVE.value}' END
    """
    # L: revenue still to come if the mandate survives the evaluation horizon.
    # Deliberately horizon-bounded rather than a lifetime figure -- the harness only
    # ever simulates `horizon.weeks`, so a lifetime L would price decisions against
    # revenue the simulation never gets to observe.
    ltv = f"round(amount_inr * ({horizon_days}.0 / debit_frequency_days), 2)"
    con.execute(
        f"""
        CREATE OR REPLACE TEMP TABLE book AS
        SELECT
            p.msno                                              AS mandate_id,
            p.msno                                              AS customer_id,
            {_rail_assignment_sql(india)}                       AS method,
            {status}                                            AS status,
            p.amount_inr,
            p.debit_frequency_days::INTEGER                     AS debit_frequency_days,
            p.frequency_imputed,
            p.membership_expire_date                            AS current_end,
            p.membership_expire_date
                + INTERVAL {india.mandate_validity_days} DAY    AS expire_by,
            {ltv}                                               AS ltv_remaining_inr,
            {params.recovery.after_lapse}                       AS recovery_after_lapse,
            {params.recovery.after_revocation}                  AS recovery_after_revocation,
            round({ltv} * {india.reachability_fraction_of_ltv}, 2)
                                                                AS reachability_value_inr,
            p.first_seen,
            date_diff('day', p.first_seen, DATE '{snapshot}')   AS tenure_days,
            p.transactions,
            round(p.lifetime_paid * {india.ntd_to_inr}, 2)      AS lifetime_paid_inr,
            p.payment_method_id                                 AS source_payment_method_id,
            m.city, m.registered_via, m.gender,
            CASE WHEN m.bd BETWEEN {low_age} AND {high_age} THEN m.bd END AS age_years,
            m.msno IS NOT NULL                                  AS member_record_found
        FROM (SELECT *, (hash(msno || '{RAIL_SALT}') % {HASH_BUCKETS})
                         / {HASH_BUCKETS}.0                     AS bucket
              FROM priced) p
        LEFT JOIN '{members}' m USING (msno)
        """
    )
    steps.append(
        FilterStep(
            step="final mandate book",
            why="members joined LEFT -- a missing demographic row must not delete a mandate",
            subscribers=_count(con, "SELECT count(*) FROM book"),
        )
    )
    return steps


def build(
    params: Params | None = None,
    interim: Path | None = None,
    out_dir: Path | None = None,
    write: bool = True,
) -> MandateBookReport:
    """Build `mandates.parquet` and report exactly who survived to be in it."""
    params = params or load_params()
    out_dir = out_dir or processed_dir()

    con = duckdb.connect()
    try:
        steps = build_book(con, params, interim)
        out_path = out_dir / "mandates.parquet"
        if write:
            ensure(out_dir)
            # ORDER BY is not decoration. Without it the row order of the output is
            # whatever the scan produced, so two runs over identical input write two
            # different files -- and T2.9 asks a stranger's fork for a byte-identical
            # regeneration, which is a claim about the file, not about the counts.
            con.execute(
                f"COPY (SELECT * FROM book ORDER BY mandate_id) TO '{out_path.as_posix()}' "
                "(FORMAT PARQUET, COMPRESSION ZSTD)"
            )

        return MandateBookReport(
            snapshot=params.india.snapshot_date,
            steps=steps,
            mandates=_count(con, "SELECT count(*) FROM book"),
            by_status=_tally(con, "status"),
            by_rail=_tally(con, "method"),
            imputed_frequency=_count(con, "SELECT count(*) FROM book WHERE frequency_imputed"),
            age_known=_count(con, "SELECT count(*) FROM book WHERE age_years IS NOT NULL"),
            members_matched=_count(con, "SELECT count(*) FROM book WHERE member_record_found"),
            total_ltv_inr=float(
                _scalar(con, "SELECT round(sum(ltv_remaining_inr), 2) FROM book") or 0.0
            ),
            megabytes=round(out_path.stat().st_size / 1e6, 1) if write else 0.0,
        )
    finally:
        con.close()


def _scalar(con: duckdb.DuckDBPyConnection, sql: str):
    row = con.execute(sql).fetchone()
    assert row is not None
    return row[0]


def _count(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    return int(_scalar(con, sql))


def _tally(con: duckdb.DuckDBPyConnection, column: str) -> dict[str, int]:
    # The name is a tie-break, not a preference: two statuses with equal counts would
    # otherwise order differently between runs, and this dict becomes a table in
    # docs/mapping.md that is supposed to regenerate identically.
    rows = con.execute(
        f"SELECT {column}, count(*) FROM book GROUP BY 1 ORDER BY 2 DESC, 1"
    ).fetchall()
    return {str(name): int(n) for name, n in rows}


def format_report(report: MandateBookReport) -> str:
    """Markdown, because these numbers are due in docs/mapping.md, not on a terminal."""
    lines = [
        f"Snapshot: **{report.snapshot}**",
        "",
        "| step | why | subscribers |",
        "|---|---|---:|",
    ]
    lines += [step.line for step in report.steps]
    lines += [
        "",
        f"**{report.mandates:,} mandates** ({report.retention:.1%} of the starting "
        f"population), {report.megabytes} MB.",
        "",
        "| status | mandates |",
        "|---|---:|",
    ]
    lines += [f"| `{name}` | {n:,} |" for name, n in report.by_status.items()]
    lines += ["", "| rail (assigned) | mandates | share |", "|---|---:|---:|"]
    lines += [
        f"| `{name}` | {n:,} | {n / report.mandates:.3f} |" for name, n in report.by_rail.items()
    ]
    lines += [
        "",
        "| quantity | value |",
        "|---|---:|",
        f"| debit frequency imputed | {report.imputed_frequency:,} "
        f"({report.imputed_frequency / report.mandates:.1%}) |",
        f"| member record matched | {report.members_matched:,} "
        f"({report.members_matched / report.mandates:.1%}) |",
        f"| age usable | {report.age_known:,} ({report.age_known / report.mandates:.1%}) |",
        f"| total L at risk | INR {report.total_ltv_inr:,.0f} |",
    ]
    return "\n".join(lines)
