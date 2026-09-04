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

## Amendment, 2026-09-04 — the claim names a platform now

This ADR said "across runs" and meant it, and for five days that was checked honestly and
repeatedly — always on the same operating system, because this repository had no git remote
and `ci.yml` had therefore never executed. The first push ran it, and `repro --check` on
`ubuntu-latest` failed on three files.

The drift is not in this code's control: floating-point summation is not associative, so
INR 413,219 accumulates as INR 413,218 on a different platform's math library, and
matplotlib resolves different fonts on Linux so the same chart rasterises different bytes.
No figure this project quotes changed. [`limitations.md` §9](../limitations.md) carries the
measurement and its CI run id.

Moving GATE 5 to `windows-latest` was tried first, on the theory that matching the
platform would be enough. It failed too — smaller, but failed: 384,906 became 384,907 on a
different Windows machine. **This is a machine property, not a platform one.** The order a
CPU and its math library accumulate a sum in is not something `uv.lock` pins, so no runner
can be configured into agreement.

**The three rules above are unchanged** — they fix ambiguity inside this code, which is what
they were written for, and every bug they caught is still caught. What changes is the scope
of the guarantee they add up to:

> byte-identical across runs **on the machine that produced the file**, and identical to
> four significant figures on any other.

CI therefore gates on `scripts/check_drift.py` — named columns, one unit in the last printed
digit, PNG dimensions rather than PNG bytes, everything else exact — on both operating
systems, both blocking. `repro --check` still runs there and still reports byte-exactly; it
just no longer decides the build, because a measured, documented difference is not a
regression. [`limitations.md` §9.4](../limitations.md) has the rule and what was rejected,
including the fix that would actually remove the drift (`math.fsum`, or integer rupees)
rather than tolerate it.

**What would force a revisit (second entry).** Any decision in this system becoming
sensitive to the fourth significant figure of a rupee total. At that point the exact-byte
gate stops being conservative and starts being wrong, and the replacement is a per-artifact
tolerance with a test proving the tolerance cannot mask a real regression — deliberately not
attempted three days before a deadline on the strength of one observation.
