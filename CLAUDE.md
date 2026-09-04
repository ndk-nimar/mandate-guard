# CLAUDE.md — how to work in this repository

MandateGuard: a re-consent allocator for Indian recurring mandates, built for the
Razorpay AI Buildathon 2026 (Track 3). Deadline **2026-09-05**.

Read [`docs/tasks.md`](docs/tasks.md) for what is being built and in what order, and
[`docs/problem.md`](docs/problem.md) for why. This file is only about *how to work here*.

---

## 1. Close every task by writing the journals

**After finishing any task — before starting the next one — update both working
journals.** They are the learning layer, and they are the first thing that gets skipped
when a session runs long, which is exactly why the rule is written down.

**Write them without being asked, and without asking.** Closing a task means the journals
are already written, not that they are ready to be written once someone confirms. Do not
offer the update as a next step, do not ask whether to do it, and do not report a task as
finished with the entry still pending -- the entry is part of finishing it. The only
acceptable reason to defer is that the task itself is not done yet.

| file | what goes in it | shape |
|---|---|---|
| [`docs/worklog.md`](docs/worklog.md) | what happened this session: decisions taken, bugs found, numbers that came out, what is still open, what is next | one `# Session N` heading, then `##` sections |
| [`docs/seekha.md`](docs/seekha.md) | reusable concepts learned, numbered and continuing from the last one | `## N. <lesson>` with **Kya seekha**, and **Kahan phir kaam aayega** |

Both are **Hinglish** and both are **gitignored** — they are personal working notes, not
submission material. That is the one deliberate exception to the disk-is-English rule in
§2. `docs/tasks-samjhao.md` is the third journal but it is a one-time explainer of the
whole plan, not a progress log: leave it alone.

Three things the journals must contain, because they are what makes them worth keeping:

* **The finding that went against us.** `q` moving from 0.35 to 0.41 made every saving
  number smaller. That belongs under its own heading, not in a footnote.
* **The bug that failed silently.** Most of what has gone wrong in this project did not
  crash — the CI sample came out 100% UPI AutoPay and every test stayed green; the
  mandate book returned different counts on identical input. Write those up with the
  mechanism, not just the fix.
* **Where the lesson will bite again.** A lesson with no "next time this shows up in T3.x"
  line is a diary entry, not a lesson.

If a session covers several tasks, one `# Session N` entry covering all of them is fine.
What is not fine is moving to the next task with the journals a task behind.

---

## 2. Language

**Chat is Hinglish. Disk is English.** Code, comments, commit messages, `docs/*.md`,
ADRs — all English. The only exceptions are the three journals named in §1.

Explanations follow the `samjhao` skill: assume low background knowledge, gloss a
technical term the first time it appears, and never state what was done without stating
what it cost and what breaks if it is wrong. Never use "obviously", "simply", "just",
"clearly". Say "mujhe pakka nahi pata, verify karna padega" and then go and check — a
confidently wrong answer is the worst thing to hand over here.

---

## 3. Three rules that already caught real bugs

**Every derived file is byte-identical across runs, on the machine that produced it.**
[ADR 0003](docs/adr/0003-determinism-of-derived-data.md). Tie-breaks are total, ambiguity
resolves *against* this project's own headline, and every `COPY` carries an `ORDER BY`.
Test it by building twice and comparing **bytes**, not counts. Matching counts are not
proof of reproducibility.

The machine clause is measured, not hedging: the same command on two GitHub runners
returned 384,905 and 384,907 for a total this laptop writes as 384,906, and the PNGs change
size on Linux because matplotlib resolves different fonts there. No quoted figure moves.
CI therefore gates on `scripts/check_drift.py` -- named columns, one unit in the last
printed digit, PNG dimensions not bytes -- while `repro --check` stays the exact-byte gate
you run before committing. `docs/limitations.md` §9 has the numbers and their origin.
**When you write a determinism claim, say which machine.**

**Features may only use what was known at the start of the period; labels may use the
future.** The person-period frame ([`mapping.md`](docs/mapping.md) §5.1) exists to enforce
this. Leakage does not crash — it makes cross-validation look excellent and production
useless.

**No number exists without an origin.** [`calibration.md`](docs/calibration.md) allows
exactly four: sourced, measured, `swept: true`, or *named as unverified*. "It was in the
build plan" is not a source. A number that cannot be verified goes in §5 of that document
by name, and does not go in the pitch.

---

## 4. Documentation discipline

Prose is a gate, not a write-up. [`mapping.md`](docs/mapping.md) has its own rule —
**the parquet does not get built until the section arguing for it is written** — and it
has held every time.

Every decision carries its alternative and the cost of being wrong. A table of options
with a chosen row and no rejected rows is not a decision record.

Numbers in docs are **generated, not typed**. Every script prints markdown for exactly
this reason: retyped numbers drift from the data they claim to describe.

---

## 5. Commands

```bash
uv sync                                        # environment
uv run pytest                                  # tests
uv run ruff check . && uv run ruff format .    # lint, format
uv run mypy src                                # types

uv run python scripts/ingest.py                # T1.1  CSVs -> typed parquet
uv run python scripts/analyse_cancel.py        # T1.2  q and the r ceiling
uv run python scripts/build_mandates.py        # T1.3  the mandate book
uv run python scripts/build_periods.py         # T1.4  person-period frame (~89 min)
uv run python scripts/build_sample.py          # T1.5  the committed CI slice
uv run python scripts/score_baseline.py        # T1.6  naive baselines
uv run python scripts/fit_hazard.py --plot     # T1.7/T1.8  hazard + reliability
uv run python scripts/run_ladder.py            # T2.1-T2.6  the arms
uv run python scripts/run_theta.py             # T3.4/T3.5  theta by search; batch vs online
uv run python scripts/compile_policy.py --check   # T4.1  re-verify every rule against the circular
uv run python scripts/build_golden.py          # T4.6  the 120-case golden set
uv run python scripts/make_llm_eval.py         # T4.7  score it -> docs/llm_eval.md
uv run python scripts/make_ledger.py --sample  # T5.1  a run's decision ledger
uv run mandateguard replay --decision-id X     # T5.2  re-run one decision
uv run mandateguard audit --rail enach         # judge one mandate, clauses named
uv run mandateguard repro --check              # T5.8  rebuild every artifact, fail on a byte
uv run python scripts/check_drift.py           # T5.11 what CI gates on instead (limitations.md 9)
uvicorn mandateguard.app.api:app               # T5.6  the page at http://127.0.0.1:8000/
```

`repro --check` is GATE 5 and it is what CI runs: about three minutes **on a plain local
disk**, no download, and it prints the two full-data artifacts it deliberately does **not**
rebuild. It also prints where it is writing derived frames, because that choice is worth
45x: measured 81s against 3,668s for the same step, byte-identical output both times, on a
cloud-synced path. `.env` is gitignored, so a fresh clone does not inherit
`MANDATEGUARD_DATA_DIR` and lands wherever the checkout is.

**`--sample` runs the whole chain on the committed 5,079-subscriber slice in seconds**,
with no download. It swaps a directory in `data/paths.py` and nothing downstream branches
on it. Use it for everything except a final number.

Full data lives outside the repo via `MANDATEGUARD_DATA_DIR` (see `.env.example`) —
`data/` sits inside a OneDrive-synced folder and the raw files are ~4 GB.

---

## 6. Layout

```
src/mandateguard/
  data/      ingest, cancel semantics, mandate book, person-period frame, sample
  risk/      hazard model, baselines, scoring, calibration
  eval/      forward forecast, the week-loop harness
  allocator/ the Policy interface and its arms
  policy/    config and compiled-rule loading
  value/ agent/ ledger/ safety/ app/    later phases
docs/        the argument: problem, mapping, eval, model card, calibration, ADRs
data/sample/ committed, and the only data CI ever reads
```

`config/params.yaml` holds every calibration constant. **Nothing is hard-coded in
Python** — if a number needs to change, it changes there, and `policy/loader.py` validates
the invariants at load time so a bad edit fails immediately rather than three layers down.
