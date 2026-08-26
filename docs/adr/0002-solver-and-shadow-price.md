# ADR 0002 — Solver choice, and how the shadow price is obtained

Date: 2026-08-26
Status: **Accepted** (spike S1 passed)

## Context

The project's headline commercial number is `theta` — "the next ask is worth Rs X". As
formulated in [`problem.md`](../problem.md) §4, `theta` is the dual variable on the budget
constraint of the MCKP LP relaxation. If the solver does not return usable duals, that
number does not exist and the pitch loses its most executive-legible output.

[`stack.md`](../stack.md) recorded this as spike **S1**, the highest-risk unverified
assumption in the plan, and scheduled it as the first task of day 1.

## Decision

**Use PuLP with the bundled CBC solver, via `pulp.PULP_CBC_CMD`.**

`theta` is read from the LP relaxation as `prob.constraints["budget"].pi`. The shipping
allocator solves the integer problem; only the relaxation supplies the dual.

## Evidence

`tests/spikes/test_lp_dual.py` — 5 mandates, 3 channels, one budget constraint. Three
assertions, all passing:

1. The LP relaxation solves to optimality and the budget constraint returns a finite,
   positive dual: **theta = 5.5485**.
2. **The dual is economically meaningful**, which matters more than its existence:
   raising the budget by Rs 1 raised the objective by exactly Rs 5.5485, matching theta to
   within 1e-3 relative. A dual that exists but does not predict the objective's response
   would be useless for the "next ask is worth Rs X" claim.
3. The integer solve also returns optimal, respects the at-most-one-channel constraint per
   mandate, and stays within budget.

## Consequences

- The scipy/HiGHS fallback named in `stack.md` is **not needed**. It stays documented as a
  contingency but is not implemented.
- `allocator/theta_search.py` (T3.4) can be built on this directly.
- `allocator/serving_rule.py` (T3.5) needs the same duals for LinkedIn's per-item threshold
  test, and they are available.

## The deprecated-API sub-decision

PuLP 3.3.2 emits `DeprecationWarning`s for the API used here: `PULP_CBC_CMD` (it suggests
`pip install pulp[cbc]` plus `COIN_CMD`), direct `LpVariable` construction, and
`prob.constraints` as a dict mapping. All three change in PuLP 4.0.

**We attempted the modern path and rejected it.** Installing `pulp[cbc]` and calling
`COIN_CMD` **hung with no output and had to be killed** — it did not fail with an error,
it simply did not return. Diagnosing an external solver binary that hangs on Windows is an
unbounded task, and this project has ten days.

So: stay on `PULP_CBC_CMD`, which is verified working here, and pin PuLP 3.x through
`uv.lock`. The deprecation warnings are downgraded from errors in
`pyproject.toml`'s `filterwarnings`, scoped to the `pulp` module only — everything else
still fails the build on a warning.

**What this costs.** The code is written against an API that PuLP 4.0 will remove. Anyone
who upgrades PuLP past 3.x breaks `allocator/`. The lockfile prevents that from happening
accidentally, and this ADR records why, so the next person does not "fix" the warnings and
lose a working solver.

**What would force a revisit.** Needing a PuLP 4.x feature, or CBC proving too slow at
book scale. In the second case the migration target is scipy/HiGHS for the dual plus a
hand-rolled greedy for the integer solution — already sketched in `stack.md`.
