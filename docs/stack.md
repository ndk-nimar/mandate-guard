# Technology Stack — MandateGuard

Status: **decided, 3 assumptions unverified** (see "Day-1 spikes" below)
Last updated: 2026-08-26

This document exists so that no technology choice in this repository has to be defended
with "it felt right". Every row below names what was chosen, why, what was rejected, and
what breaks if the choice turns out to be wrong.

---

## Constraints that drove every choice

1. **10 days, one developer.** Anything that needs a second language, a server to
   administer, or infrastructure to provision is disqualified regardless of technical
   merit.
2. **A judge must be able to reproduce it.** One clone, one command, no API key required
   for the core evaluation, no multi-gigabyte download.
3. **Laptop-scale data.** The KKBox `transactions.csv` is roughly 21 million rows and must
   be processable without exhausting memory.
4. **Every number must be defensible.** Where a tool hides its reasoning — a black-box
   solver, an uncalibrated model — it loses to a tool that exposes it. The shadow price
   theta is the clearest example: it only exists if the solver hands back LP duals.

---

## Layer-by-layer decisions

| Layer | Choice | Why | Rejected | What breaks if wrong |
|---|---|---|---|---|
| Language | **Python 3.12** | Data, model, solver, LLM, API and UI all in one language | Python + TypeScript split | ~2 of the 10 days lost to frontend plumbing |
| Packaging | **uv + `pyproject.toml` + `uv.lock`** | Already installed; the lockfile is what lets a judge resolve identical versions | pip + `requirements.txt` (no lock), poetry (slower), conda (heavy) | A judge's clone resolves different versions and `results.md` differs |
| Layout | **`src/` layout, package `mandateguard`** | Prevents accidental imports from the working directory; CI tests the installed package | flat layout | Tests pass locally and fail in CI |
| Bulk ingest | **DuckDB** (SQL over CSV, written out as Parquet) | 21M rows stream through DuckDB without being loaded into memory; one dependency, no server process | `pandas.read_csv` (2–3 GB RAM, real OOM risk on Day 2), polars (fine, but a smaller answer surface when stuck) | An out-of-memory crash on Day 2 — the worst possible day to lose |
| Analysis frames | **pandas + numpy** | Everything downstream (scikit-learn, matplotlib) speaks pandas natively | polars | Constant conversion friction |
| On-disk format | **Parquet (pyarrow)** | Columnar and typed; roughly 10x smaller than CSV | CSV | Slow reloads and dtype drift between runs |
| Hazard model | **`sklearn.linear_model.LogisticRegression` on person-period data** | Discrete-time survival *is* a per-period logistic regression. Calibrates well and can be explained on a whiteboard | lifelines CoxPH (continuous-time, harder to calibrate), XGBoost (better AUC, worse calibration — and calibration is literally Gate 1) | Gate 1 fails on Brier score |
| Calibration | **`calibration_curve` + `brier_score_loss`** | Both ship with scikit-learn; the reliability diagram is the Gate-1 artefact | hand-rolled binning | — |
| **MCKP + LP dual** | **PuLP + CBC** | Solves the integer program *and* exposes LP-relaxation duals via `constraint.pi`. Theta comes from that dual | OR-Tools CP-SAT (faster, but no clean LP duals — and theta is the headline number), `scipy.linprog` HiGHS (gives duals but cannot do the ILP) | **Theta becomes unobtainable, and the project loses its most executive-legible number.** Verified by spike S1 |
| MCKP fallback | `scipy.optimize.linprog` (HiGHS) for duals + a hand-rolled greedy-by-incremental-efficiency for the integer solution | Adopted only if S1 fails | — | Costs roughly half a day |
| Whittle index | **Hand-written numpy**, binary search on the subsidy lambda | No library exists for 2-state restless bandits; this is around 150 lines | — | It is cut-line #1 anyway |
| Domain models | **Pydantic v2** | One schema definition serves the domain layer, FastAPI, *and* the LLM structured outputs | dataclasses, attrs | Three parallel schema definitions that drift apart |
| Config | **YAML + pydantic-settings** | Nothing calibration-related may be hard-coded. `mandate_policy.yaml` is also the LLM compile target, so it has to be a plain diffable file | `.env` only, TOML | The policy cannot be versioned or diffed, and the "replay old decisions under old policy" story collapses |
| LLM SDK | **`anthropic` Python SDK** | Official SDK: typed exceptions, Batch API, structured outputs | raw `httpx` calls | Hand-maintained request shapes that drift from the API |
| LLM model | **`claude-opus-5`** with `thinking={"type": "adaptive"}` | Best judgment for regulatory reasoning. Adaptive thinking is the current API — `budget_tokens` is rejected with a 400 on this model | `claude-sonnet-5` (cheaper, weaker on regulatory nuance) | Wrong verdicts from the auditor; a stale `budget_tokens` parameter returns a 400 |
| LLM structured output | **`output_config={"format": ...}` / `messages.parse()`, plus `strict: true` on tool definitions** | Verdicts are validated at the API boundary and the model retries on mismatch | regex-parsing free text | Parse failures during the live demo |
| LLM eval cost | **Batch API** for the ~120-case golden set | 50% cost, and CI is not latency-sensitive | live calls on every push | Meaningful spend across ~50 pushes |
| Notice validation | **Plain Python linter + pytest** | Regulated text needs a *deterministic* gate. An LLM checking an LLM is not a gate | LLM-as-judge only | Unvalidated model text reaching a regulated notice — the single worst failure mode in this project |
| API | **FastAPI + uvicorn** | Reuses the Pydantic models directly; OpenAPI docs come free | Flask (no typing), Django (far too heavy) | — |
| UI | **One hand-written HTML page served by FastAPI** (revised 2026-09-04) | Total control over colour and typography, no build step, no second process, and the page can only reach the API over HTTP so the boundary stays real | **Streamlit** -- chosen originally and rejected on delivery: `config.toml` exposes five colours and one font family, so a distinct visual identity needs CSS-injection hacks that break on upgrade. Also Next.js (~2 days), Gradio (ML-demo shaped), Dash (boilerplate) | The video loses its most memorable moment. Cost of the revision: ~3 hours against Streamlit's ~20 minutes |
| Charts | **matplotlib** for CI-committed PNGs; **hand-rolled inline SVG** for the live page | CI has to emit static artefacts the repo commits; the page needs interactivity **and must not need the network at demo time**, which rules out a CDN | plotly everywhere (heavier CI), any charting library over a CDN (a demo that fails offline) | The two do not share a palette yet -- `docs/tasks.md` T5.6 records that as deferred |
| Ledger | **Append-only JSONL** | Greppable, diffable, auditable by hand, and append-only is provable in a test | SQLite (mutable), Postgres (infrastructure) | The "auditable non-action" claim gets weaker |
| CLI | **typer** | Shares the Pydantic types; `replay` and `repro` both need a real CLI | argparse, click | — |
| Tests | **pytest + pytest-cov** | Chaos tests are built on `monkeypatch` | unittest | — |
| Lint / format | **ruff** | One tool replaces black + flake8 + isort, and one CI step instead of three | the three-tool stack | Slower CI and more configuration to maintain |
| Types | **mypy on `value/` and `allocator/` only** | Those two packages carry the rupee math. Full-repo mypy is a time sink at this deadline | full mypy, or no mypy at all | A silent unit error in the money calculations |
| CI | **GitHub Actions**, one ubuntu job | `uv sync`, then ruff, pytest, and regeneration of `results.md` | no CI, or a matrix build | The reproducibility claim has nothing backing it |
| CI data | **A committed ~5,000-subscriber sample (a few MB)** | CI cannot fetch the full dataset. Full data and sample share one code path behind a `--sample` flag | full download in CI, or no evaluation in CI | Gate 2 and Gate 5 both fail |
| Reproducibility | **Fixed seeds + `uv run mandateguard repro`** | Gate 5 is literally this one command | — | Gate 5 fails |
| Secrets | **`.env` + python-dotenv**, `ANTHROPIC_API_KEY` | Standard, and `.env` is gitignored | committing keys | Leaked key |
| Hosting | **Public GitHub repo, created with `gh`** | Judges have to be able to clone it | private repo | Not judgeable |

---

## Day-1 spikes: the three assumptions that must be verified before building on them

| # | Assumption | Verified by | If it fails |
|---|---|---|---|
| S1 | PuLP/CBC returns LP duals on Windows | `tests/spikes/test_lp_dual.py` — solve a 5-item MCKP LP relaxation, read the budget constraint's `.pi`, assert it is a finite non-zero float | Switch to scipy HiGHS for the dual plus a hand-rolled greedy for the integer solution, and amend this document |
| S2 | Kaggle grants access to the KKBox WSDM 2018 download | Accept the competition rules, start the `transactions.csv` download | Fall back to the Telco Customer Churn dataset. It has no `is_auto_renew` column, which materially weakens the Tier-1 claim — `docs/limitations.md` must say so plainly |
| S3 | uvicorn and Streamlit start together from one command on Windows | `scripts/dev.py` starts both; Streamlit renders one number fetched from FastAPI over httpx | Ship Streamlit-only and drop the API-boundary claim from the pitch |

**Honest note on S1.** This is a genuine unknown, not a formality. PuLP exposes
`constraint.pi` for LP solves, but that behaviour has not been confirmed on this machine
with the bundled CBC binary. That is exactly why it is the first task on Day 1 rather than
a discovery on Day 6, when the shadow price is due.

---

## Dependencies

**Runtime**

`duckdb` · `pandas` · `numpy` · `pyarrow` · `scikit-learn` · `matplotlib` · `pulp` ·
`scipy` · `pydantic` · `pydantic-settings` · `pyyaml` · `anthropic` · `fastapi` ·
`uvicorn` · `httpx` · `streamlit` · `typer` · `python-dotenv`

**Development**

`pytest` · `pytest-cov` · `ruff` · `mypy` · `kaggle`

---

## Directory layout

```
mandate-guard/
├── src/mandateguard/
│   ├── data/         # L1  KKBox ingestion + India mapping layer
│   ├── risk/         # L2  discrete-time survival hazard model
│   ├── value/        # L3  rupee pricing: prices, reachability, ltv, fatigue, channel_priors
│   ├── allocator/    # L4  Policy ABC, 6 implementations, serving_rule, theta_search
│   ├── policy/       # L5  YAML loader + policy hashing
│   ├── agent/        # L6  4 bounded Claude jobs + the deterministic compliance linter
│   ├── ledger/       # L7  append-only JSONL + replay
│   ├── safety/       # L8  shadow mode, kill switch, spend cap, degradation ladder
│   ├── app/          # L9  FastAPI service + static/ page + Streamlit spike
│   └── eval/         #     harness, arms, holdout, sweeps, metrics
├── config/params.yaml
├── policy/mandate_policy.yaml
├── data/{raw,interim,processed,sample}/   # only sample/ is committed
├── docs/                                  # 9 docs + adr/ + img/
├── tests/                                 # incl. golden/ and spikes/
├── scripts/dev.py
└── .github/workflows/ci.yml
```

---

## What is deliberately *not* in this stack

- **No database server.** Parquet files and JSONL. A Postgres dependency would make the
  one-command reproduction claim false.
- **No orchestration framework** (Airflow, Prefect, Dagster). The pipeline is a typer CLI
  with fixed seeds. Anything more is infrastructure a judge would have to install.
- **No LLM framework** (LangChain, LlamaIndex). Four bounded jobs against one SDK. A
  framework here would obscure exactly the part that has to be legible: what the model was
  asked, what it returned, and what validated it.
- **No experiment tracker** (MLflow, W&B). `results.md` is regenerated by CI from fixed
  seeds and committed to git. Git *is* the tracker, and it is the one the judge already has
  open.
- **No Docker.** `uv sync` is the environment. Docker would add a build step between the
  judge and the result.
