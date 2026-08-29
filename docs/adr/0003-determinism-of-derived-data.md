# ADR 0003 — Every derived file must be byte-identical across runs

Date: 2026-08-29
Status: **Accepted**

## Context

GATE 2 in [`tasks.md`](../tasks.md) asks that "a stranger's fork produces a byte-identical
`results.md`". Until this ADR, nothing enforced that, and it was already false.

Building the mandate book twice from the same input produced `cancelled: 1,053` on one run
and `cancelled: 1,054` on the next. Nothing failed. No test caught it, because every test
asserted counts on a fixture small enough that the ambiguity never arose, and the reports
in [`mapping.md`](../mapping.md) were written from whichever run happened to be last.

The cause is that SQL sorts are not total unless you make them so. About 0.14% of
subscribers have two transactions written on their final day; where those two rows grant
the same coverage but disagree about `is_cancel`, `ORDER BY membership_expire_date DESC`
leaves them tied, and `row_number()` over a tie is resolved by whatever order DuckDB's
parallel scan produced. The same hole existed in three other places:

- `mode(payment_plan_days)`, which has no defined answer when two cycle lengths are
  equally common;
- `arg_min(had_cancel, coverage_until)` in the person-period labels, where a backdated
  cancel makes two days share one coverage end;
- every `COPY ... TO parquet` without an `ORDER BY`, where the rows are right and their
  order is not.

None of these produce a wrong answer. They produce *an* answer, differently each time,
which is worse: a wrong answer gets found.

## Decision

**Three rules, applying to every module that writes or aggregates derived data.**

**1. Every tie-break is total.** A sort used to select one row out of several must order
on enough columns that two rows which still tie are identical rows.
`mandates.SAME_DAY_TIE_BREAK` is that order for transactions and is shared by every module
that needs it, rather than restated.

**2. Ambiguity resolves against our own headline.** Where a tie-break has to choose
between two readings of the data, it takes the one less favourable to this project's
claims. Of a cancel and a non-cancel granting equal coverage, the cancel wins —
under-stating retention. Of two equally common billing cycles, the longer wins — shrinking
the imputed `L`. A rule that resolves ambiguity in its own favour is not a rule.

**3. Every written file is ordered.** `COPY` carries an explicit `ORDER BY` — by key where
one exists (`mandate_id`; `mandate_id, week_index`), or `ORDER BY ALL` where the rows have
no key. Aggregate tallies that surface in documentation order on the value *and* the name.

Each rule is enforced by a test that builds the same input two or three times and compares
the written bytes, not the counts.

## Consequences

- The mandate book moved by 244 mandates out of 1.39M, and the affected numbers in
  `mapping.md` §3 were regenerated. §3.8 records why.
- `data/sample/`, which is committed, is now written `ORDER BY ALL`. Without it every
  rebuild showed four changed binaries in `git diff` for no reason — a spurious diff on a
  committed file is a spurious diff in the history.
- `data/interim/` is deliberately **exempt**. It is gitignored, never reviewed, and
  sorting 21.5M rows on every ingest would cost minutes to fix a diff nobody sees. The
  sample's own `ORDER BY ALL` makes the committed artifact deterministic regardless of
  what order its rows arrive in, which is where the guarantee is actually needed.
- Sorting 46M person-weeks before writing is the slowest step in the T1.4 build. That cost
  is accepted: the alternative is relying on DuckDB's implicit order preservation, which
  is a property of the planner rather than a promise, and the whole point of this ADR is
  to stop depending on things that happen to be true.

## What would force a revisit

A derived frame large enough that the final sort stops fitting the machine. The escape
hatch is a dense integer surrogate key assigned in one deterministic pass and sorted on
instead of the 44-character `msno` — cheaper to sort, at the cost of a lookup table that
every consumer then has to carry.
