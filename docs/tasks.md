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

- [ ] **T0.10 — Repo `CLAUDE.md`** and `docs/adr/0001-record-architecture-decisions.md`.
  **Done when:** committed.

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

- [ ] **T1.3 — India mapping layer.** `payment_method_id` → rail (UPI AutoPay / card / PPI
  / e-NACH); NTD → INR; `payment_plan_days` → debit frequency; `membership_expire_date` →
  mandate validity. Every mapping decision defended in prose, not just in code.
  **Done when:** `docs/mapping.md` is complete and `data/processed/mandates.parquet` exists.

- [ ] **T1.4 — Person-period expansion.** Convert each subscriber into one row per week
  alive, with `event=1` on the lapse week. This is the shape a discrete-time survival model
  needs.
  **Done when:** a test asserts no subscriber has post-event rows.

- [ ] **T1.5 — Build the CI sample.** Deterministically sample ~5,000 subscribers into
  `data/sample/` (a few MB) and **commit it**. CI cannot download the full dataset; every
  later `results.md` regeneration runs on this sample.
  **Done when:** committed, and the full-data and sample paths share one code path behind a
  `--sample` flag.

- [ ] **T1.6 — Naive baseline.** "Risk equals closeness to expiry." Score it first, so the
  real model has something to beat.
  **Done when:** the baseline Brier score is recorded.

- [ ] **T1.7 — Discrete-time survival model.** `sklearn` logistic regression on the
  person-period frame, per-week hazard. Not Cox: simpler, calibrates better, easier to
  defend on a whiteboard.
  **Done when:** the model fits, predictions are in [0,1], and the seed is fixed.

- [ ] **T1.8 — Calibration, Brier, plots.** `calibration_curve`, `brier_score_loss`,
  reliability diagram committed as a PNG.
  **Done when:** the plot is in `docs/img/` and the numbers are in `docs/eval.md`.

- [ ] **T1.9 — `docs/model_card.md`.** Data, features, performance, limitations, intended
  use. The cleanest AI-maturity signal available, and it is cheap.
  **Done when:** committed.

- [ ] **T1.10 — `docs/calibration.md`.** Every India-layer number mapped to its citation:
  UPI AutoPay 50M new mandates/month, 808M executions/month, 20M+ revocations/month, ~74%
  business decline across the top 50 banks; card post-2021 failure 20%+ in some categories;
  the 2021 migration at ~70% decline and 62.5M mandates over 9 months. **Verify the
  20M/month figure yourself** — you will be asked about it.
  **Done when:** no number anywhere in the repo lacks a source line.

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

- [ ] **T2.1 — Eval harness.** For each week over a 12-week horizon, hand the policy the
  live mandate book and that week's budget, collect decisions, roll the world forward using
  the hazard model plus the intervention-effect parameters.
  **Done when:** the harness runs `NoAskPolicy` end-to-end and returns a metrics dict.

- [ ] **T2.2 — Metrics module.** Mandates retained · revocations caused · INR ARR retained
  · asks spent · INR per ask · shadow price theta (null until P4).
  **Done when:** all six are computed and unit-tested on a hand-built 3-mandate fixture.

- [ ] **T2.3 — P0 `NoAsk`.** The floor. Without it, "how much did we save" means nothing.
  **Done when:** the harness reports it.

- [ ] **T2.4 — P1 `ChronologicalCap`.** First-come until budget B. This is the real
  industry default (Braze, MoEngage, CleverTap).
  **Done when:** the harness reports it.

- [ ] **T2.5 — P2 `RoundRobin`.** Same budget, fair rotation. This is the arm that made
  ARMMAN's result credible. **Never cut this arm.**
  **Done when:** the harness reports it.

- [ ] **T2.6 — P3 `GreedyEV`.** Top-B by expected value. The honest simple baseline — and
  what many "smart" systems actually are.
  **Done when:** the harness reports it.

- [ ] **T2.7 — Budget sweep infrastructure.** Sweep the budget from 0 to 3x optimum and
  produce the inverted-U curve. Zhang's calibration: optimum 7; sending 10 costs −16%
  profit; sending 4 costs −32%. Under-asking is roughly twice as expensive as over-asking,
  so the curve must come out asymmetric.
  **Done when:** the curve plots and the asymmetry is visible.

- [ ] **T2.8 — Sensitivity grid infrastructure.** The (uplift × backfire) plane. Do not
  fill it with a point estimate — sweep it and draw the region where MandateGuard beats
  round-robin.
  **Done when:** the grid runner produces a heatmap on the sample data.

- [ ] **T2.9 — `results.md` auto-generation in CI.** A CI job runs the harness on the
  committed sample with fixed seeds and regenerates `results.md` plus raw artifacts.
  **Done when:** a stranger's fork produces a byte-identical `results.md`.

> **GATE 2** — `results.md` is auto-generated by CI with four arms in it.

---

## Phase 3 · Optimizer — Days 6–7

Goal: the two arms that are actually yours, plus the value function that makes the
prior-art traceability real.

Two structural errors get fixed by this phase's design. A unit-cost "knapsack" is really
just a greedy sort — fixed by giving channels different costs, which makes it a genuine
multiple-choice knapsack. And single-period allocation cannot express "ask later" — fixed
by making the decision variable `(mandate, channel, week)`.

- [ ] **T3.1 — Channel cost table** in `config/params.yaml`: in-app ₹0, email ₹0.05, SMS
  ₹0.15, WhatsApp ₹0.35, IVR ₹2, physical letter ₹25, agent call ₹40 — each with its own
  efficacy prior. Channel variation is also a regulatory requirement (RBI KYC directions
  want at least one physical letter per phase), not decoration.
  **Done when:** loaded and validated.

- [ ] **T3.2 — `value/` — the four-term rupee price.** Each term traces to a different
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

- [ ] **T3.3 — P4 `MCKP` batch solver.** Multiple-Choice Knapsack: per mandate choose **at
  most one** channel, each with its own cost and efficacy. PuLP + CBC. **Depends on T0.3.**
  **Done when:** the solver returns a feasible within-budget allocation on a 100-mandate
  fixture.

- [ ] **T3.4 — `allocator/theta_search.py` — Pinterest's theta.** Hill-climb plus binary
  search on the shadow price so total asks match the global budget. This is the *algorithm*
  for producing theta, not just the name of it.
  **Done when:** theta converges and total asks land within ±2% of budget.

- [ ] **T3.5 — `allocator/serving_rule.py` — LinkedIn's online rule.** The LP-dual per-item
  threshold test `mu·P(re-consent) − nu·P(revoke) − cost > 0`, so mandates can be decided
  one at a time without re-solving the knapsack. **The batch-vs-online comparison is itself
  a result** — report both.
  **Done when:** the online rule reproduces batch MCKP within a stated tolerance.

- [ ] **T3.6 — Sanity check the shape.** LinkedIn's published shape: volume −64.5%,
  sessions only −1.8%, complaints −47%. Your result should have the same shape — far fewer
  asks, slightly less retention. If it does not, something is wrong.
  **Done when:** the three deltas are printed and compared in `docs/eval.md`.

- [ ] **T3.7 — Segment plot (Pinterest's testable prediction).** Pinterest found an
  inverted-U: the most active *and* the most dormant users got the fewest messages. Your
  allocator should give the healthiest and the most doomed mandates the fewest asks. If it
  does, that is independent validation. If it does not, explain it. Either way the plot
  ships.
  **Done when:** the plot is in `docs/eval.md`.

- [ ] **T3.8 — [CUT #1] P5 `WhittleIndex`.** Multi-period restless bandit over
  `(mandate, channel, week)`, 12-week horizon, per-week budgets, binary search on the
  subsidy lambda. If Day 7 ends without it working, ship five arms and say so.
  **Done when:** the six-arm chart renders, or the arm is formally dropped in
  `docs/eval.md`.

- [ ] **T3.9 — `eval/holdout.py` — Meta's identification design.** A 50% random-drop
  holdout matching `P(active|do(send)) − P(active|do(drop))`. You cannot run this in
  production, but building the harness in that shape is the serious answer to "how would
  you validate this?"
  **Done when:** the harness supports a random-drop arm.

- [ ] **T3.10 — `docs/limitations.md`, written now, not at the end.** Include the Adyen
  sanity check in your own words: Adyen's contextual bandit beats a fixed retry schedule by
  ~6%, the most trustworthy public number in payments. If your simulator claims 40%, that
  is a red flag, not a result. Doubting your own number is the highest-credibility move
  available.
  **Done when:** committed, and it names the three things needed before production — real
  India mandate data, an intervention holdout, and a shadow-mode merchant pilot.

> **GATE 3** — The six-arm chart exists (or five, honestly labelled) and theta prints as a
> real rupee number.

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
