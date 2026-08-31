# Build Task List — MandateGuard

Derived from the MandateGuard Build Plan (Razorpay AI Buildathon 2026, Track 3, v3).
The build plan argues *what* to build and why it is defensible. This document is the
executable version of it: dependency-ordered tasks, a pass/fail gate per phase, and an
explicit cut order.

Technology choices live in a separate document: [`stack.md`](./stack.md).

Deadline: **2026-09-05**. Written: 2026-08-26.

---

## Calendar reality

11 calendar days: Wed 26 Aug through Fri 5 Sep. Day 11 is submission and buffer, not build
time.

| Day | Date | Phase |
|---|---|---|
| 1 | Wed 26 Aug | Phase 0 Ground — repo, spikes, start reading |
| 2–3 | Thu 27 – Fri 28 Aug | Phase 1 Evidence — **the hardest gate** |
| 4–5 | Sat 29 – Sun 30 Aug | Phase 2 Ladder |
| 6–7 | Mon 31 Aug – Tue 1 Sep | Phase 3 Optimizer |
| 8–9 | Wed 2 – Thu 3 Sep | Phase 4 Agent |
| 10 | Fri 4 Sep | Phase 5 Surface, docs, video |
| 11 | Fri 5 Sep | Buffer and submit |

Reading (~17h total) is interleaved, not blocked out. Only two reads gate downstream work
and are pinned to specific days: the RBI circular (before T4.1) and MCKP/LP duality
(before T3.3).

---

## Phase 0 · Ground — Day 1

Goal: an empty but *green* repository, plus early answers to the three questions that
could break the plan later.

- [ ] **T0.1 — Repo skeleton.** `git init`, `uv init`, `src/` layout with package
  `mandateguard` and subpackages `data/ risk/ value/ allocator/ policy/ agent/ ledger/
  safety/ app/ eval/`. Plus `docs/ specs/ tests/ config/ notebooks/ scripts/`.
  `.gitignore` excluding `data/raw/`, `.env`, `*.pkl`.
  **Done when:** `uv run python -c "import mandateguard"` succeeds.

- [ ] **T0.2 — `docs/stack.md`.** Written. First real commit.
  **Done when:** committed, and every row has a rejected alternative.

- [ ] **T0.3 — SPIKE S1: LP duals from PuLP/CBC on Windows.** ~30 minutes. Solve a 5-item
  MCKP LP relaxation, read the budget constraint's dual (`constraint.pi`), assert it is a
  finite non-zero float. **This is the single highest-risk technical assumption in the
  project** — the shadow price theta is the headline executive number and it comes from
  this dual.
  **Done when:** `tests/spikes/test_lp_dual.py` passes, or the scipy HiGHS fallback
  (`res.ineqlin.marginals`) is adopted and `docs/stack.md` amended.

- [x] **T0.4 — SPIKE S2: Kaggle access.** `uv add kaggle`, create an API token, accept the
  WSDM 2018 competition rules, start the `transactions.csv` download in the background.
  **Done when:** the download is running or complete. If Kaggle blocks the account, fall
  back to Telco Customer Churn — it has no `is_auto_renew`, which weakens the Tier-1 claim,
  and `docs/limitations.md` must say so.

- [ ] **T0.5 — SPIKE S3: two-process demo on Windows.** Prove uvicorn and Streamlit start
  from one command and talk to each other over httpx.
  **Done when:** `scripts/dev.py` starts both and Streamlit renders one number fetched from
  FastAPI.

- [ ] **T0.6 — Domain models.** Pydantic v2: `Mandate`, `Channel`, `Decision`,
  `LedgerEntry`, `PolicyRule`, `AllocationRequest`/`AllocationResponse`. These are the
  contract every later layer imports. Mirror Razorpay Subscriptions/Tokens field naming
  where possible.
  **Done when:** models import and a JSON round-trip test passes.

- [ ] **T0.7 — `Policy` interface.** Abstract base:
  `allocate(mandates, budget, week) -> list[Decision]`. Six implementations follow; write
  the ABC and a `NoAskPolicy` stub now.
  **Done when:** `NoAskPolicy` returns empty decisions and a test asserts it.

- [ ] **T0.8 — `config/params.yaml` and `policy/mandate_policy.yaml` v0.** Empty-but-valid
  skeletons with schema validation. Every calibration constant lands here; nothing
  hard-coded.
  **Done when:** the loader test passes on both files.

- [ ] **T0.9 — CI skeleton.** GitHub Actions, one ubuntu job: `uv sync`, `ruff check`,
  `ruff format --check`, `pytest`. Create the public repo with `gh repo create`.
  **Done when:** green check on GitHub.

- [x] **T0.10 — Repo `CLAUDE.md`** and `docs/adr/0001-record-architecture-decisions.md`.
  **Done when:** committed.
  **Done:** ADR 0001 committed in Phase 0; `CLAUDE.md` written during Phase 2, once there
  were enough repeated rules to be worth writing down rather than guessed at. Its first
  section is the one that kept getting skipped: close every task by writing
  `docs/worklog.md` and `docs/seekha.md` before starting the next one.

> **GATE 0** — CI is green and an empty `Policy` implementation imports cleanly.

**Interleaved reading (~5h):** KKBox dataset schema (3h, P1) · Razorpay Subscriptions and
Tokens object model (2h, P1).

---

## Phase 1 · Evidence — Days 2–3

Goal: a lapse-hazard model fit on real subscriber data that beats a naive baseline.

> **This is the most important gate in the project.** The entire Tier-1 claim — "our lapse
> hazard is fit on real data, not invented" — dies here if it fails. Nothing downstream is
> worth building until it passes.

- [x] **T1.1 — Ingest KKBox with DuckDB.** `transactions.csv` (~21M rows) is too large for
  a comfortable `pandas.read_csv` on a laptop. Run DuckDB SQL directly over the CSV, select
  only the needed columns, write `data/interim/transactions.parquet`. Same for
  `members.csv` and the train labels.
  **Done when:** row counts and date ranges are printed and recorded in `docs/mapping.md`.

- [x] **T1.2 — Nail down `is_cancel` semantics.** Cancel is not churn. Verify empirically:
  how many `is_cancel=1` rows are followed by another transaction?
  **Done when:** the answer is a number written into `docs/mapping.md`.

- [x] **T1.3 — India mapping layer.** `payment_method_id` → rail (UPI AutoPay / card / PPI
  / e-NACH); NTD → INR; `payment_plan_days` → debit frequency; `membership_expire_date` →
  mandate validity. Every mapping decision defended in prose, not just in code.
  **Done when:** `docs/mapping.md` is complete and `data/processed/mandates.parquet` exists.
  **Done:** `docs/mapping.md` §3; 1,392,175 mandates (58.9% of subscribers), 104.5 MB.
  Rail, mandate validity and `R` are synthetic overlays and labelled as such in §3.1;
  `transactions_v2` stays out and is held back as out-of-time validation (§3.2).

- [x] **T1.4 — Person-period expansion.** Convert each subscriber into one row per week
  alive, with `event=1` on the lapse week. This is the shape a discrete-time survival model
  needs.
  **Done when:** a test asserts no subscriber has post-event rows.
  **Done:** `docs/mapping.md` §5; 58,079,041 person-weeks over 1,379,341 spells, 183.3 MB.
  Per-week death rate 0.0130, and the baseline hazard is **not** flat -- weeks 4-7 run at
  0.0740, 4.5x the average, which is the first renewal of a 30-day plan (§5.7). Features
  are recomputed as-of each week rather than copied from the snapshot book; the leakage
  barrier has its own test. Deaths carry `lapse`/`revocation` so `q` and `r` stay
  separable.

- [x] **T1.5 — Build the CI sample.** Deterministically sample ~5,000 subscribers into
  `data/sample/` (a few MB) and **commit it**. CI cannot download the full dataset; every
  later `results.md` regeneration runs on this sample.
  **Done when:** committed, and the full-data and sample paths share one code path behind a
  `--sample` flag.
  **Done:** `docs/mapping.md` §4; 5,079 subscribers, 0.77 MB, all four tables. `--sample`
  swaps a directory in `paths.source_dir()` and nothing downstream branches on it. The
  sample reproduces the full book's shape (retention 59.6% vs 58.9%, `q` 0.387 vs 0.407)
  and enters every branch the pipeline has (§4.3).

- [x] **T1.6 — Naive baseline.** "Risk equals closeness to expiry." Score it first, so the
  real model has something to beat.
  **Done when:** the baseline Brier score is recorded.
  **Done:** `docs/eval.md` §1. Three baselines, out-of-time split, scored in SQL over the
  full 6.35M-row held-out slice. `expiry_bins` Brier **0.007400**, log loss **0.0401**;
  `base_rate` Brier **0.007269**. The taken-literally rule (`expiry_rule`) is 29x worse
  than the floor, which is the number that justifies the project. `expiry_bins`
  discriminates better than the floor and still loses on Brier, because the training
  period's death rate is 0.0139 and the test period's is 0.0073 — a duration-mix shift
  a period-agnostic lookup cannot track. §1.5 states the two specific bars T1.7 has to
  clear.

- [x] **T1.7 — Discrete-time survival model.** `sklearn` logistic regression on the
  person-period frame, per-week hazard. Not Cox: simpler, calibrates better, easier to
  defend on a whiteboard.
  **Done when:** the model fits, predictions are in [0,1], and the seed is fixed.
  **Done:** `docs/eval.md` §2. 56 features, fitted on a 1M-row deterministic subsample
  (seed 20260905), scored as a SQL expression over all 6.35M held-out rows. Brier
  **0.006817** (skill **+0.0621**), log loss **0.0369** — clears both bars §1.5 set.
  A test asserts the SQL scoring path reproduces sklearn's `predict_proba` to 1e-9, and
  another asserts the five excluded columns appear in no feature.

- [x] **T1.8 — Calibration, Brier, plots.** `calibration_curve`, `brier_score_loss`,
  reliability diagram committed as a PNG.
  **Done when:** the plot is in `docs/img/` and the numbers are in `docs/eval.md`.
  **Done:** `docs/img/reliability.png`, numbers in `docs/eval.md` §3. ECE **0.00363**
  against the binned baseline's 0.00722. The honest finding: the predictions are too
  spread out — the top bucket over-predicts by 1.6x, which biases the optimiser toward
  asking. Recorded in the model card and `limitations.md` rather than smoothed over.

- [x] **T1.9 — `docs/model_card.md`.** Data, features, performance, limitations, intended
  use. The cleanest AI-maturity signal available, and it is cheap.
  **Done when:** committed.
  **Done:** seven sections, limitations ordered by how much they should worry a reader.
  Names the two strongest features that are about *our own data* rather than about
  customers (`frequency_imputed`, `member_known`) and says they would not survive a move
  to a merchant's book.

- [x] **T1.10 — `docs/calibration.md`.** Every India-layer number mapped to its citation:
  UPI AutoPay 50M new mandates/month, 808M executions/month, 20M+ revocations/month, ~74%
  business decline across the top 50 banks; card post-2021 failure 20%+ in some categories;
  the 2021 migration at ~70% decline and 62.5M mandates over 9 months. **Verify the
  20M/month figure yourself** — you will be asked about it.
  **Done when:** no number anywhere in the repo lacks a source line.
  **Done:** four origins, and every number in the repo is in exactly one — sourced,
  measured, swept, or **unverified and named as such** (§5). The 20M/month figure is
  verified: Business Standard, Sept 2025, citing NPCI via industry sources; it is a
  *revocation* count driven by insufficient balance, not a churn count, and that is the
  answer if the panel asks. Two corrections fell out: the volumes are **July 2025**
  figures and `problem.md` now says so, and the 62.5M migration took about **eight**
  months, not nine. The "card post-2021 failure 20%+" claim **could not be sourced** and
  is struck. The RBI circular is real — RBI/DPSS/2026-27/396, 21 April 2026 — and its
  ₹15,000 AFA ceiling is where `params.yaml`'s threshold comes from.

> **GATE 1** — The hazard model beats the naive baseline on Brier score, and
> `docs/model_card.md` is committed.
> **If it fails:** stop. Do not proceed to Phase 2. Either fix the features, or fall back to
> a Tier-2-only framing and rewrite the pitch honestly. Do not paper over it.

**Interleaved reading (~5h):** the RBI e-mandate circular, actual text, 21 Apr 2026 (3h,
P1) · discrete-time survival / logistic hazard (2h, P2).

---

## Phase 2 · Ladder — Days 4–5

Goal: the evaluation harness and four baselines, **before** building your own optimizer.
Building baselines first is deliberate: it stops you from tuning the harness to flatter
your own policy.

- [x] **T2.1 — Eval harness.** For each week over a 12-week horizon, hand the policy the
  live mandate book and that week's budget, collect decisions, roll the world forward using
  the hazard model plus the intervention-effect parameters.
  **Done when:** the harness runs `NoAskPolicy` end-to-end and returns a metrics dict.
  **Done:** `eval/forecast.py` rolls each live mandate's features forward and scores them
  with T1.7's own SQL expression; `eval/world.py` runs the week loop. Deterministic
  expectations rather than Monte Carlo (ADR 0003, and T2.7 needs a smooth curve). 1,354
  live mandates on the sample; P0 retains 89.758%.

- [x] **T2.2 — Metrics module.** Mandates retained · revocations caused · INR ARR retained
  · asks spent · INR per ask · shadow price theta (null until P4).
  **Done when:** all six are computed and unit-tested on a hand-built 3-mandate fixture.
  **Done:** `RunMetrics` in `eval/world.py`, 20 tests in `tests/test_world.py` on a
  3-mandate 2-week fixture where every number is checkable on paper (retained = 0.25 +
  0.81 + 1.0). `INR/ask` is self-contained — net value created per ask, not a difference
  against P0 — so an arm can be scored without running another one first.

- [x] **T2.3 — P0 `NoAsk`.** The floor. Without it, "how much did we save" means nothing.
  **Done when:** the harness reports it.

- [x] **T2.4 — P1 `ChronologicalCap`.** First-come until budget B. This is the real
  industry default (Braze, MoEngage, CleverTap).
  **Done when:** the harness reports it.

- [x] **T2.5 — P2 `RoundRobin`.** Same budget, fair rotation. This is the arm that made
  ARMMAN's result credible. **Never cut this arm.**
  **Done when:** the harness reports it.

- [x] **T2.6 — P3 `GreedyEV`.** Top-B by expected value. The honest simple baseline — and
  what many "smart" systems actually are.
  **Done when:** the harness reports it.

- [x] **T2.7 — Budget sweep infrastructure.** Sweep the budget from 0 to 3x optimum and
  produce the inverted-U curve. Zhang's calibration: optimum 7; sending 10 costs −16%
  profit; sending 4 costs −32%. Under-asking is roughly twice as expensive as over-asking,
  so the curve must come out asymmetric.
  **Done when:** the curve plots and the asymmetry is visible.
  **Done:** `eval/sweep.py`, geometric ladder from 0 to saturation. **The asymmetry is
  not visible, and that is the finding:** at the shipped parameters every arm's optimum
  is a budget of zero, so the curve is monotone and there is no interior optimum to be
  asymmetric about. Zhang's shape only appears where an ask is worth making, which is
  what T2.8 maps. The reading code is tested against a hand-built Zhang-shaped curve, so
  the machinery is there the moment the region moves.

- [x] **T2.8 — Sensitivity grid infrastructure.** The (uplift × backfire) plane. Do not
  fill it with a point estimate — sweep it and draw the region where MandateGuard beats
  round-robin.
  **Done when:** the grid runner produces a heatmap on the sample data.
  **Done:** `docs/results.md` §4 and `docs/img/sweeps.png`. 6x6 `(uplift x backfire)`
  plane. There is a clean diagonal frontier and **the shipped point sits just on the
  wrong side of it**. Cells where the challenger asked nobody are parenthesised, because
  its advantage there is the reference burning money, not selection working — conflating
  the two would be the most flattering possible reading.

- [x] **T2.9 — `results.md` auto-generation in CI.** A CI job runs the harness on the
  committed sample with fixed seeds and regenerates `results.md` plus raw artifacts.
  **Done when:** a stranger's fork produces a byte-identical `results.md`.
  **Done:** `scripts/make_results.py` builds the book, the frame, the hazard, the ladder
  and both sweeps from `data/sample/` alone and writes `docs/results.md` plus its PNG.
  CI regenerates both on ubuntu and runs `git diff --exit-code`, so the gate is enforced
  rather than promised. Verified byte-identical across runs locally; the cross-platform
  half is what the CI job is for. Emitted model coefficients are rounded to 6 decimals
  so `lbfgs` convergence noise cannot surface as an unexplainable failing diff.

> **GATE 2** — `results.md` is auto-generated by CI with four arms in it.

---

## Phase 3 · Optimizer — Days 6–7

Goal: the two arms that are actually yours, plus the value function that makes the
prior-art traceability real.

Two structural errors get fixed by this phase's design. A unit-cost "knapsack" is really
just a greedy sort — fixed by giving channels different costs, which makes it a genuine
multiple-choice knapsack. And single-period allocation cannot express "ask later" — fixed
by making the decision variable `(mandate, channel, week)`.

- [x] **T3.1 — Channel cost table** in `config/params.yaml`: in-app ₹0, email ₹0.05, SMS
  ₹0.15, WhatsApp ₹0.35, IVR ₹2, physical letter ₹25, agent call ₹40 — each with its own
  efficacy prior. Channel variation is also a regulatory requirement (RBI KYC directions
  want at least one physical letter per phase), not decoration.
  **Done when:** loaded and validated.
  **Done:** the loader now rejects duplicate names, a table with no intrusive channel,
  and — the useful one — any **dominated** channel (as cheap and no less effective than
  another). A dominated channel is one no optimiser would ever pick, which would make
  T3.3's multiple-choice knapsack quietly smaller than the config claims. The letter
  channel is sourced: RBI's KYC (Amendment) Directions, 2025 require at least one letter
  per escalation phase (`docs/calibration.md` §1.2).

- [x] **T3.2 — `value/` — the four-term rupee price.** Each term traces to a different
  paper:
  - `value/prices.py` — **LinkedIn, KDD 2016:** separate mu (good-outcome price) and nu
    (complaint price). Not one netted number.
  - `value/reachability.py` — **Twitter/X, 2022:** `alpha · P(still reachable)` as its own
    term. Losing the UPI AutoPay rail to a customer costs more than losing that one
    subscription. This is the single most differentiating modelling choice in the project.
  - `value/ltv.py` — **Pinterest, KDD 2018**, extended per
    [`problem.md`](./problem.md) §6.2: `LTV_remaining` carries **two separate recovery
    probabilities**, not one — `q[i]` for a customer whose mandate **lapsed** quietly, and
    `r[i]` for one who **revoked** in irritation, with `q > r`. So:
    `loss on lapse = L·(1−q)` but `loss on revocation = L·(1−r) + alpha·R`.
    Two things break if this collapses to a single number. With one shared recovery
    probability the model cannot express that an ask which fails can **convert a soft
    ending into a hard one** — the whole reason contacting a probably-doomed mandate is not
    free. And with `r = 0` (revocation as total loss) the system becomes pathologically
    conservative and stops asking anyone.
    **Config:** `params.yaml` needs both `recovery_after_lapse` and
    `recovery_after_revocation`, with a validator asserting `q > r`.
  - `value/fatigue.py` — **Duolingo, KDD 2020:** `−gamma · 0.5^(d/h)`, half-life ~15 days,
    **plus a template-reuse penalty**. This is what wires the LLM layer into the optimizer
    instead of leaving it decorative.
  - `value/channel_priors.py` — **Chrome, USENIX Security 2021:** a softer channel loses
    2–5% of grants but avoids 17–31% of *permanent* refusals.
  **Done when:** each file has a docstring naming its source paper, and a unit test — plus
  one test that fails if `q <= r`, and one that asserts the loss on revocation exceeds the
  loss on lapse for the same mandate.
  **Done:** five modules plus `value/price.py` composing them, 24 tests. Both named
  assertions are in `tests/test_value.py`. Writing them found two things: `MandateWeek`
  did **not** enforce `q > r` (only `Mandate` did, and the policy never sees a `Mandate`),
  and the world and the pricer were using **different physics** — the pricer softened
  backfire by channel and the harness charged the unsoftened rate, so P3 bought asks its
  own arithmetic called profitable and the harness scored them at a loss. Both fixed;
  there is now one definition of what an ask does, shared by the simulator and every arm.

- [x] **T3.3 — P4 `MCKP` batch solver.** Multiple-Choice Knapsack: per mandate choose **at
  most one** channel, each with its own cost and efficacy. PuLP + CBC. **Depends on T0.3.**
  **Done when:** the solver returns a feasible within-budget allocation on a 100-mandate
  fixture.
  **Done:** `allocator/mckp.py`, 17 tests. Feasibility is checked as three separate
  claims — every mandate gets a decision, none is contacted twice in a week, spend stays
  inside the budget. **theta is real**: positive when the budget binds, zero when it does
  not, and it predicts what another rupee actually buys (spike S1's assertion, on the
  hundred-mandate fixture rather than the five-mandate toy).
  Two things fell out. The first draft put *gross* profit in the objective and P4 bought
  258 asks for a **negative** net value — a pair can be gross-positive and net-negative,
  and a gross objective cannot tell. And with a zero-cost channel configured, **no mandate
  is ever refused for lack of budget**: anything worth contacting can be contacted free,
  so the budget rations *which channel*, not *whether*. That is `problem.md` §5.1's thesis
  falling out of the solver rather than being asserted at it.

- [x] **T3.4 — `allocator/theta_search.py` — Pinterest's theta.** Hill-climb plus binary
  search on the shadow price so total asks match the global budget. This is the *algorithm*
  for producing theta, not just the name of it.
  **Done when:** theta converges and total asks land within ±2% of budget.
  **Done:** `allocator/theta_search.py` plus `allocator/candidates.py`, which T3.3 now
  shares so the two solvers argue over one candidate set rather than two lookalikes.
  47 tests, and `scripts/run_theta.py` prints `docs/eval.md` §4.
  **Convergence: yes** — all 8 binding budgets on the sample book, slowest 56 steps of a
  64 cap, bracket closed to 1e-13. **The ±2% fit: 5 of 8.** It misses at three budgets,
  and the miss is *optimal rather than a failure*, which is checked rather than argued:
  `affordable_upgrades()` reports **zero** profitable asks that would have fit in the
  unspent slack, so no allocator — CBC included — could have spent it. The gate is a
  property of the instance, not of the algorithm.
  The number worth having is the cross-check: the searched theta and CBC's LP dual agree
  to **0.00%** across all 8 budgets, and the search plus its greedy repair captures
  **99.901%** of CBC's integer optimum with no solver in the loop. That is what T3.5's
  online rule stands on.
  Two things fell out. The bracket was being taken from `profit / cost`, which is the
  wrong crossing whenever a **free** channel exists: `in_app` never falls with theta, so a
  paid channel has to beat the fallback rather than beat zero, and the hill-climb was
  starting above the entire region where anything changes hands. And **theta is not
  monotone in the budget** — widening it unlocks dearer, more effective rungs, so the
  marginal rupee can be worth *more* than before. Both are pinned as tests, because the
  second looks exactly like a convergence bug and is not one.

- [x] **T3.5 — `allocator/serving_rule.py` — LinkedIn's online rule.** The LP-dual per-item
  threshold test `mu·P(re-consent) − nu·P(revoke) − cost > 0`, so mandates can be decided
  one at a time without re-solving the knapsack. **The batch-vs-online comparison is itself
  a result** — report both.
  **Done when:** the online rule reproduces batch MCKP within a stated tolerance.
  **Done:** `allocator/serving_rule.py`, 33 tests, and `docs/eval.md` §5 reports both.
  `P4o` is a **variant of P4, not a sixth rung** — same value function, same price, no
  solver. The tolerance, stated: with the price refreshed weekly it reproduces batch P4
  **exactly** at every budget from ₹8.46 up, and its worst showing anywhere is **76.82%**
  of P4's gain over doing nothing. Reported against the *gain*, never against total
  profit — on total profit every arm scores 99.99% of every other and the comparison says
  nothing.
  Two equivalences make it a real reproduction rather than a resemblance, and both are
  tests: at the same theta with the meter never binding the online rule reproduces the
  batch Lagrangian selection **exactly**, and at `theta = 0` it collapses to the plain
  four-term LinkedIn threshold — so the budget-aware rule *contains* the budget-free one.
  Three findings. **Staleness costs more than the batch/online choice does**: one price
  held twelve weeks captures 51.65% of the gain at worst against 76.82% refreshed weekly,
  with the rule, the value function and the book identical. **The residual gap is the
  repair step, and it is unrecoverable at any refresh rate** — the greedy fill ranks every
  mandate's upgrade against every other's, so it needs the whole book; seeing one mandate
  at a time costs exactly the part of the answer that needs to see them all. And T3.4 had
  a **silent** defect this surfaced: `ThetaSolution` published a *rounded* theta beside a
  selection computed at the *unrounded* bracket, so re-deriving the allocation from the
  published price — which is precisely what this arm does — returned a different, dearer
  basket. Nothing raised. Rounding is now applied before selection, and upward, so the
  published price can only ever drop candidates and the budget stays respected.

- [x] **T3.6 — Sanity check the shape.** LinkedIn's published shape: volume −64.5%,
  sessions only −1.8%, complaints −47%. Your result should have the same shape — far fewer
  asks, slightly less retention. If it does not, something is wrong.
  **Done when:** the three deltas are printed and compared in `docs/eval.md`.
  **Done:** `eval/shape.py`, 12 tests, `docs/eval.md` §6. **This is the check the project
  fails, and it is the most valuable section in the document.**
  | axis | LinkedIn | here |
  |---|---:|---:|
  | volume | −64.5% | **−99.3%** |
  | engagement | −1.8% | **+7.4%** |
  | complaints | −47% | **−99.7%** |
  Direction agrees on volume and complaints. Magnitude does not, and retention moves the
  **wrong way** — cutting asks is not supposed to *raise* the thing the asks were for.
  The obvious suspect was backfire, so `anchor()` sweeps it over four orders of magnitude
  and asks which value reproduces LinkedIn's triple. **None does.** Distance rises
  monotonically with backfire and the closest comparable fit still cuts volume 91.3%.
  Turning the one knob we had does not close the gap — a negative result worth more than
  a fitted value would have been. (Rows where neither arm causes a revocation are scored
  on two axes not three, so they are excluded from "closest": a row that wins by dropping
  the axis we are furthest off on has not won anything.)
  What backfire *does* control is the retention axis — +7.4% shipped, −0.2% at the bottom
  of the sweep, which is LinkedIn's direction. So the wrong-way number is a consequence of
  an unmeasured parameter, not an independent finding.
  The residual explanation is the **book**: §1's median projected hazard is 0.0016/week, so
  almost no ask is worth its cost at any backfire rate. **A −99.3% volume cut is a claim
  about the book, not an achievement.** Two consequences now binding on the rest of the
  project: this is *not* LinkedIn-shaped validation and the pitch does not get that
  sentence, and the honest headline stays the rupee gain over doing nothing (₹212), not a
  retention percentage. Carries into T3.10.

- [x] **T3.7 — Segment plot (Pinterest's testable prediction).** Pinterest found an
  inverted-U: the most active *and* the most dormant users got the fewest messages. Your
  allocator should give the healthiest and the most doomed mandates the fewest asks. If it
  does, that is independent validation. If it does not, explain it. Either way the plot
  ships.
  **Done when:** the plot is in `docs/eval.md`.
  **Done:** `eval/segments.py`, 12 tests, `docs/eval.md` §7, `docs/img/segments.png`.
  **It does not.** We get a **threshold**, not an inverted U — eight of ten hazard buckets
  (80% of the book) receive zero asks, then 0.074 and 0.728 asks per mandate. Strictly
  monotone, checked at 25 buckets as well as 10 in case a U was hiding in the top decile.
  **The explanation was written before the measurement, not after it.** The gain from an
  ask is `alive·(1−b)·h·uplift·efficacy·L_lapse` — *linear in h* — while the backfire cost
  does not involve `h` at all. So the value of asking only ever rises with risk and the
  rule is structurally a threshold. Pinterest's right-hand tail needs a falling *response
  probability* at the dormant end; here `efficacy_prior` belongs to the **channel** and is
  identical for every mandate, so a mandate three days from expiry and one certain to lapse
  are assumed equally persuadable. That is the honest modelling gap this plot exposes, and
  it goes to T3.10 as the second-named thing the model is missing.
  The one candidate for bending the curve down was `alive` (it scales gain and backfire but
  not cost). Measured: the top decile's mean hazard is 0.02512/week so **73.7%** of it
  survives the horizon — the survival weight never collapses, against a gain 8.9x larger at
  the top than the bottom.
  Two process notes. `world.RunMetrics` gained `asks_by_mandate` — a distribution cannot be
  read off aggregate counts. And the first draft of §7.3 typed "around 0.06", "roughly half"
  and "50x", all read off the top bucket's **range midpoint** instead of its members' mean;
  the range is 0.01667–0.09673 and skewed, so the midpoint is nowhere near it. Caught by
  going to compute them. They are generated now.

- [x] **T3.8 — [CUT #1] P5 `WhittleIndex`.** Multi-period restless bandit over
  `(mandate, channel, week)`, 12-week horizon, per-week budgets, binary search on the
  subsidy lambda. If Day 7 ends without it working, ship five arms and say so.
  **Done when:** the six-arm chart renders, or the arm is formally dropped in
  `docs/eval.md`.
  **Done — not cut.** `allocator/whittle.py`, 14 tests, `docs/eval.md` §8, and the
  six-arm chart is in `results.md` §2. Backward induction over `(week, asks, recency)`,
  vectorised across the whole book with numpy; the index is the subsidy at which acting
  and waiting are indifferent.
  **P5 buys the same 109 asks as P4 and is worth 9.8% more for them.** Identical volume,
  different schedule: P4 front-loads 70 asks into the first three weeks, P5 puts 42 in the
  back half against P4's 20. Nothing else differs, so the entire margin is timing.
  **Why there was timing to find** — the aggregate hides it. `results.md` §1's median
  hazard barely moves (0.00164 → 0.00140), but that median is *across* mandates: the
  median mandate's own hazard varies **60x** across the horizon and peak weeks are spread
  over all twelve. A cross-sectional median of a time series says nothing about the time
  series. Checked before building, per the T3.7 lesson.
  Three things this cost, all recorded in §8.3 and the journals:
  * **The arm shipped with its answer depending on its own tolerance** — 31 asks at 8
    bisection steps, 108 at 16, 109 at 24. The bracket was a single global ceiling (~1,300
    against an index under a rupee), so the halvings were spent travelling. A bracket safe
    as a *bound* was wrong as a *bracket*. Per-mandate bracket seeded at `lambda = 0`
    fixed it; stable from six halvings now, and pinned.
  * **`id()`-keyed memoisation served one book's arrays to another.** ids are unique only
    among *live* objects, so a collected `Horizon` handed its address to the next. It
    surfaced as an index identical to six decimals across hazards 0.10–0.30 — nearly
    written up as a finding about the value function. Tables are threaded as an argument
    now.
  * **`MandateWeek` gained `hazard_path`, offered to every arm.** The harness had it since
    T2.1a and was not passing it on. Without it P5 was worth +1% over P4; with it, +9.8%.
    Given to all six arms deliberately — handing it to P5 alone would make the result a
    claim about *information* rather than about formulation.
  Honest limits: the index is a heuristic (indexability unverified), the free channel is
  excluded from it for the same reason it broke T3.4's bracket, and it costs ~50s a run —
  so it is in the ladder and deliberately out of the sweeps.

- [x] **T3.9 — `eval/holdout.py` — Meta's identification design.** A 50% random-drop
  holdout matching `P(active|do(send)) − P(active|do(drop))`. You cannot run this in
  production, but building the harness in that shape is the serious answer to "how would
  you validate this?"
  **Done when:** the harness supports a random-drop arm.
  **Done:** `eval/holdout.py`, 16 tests, `docs/eval.md` §9. `RandomDrop` wraps *any* arm
  rather than being a seventh one — there is no "the holdout policy", only a holdout
  applied to one.
  **The headline is the gap between the two contrasts, on one run of one arm:**
  | contrast | effect |
  |---|---:|
  | naive: contacted vs untouched | **−0.1852** |
  | holdout: sent vs withheld | **+0.0039** |
  Same allocator, same book, same run. Report it one way and the system destroys 18.5% of
  retention; report it the other and it adds a little. The allocator contacts the mandates
  most likely to die, so the contacted group is sicker *before* anything is sent — that is
  selection measured, not asserted, and it is the clearest argument in the document for
  why the design is not ceremony. It is also the number a notification dashboard shows.
  **The second finding is about the pilot, not the allocator.** Over 8 independent draws
  of the assignment the effect is +0.0128 with a spread of 0.0142 — **a ratio of 0.9, so
  the correctly-designed experiment cannot detect its own effect.** P4 selects only 107
  mandates, so each arm holds ~53. Standard error falls with √n, so a pilot needs roughly
  **521 selected mandates (≈5x)** before it could distinguish its result from zero. Worth
  knowing before running a pilot rather than after; carries into T3.10.
  Two design points: the coin is flipped **inside the policy's own selections**, not across
  the book (otherwise the control group fills with mandates nobody would have contacted and
  the estimate dilutes toward zero); and assignment is a salted `hashlib` hash of the
  mandate id — **never the builtin `hash()`**, which Python salts per process, so the
  experiment would have been irreproducible in the one way nothing flags, since a slightly
  different number every run is exactly what a noisy experiment looks like. Its salt
  differs from the sample's, per the bug `data/sample.py` records.
  Uncertainty is a **randomisation distribution**, not a standard error: the harness carries
  expectations, so given an assignment the outcome is fixed and the assignment is the only
  random object in the design. `RunMetrics` gained `alive_by_mandate` — a contrast between
  two groups cannot be read off an aggregate that already summed across the split.

- [x] **T3.10 — `docs/limitations.md`, written now, not at the end.** Include the Adyen
  sanity check in your own words: Adyen's contextual bandit beats a fixed retry schedule by
  ~6%, the most trustworthy public number in payments. If your simulator claims 40%, that
  is a red flag, not a result. Doubting your own number is the highest-credibility move
  available.
  **Done when:** committed, and it names the three things needed before production — real
  India mandate data, an intervention holdout, and a shadow-mode merchant pilot.
  **Done:** `docs/limitations.md`, seven sections. The three production gates are §6.1–§6.3,
  each carrying a measured requirement rather than an intention: the holdout is already built
  (§6.2) and the pilot has a **minimum size** (§6.3, ~521 selected mandates from `eval.md` §9).
  **The Adyen check is generated, not typed** — `results.md` §5, so it re-runs in CI and will
  flip its own verdict if a parameter change pushes the lift into red-flag territory.
  **What the check found is not what it was meant to find.** The comparable figure is `P4`
  against `P1` (Adyen contrasts adaptive against *fixed active*, not against doing nothing),
  and it is **+7.42%** against Adyen's ~6% — passing. Then two problems surfaced:
  * That is the **same contrast** `eval.md` §6.1 already reports as its engagement axis,
    where it is a **failure** (LinkedIn lost 1.8%, we gain 7.4% — the wrong direction). One
    number, passing one external check and failing another, both readings correct. Writing
    only the flattering one would have put two of this project's own documents in direct
    contradiction.
  * It is governed by `intervention.backfire_first_ask`, which runs the lift from −0.2% to
    **+36.5%** across its swept range — so at the top of the sweep the check *fails*. The
    verdict holds at the shipped parameter and is not robust to it.
  **And that parameter is not in `calibration.md` §4**, although `eval.md` §6.2 cites §4 as
  its origin. The register of unsourced constants was missing the constant the headline turns
  on; it is now §2.1 of `limitations.md`, ranked first.
  **Two documentation bugs fixed on the way:** `eval.md` had §6.2 duplicated verbatim (24
  lines, an unnumbered copy above the numbered one), and `results.md`'s old §5 is now §6.

> **GATE 3 — PASSED.** The six-arm chart exists (`results.md` §2: P0–P5, none cut) and
> theta prints as a real rupee number — INR 4.5626 at the tightest binding budget on the
> sample book, agreeing with CBC's LP dual to 0.00% (`eval.md` §4). Both halves met.
>
> **Phase 3 is closed:** T3.1–T3.10 all done, no task cut. Cut #1 (T3.8 P5 Whittle) was
> offered and not taken.

**Interleaved reading (~5h):** MCKP and LP duality (3h, P1, **before T3.3**) · ARMMAN
AAAI 2022 Whittle index (2h, P2, before T3.8).

---

## Phase 4 · Agent — Days 8–9

Goal: an LLM layer that is a shipped system, not an API call. The eval suite is what proves
the difference.

- [ ] **T4.1 — Job 1: Policy compiler.** Claude reads the actual RBI circular text and
  compiles it into `mandate_policy.yaml` with clause citations. You review the diff.
  Human-in-the-loop by design; every YAML rule traces back to a clause. **Depends on the
  RBI circular read.**
  **Done when:** there are 10 or more rules in the YAML, each with a clause reference.

- [ ] **T4.2 — Job 2: Mandate auditor.** A structured verdict per mandate: `compliant`,
  `non_compliant`, or `needs_human`. The third is a first-class output with a reason, not a
  fallback.
  **Done when:** verdicts validate against the Pydantic schema and `needs_human` is
  reachable.

- [ ] **T4.3 — Job 3: Notice composer.** An RBI-compliant pre-debit notice with a
  piggybacked re-consent CTA.
  **Done when:** it generates a notice.

- [ ] **T4.4 — Deterministic compliance linter.** Plain Python, no LLM. Must assert that
  amount, date, opt-out path, at least 24h lead time, and merchant name are present, and
  that dark patterns are absent. Fail leads to regenerate; two failures escalate. **No
  unvalidated LLM text ever reaches a regulated notice.**
  **Done when:** the linter has its own unit tests, including deliberately bad notices.

- [ ] **T4.5 — Job 4: Refusal explainer.** A plain-language, rupee-backed reason for every
  *not-asked* decision.
  **Done when:** ledger entries carry a human-readable reason string.

- [ ] **T4.6 — Golden set: ~120 mandate edge cases** with expected verdicts, **including
  expected abstains**.
  **Done when:** `tests/golden/mandates.jsonl` is committed.

- [ ] **T4.7 — LLM eval suite in CI.** Metrics: accuracy, abstain precision and recall,
  cost per verdict, p95 latency. Generates `docs/llm_eval.md` on every push. Run the golden
  set through the Batch API at 50% cost — 120 cases per push adds up.
  **Done when:** `docs/llm_eval.md` is CI-generated.

- [ ] **T4.8 — [CUT #2] Red-team agent.** An adversarial generator producing cases designed
  to break the auditor: ₹14,999 versus ₹15,001, the FASTag/NCMC exemption, a mid-cycle
  modification, a pre-April-2026 grandfathered mandate, cross-border, the ₹1 lakh insurance
  cap, a variable-amount mandate with a customer cap. Then report **natural-set accuracy
  versus adversarial-set accuracy** — the gap between them is your honesty score.
  **Done when:** the gap number appears in `docs/llm_eval.md`.

> **GATE 4** — `docs/llm_eval.md` is CI-generated and contains the adversarial gap, or
> states that the red-team arm was cut.

**Interleaved reading (~2h):** NPCI UPI AutoPay mandate lifecycle (1h, P3) · LLM eval
practice and abstain metrics (1h, P3).

---

## Phase 5 · Surface — Day 10

Goal: everything that turns working code into a submission.

- [ ] **T5.1 — `ledger/` append-only JSONL.** Every decision, **asked and not-asked**, with
  reason, rupee number, policy hash, model version, seed, and template ID.
  **Done when:** a full eval run produces a ledger and a test asserts append-only.

- [ ] **T5.2 — `replay`.** `mandateguard replay --decision-id X` re-runs any historical
  decision from `(policy_hash, model_version, seed, snapshot)`. Payments engineers
  recognise this instantly.
  **Done when:** replaying a decision reproduces it byte-identically.

- [ ] **T5.3 — `safety/`.** Shadow mode by default (propose, never act), kill switch, spend
  cap, rate limiter, and the degradation ladder: LLM down leads to rules-only; a stale model
  leads to the conservative floor; a policy-hash mismatch halts and alerts.
  **Done when:** **a test proves the spend cap cannot be crossed**, and every ladder step
  has its own test.

- [ ] **T5.4 — Chaos tests in CI.** Kill the LLM, corrupt the policy file, feed nulls. The
  system must degrade, not crash.
  **Done when:** the chaos suite is green in CI.

- [ ] **T5.5 — FastAPI service.** `/allocate`, `/explain`, `/ledger`, `/replay`.
  **Done when:** the OpenAPI docs render and all four return valid responses.

- [ ] **T5.6 — [CUT #5] Streamlit surface.** Book view, budget dial, six-arm chart,
  sensitivity heatmap, ledger tab, per-mandate rupee breakdown. Talks to FastAPI over httpx.
  **Done when:** the budget dial moves and the six-arm chart moves with it.

- [ ] **T5.7 — Remaining docs.** `problem.md` (framed by the Chrome 300M-user result),
  `prior_art.md` (the ten-system table — nobody writes this doc in a hackathon),
  `architecture.md`, `adr/`, and a final pass on `eval.md`.
  **Done when:** all nine docs exist and cross-link.

- [ ] **T5.8 — One-command reproduction.** `uv run mandateguard repro` runs the full eval on
  the committed sample and regenerates `results.md` and every plot.
  **Done when:** verified in a fresh clone in a temporary directory.

- [ ] **T5.9 — 5-minute video.** 0:00 problem, in numbers not adjectives · 0:40 the three
  gaps · 1:05 the Chrome result · 1:10 the evidence stack · 1:40 live demo · 3:00 refusal
  ledger · 3:40 **live chaos test** · 4:15 the Subscription Recovery collision · 4:45
  limitations, unprompted.
  **Done when:** recorded and under 5:00.

- [ ] **T5.10 — Interview answer sheet.** Write the eight panel answers into
  `docs/interview_prep.md`. Not submitted — this one is for you.
  **Done when:** all eight are written.

> **GATE 5** — A stranger clones the repo and reproduces the full eval with one command.

---

## Day 11 (5 Sep) — Buffer and submit

Reserved. If Phase 5 slipped, this absorbs it. Otherwise: re-read the submission
requirements, verify every link, submit.

---

## Cut order — decide with this list, not with panic

Cut from the top:

1. **T3.8 P5 Whittle** — ship five arms.
2. **T4.8 Red-team agent** — ship the golden set alone.
3. **Multi-period horizon** — collapse to a single period. Note that this also removes the
   point of P5.
4. **T2.8 Sensitivity grid** — collapse to one nominal point.
5. **T5.6 Streamlit polish** — raw matplotlib PNGs are acceptable.

**Never cut:** real data (Phase 1) · the P2 RoundRobin arm · the refusal ledger · the model
card · `limitations.md`. Those five *are* the score.
