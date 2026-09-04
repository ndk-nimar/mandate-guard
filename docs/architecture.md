# Architecture

Status: written 2026-09-03, T5.7.

What this document is for: a reader who has seen the results and now wants to know whether
the code that produced them is arranged in a way that could be trusted with money. It is
about **boundaries** — which layer is allowed to decide what, and what stops each one
reaching past its edge. The arguments for the *decisions* live in
[`problem.md`](./problem.md) and [`eval.md`](./eval.md); the arguments for the *numbers*
live in [`calibration.md`](./calibration.md). This one is about the shape.

Related: [`stack.md`](./stack.md) — what was chosen and what was rejected ·
[`adr/`](./adr/) — the five decisions that were expensive enough to record ·
[`mapping.md`](./mapping.md) — how KKBox becomes an Indian mandate book.

---

## 1. What the system does, in one line

A merchant's mandate book, a weekly attention budget, and one question per mandate per
week: **ask this customer to re-consent, on which channel, or leave them alone?**

```
data/sample/*.parquet
        |
        v
   data/       mandate book, person-period frame        "what exists, and when"
        |
        v
   risk/       discrete-time hazard model               "what is p(lapse) this week"
        |
        v
   value/      four rupee terms per (mandate, channel)  "what is this ask worth"
        |
        v
  allocator/   MCKP under a budget -> theta             "which asks fit the budget"
        |
        +--> policy/   compiled RBI rules               "is this ask legal"
        |
        +--> agent/    LLM: compile, audit, compose, explain
        |
        v
   safety/     Guard.authorise                          "may this actually happen"
        |
        v
   ledger/     append-only, hash-chained JSONL          "what did we do, and why"
```

`eval/` sits beside all of it rather than under it: the week-loop harness, the forward
forecast, the sweeps, the holdout, the shape check, and `repro`. `app/` is the HTTP surface
and `cli.py` the terminal one. Neither contains a decision.

---

## 2. The five boundaries that carry weight

A layer diagram is decoration unless something enforces it. These five are enforced, each
by a specific mechanism, and each one is written here with the alternative it rejected and
what breaks if the choice was wrong.

### 2.1 The model never decides. It compiles, audits, composes and explains

`agent/` is the only package that talks to an LLM, and none of its four jobs returns an
allocation. It compiles the RBI circular into `policy/mandate_policy.yaml` (rules that are
then re-verified against the source text by `scripts/compile_policy.py --check`, with no
model in the loop), audits a mandate, composes a notice, and explains a refusal in rupees.
Every one of those runs to completion with `client=None`.

**Rejected:** letting the model rank or select mandates — the obvious "AI agent" shape for
a hackathon in this track.

**Why:** a selection made by a model cannot be replayed, cannot be swept, and cannot be
audited against a clause. The headline number (`results.md` §2) has to survive a reviewer
asking *"re-run it and show me the same answer"*, and a temperature-0 API call is not that
guarantee — the account, the model version and the vendor's serving stack are all outside
this repository.

**Cost of being wrong:** the system is less impressive in a demo, and a genuinely better
ranker is left on the table. `docs/llm_eval.md` scores the rules arm at 114/114 on the
golden set, so on *this* set nothing was left on the table — but see `limitations.md` §8.7,
where the same person wrote the cases and the rules.

**What enforces it:** `agent/` has no import of `allocator/`. `tests/test_chaos.py` kills
the client and asserts every job still answers.

### 2.2 `Guard.authorise` is the only path to acting

`safety/guard.py`, T5.3. Shadow mode is the default in `config/params.yaml`; the kill
switch, the spend cap, the rate limiter and the four-rung degradation ladder all live
behind one call. `/allocate` asks it per contact and returns `acted: false` as a **field**,
not as a line of documentation.

**Rejected:** checking the cap where sends happen.

**Why:** a limit checked at three call sites is a limit missing from the fourth. That is
lesson-shaped rather than theoretical here — the HTTP layer was the fourth call site, and
it was written after the guard.

**Cost of being wrong:** if `authorise` is bypassed by any future caller, every safety
property in `limitations.md` §8.10 becomes a claim about intent rather than about code.

**What enforces it:** there is no `send()` in the repository that does not go through it;
`tests/test_safety.py` and `tests/test_chaos.py` cover each rung of the ladder separately.

### 2.3 Every constant lives in `config/params.yaml`, validated at load

`policy/loader.py` reads it once and checks the invariants there — `q > r` is refused at
load rather than three layers down, and the same validator runs on the HTTP models, so a
422 comes back instead of a nonsense allocation.

**Rejected:** module-level constants next to the code that uses them.

**Why:** [`calibration.md`](./calibration.md) can only be a complete register of the
project's numbers if there is exactly one place for a number to be. A constant defined in
Python is a constant that never appears in that document, and the failure is silent.

**Cost of being wrong:** a swept parameter that is secretly hard-coded makes
`results.md` §4's sensitivity plane a picture of one point.

### 2.4 `data/paths.py` is the only module that knows where data lives

`--sample` swaps one directory and nothing downstream branches on it, because
`data/sample/` holds the same file names as `data/interim/`. Derived frames go to a
gitignored `processed/sample/` subdirectory rather than overwriting the full ones.

**Rejected:** a separate sample code path, and committing the derived frames.

**Why:** a second code path is free to drift, and the entire point of the committed slice
is that CI exercises the code the full run exercises. A committed derived frame goes stale
the first time the code changes and nothing notices.

**Cost of being wrong:** a 5,079-subscriber Brier score quoted as a 1.4M-mandate one. The
separate output directory exists because that mistake has no other guard rail.

### 2.5 Every derived file is byte-identical across runs

[ADR 0003](./adr/0003-determinism-of-derived-data.md). Total tie-breaks, `ORDER BY` on
every `COPY`, matplotlib's version stamp stripped from every PNG, and ambiguity resolved
*against* this project's own headline.

**Rejected:** comparing row counts.

**Why:** matching counts are not proof of reproducibility. Two runs of the mandate book
once returned the same count and different rows.

**Cost of being wrong:** GATE 5 is unenforceable and every generated document becomes a
snapshot of a machine rather than of the data.

**What enforces it:** `uv run mandateguard repro --check` (T5.8) rebuilds
`results.md`, `sweeps.png`, `llm_eval.md` and `segments.png` from `data/sample/` and fails
on one differing byte. CI runs that exact command on `windows-latest`, the platform the
committed files were produced on.

**And the scope of that sentence is narrower than it was until 2026-09-04.** CI ran for the
first time that day -- this repository had no remote before it, so `ci.yml` had never
executed -- and the same command failed on `ubuntu-latest`, then failed again on
`windows-latest`. The drift was arithmetic, not line endings: 384,906 came back as 384,907
on one runner and 384,905 on the other, because floating-point addition is not associative
and the accumulation order belongs to the hardware and its math library rather than to this
code. Both PNGs also changed size on Linux, where matplotlib resolves different fonts. Every
figure this project quotes was unchanged on all three machines.

Byte-identity is therefore a **same-machine** guarantee, not a same-platform one. What CI
gates on instead is `scripts/check_drift.py`, on both operating systems and both blocking:
prose byte-identical, table cells exact outside the columns where drift was measured, one
unit in the last printed digit allowed there, PNG dimensions rather than PNG bytes.
`tests/test_drift_check.py` pins that allowance from both sides.
[`limitations.md` §9](./limitations.md) is the full account, and names the fix that would
remove the drift rather than tolerate it.

**What it does not enforce is tractability, and that turned out to matter.** The same
command on the same tree took **2m51s** with derived frames on a plain drive and **64m01s**
with them on a cloud-synced path — byte-identical both ways, so nothing failed and nothing
warned. `.env` is gitignored, so a fresh clone inherits no `MANDATEGUARD_DATA_DIR` and
writes wherever the checkout happens to be. The report therefore names its data directory
on its first line. Determinism and tractability are separate properties and only one of
them was being checked.

---

## 3. One request, end to end

`POST /allocate` is the shortest path that touches every layer:

1. **`policy/loader.py`** — params and the compiled rulebook, loaded at import. Per-request
   loading would turn a policy check into a latency budget and still only prove the file
   had not changed since the previous request.
2. **`eval/world.py`** — the book for this week, with each mandate's state.
3. **`risk/hazard.py`** — `p(lapse)` per mandate-week, from the fitted model.
4. **`value/price.py`** — four rupee terms per `(mandate, channel)` candidate.
5. **`allocator/mckp.py`** — multiple-choice knapsack under the budget: at most one channel
   per mandate. The LP relaxation's dual on the budget constraint is theta, the shadow
   price ([ADR 0002](./adr/0002-solver-and-shadow-price.md)).
6. **`safety/guard.py`** — `authorise` per contact. Shadow mode means the response says
   what it *would* do.
7. **`ledger/store.py`** — every decision written, **asked and not-asked**, with its
   reason, its rupee number, the policy hash, the model version, the seed and the template
   id. `/ledger` re-verifies the hash chain on every read rather than caching a verdict; a
   tampered chain is a **409**, with the row number.

A `Guard` is constructed **per request**, not per process, so the day's first caller cannot
exhaust everyone's spend cap. The cost of that choice is real and recorded: the cap bounds
an allocation, not the service (`limitations.md` §8.10).

---

## 4. What is deliberately not here

* **No database.** Parquet on disk and an append-only JSONL ledger. The unit of work is a
  weekly batch over a book that fits in memory; a database would be a dependency with no
  question to answer. `stack.md` carries the rejected alternative.
* **No queue, no scheduler.** The week loop is a loop.
* **No framework on the surface.** T5.6 was cut on 2026-09-03 and **un-cut on
  2026-09-04** once two measurements showed a live dial was affordable: five arms over a
  12-week horizon solve in 1.67s, against the 92s a whole `make_results` run takes. The
  page at `/` is one hand-written HTML file with inline SVG -- no build step, no bundler,
  no CDN, because a demo must not need the network. Streamlit was the original choice and
  is now the rejected one (`stack.md`): its `config.toml` exposes five colours and one font
  family, so a distinct visual identity needs CSS injection that breaks on upgrade.
  `app/ui.py` survives as the T0.5 two-process spike, which is what keeps the API boundary
  exercised from a second client.
* **The page cannot write.** GET-only by construction -- `/ladder`, `/refusal`, `/runs` and
  `/ledger` exist so that nothing on it needs a write request, and a test asserts the file
  contains no form element and none of the vocabulary of contacting anyone. What this
  costs: the sensitivity heatmap is still a static PNG, and the PNGs do not yet share the
  page's palette.
* **No authentication on the API.** It is a demonstration surface running on localhost, and
  saying so is better than a login form that implies more.
* **One process for the real surface.** The page is served by the same uvicorn that serves
  the API, so a demo needs one command. `scripts/dev.py` still starts the Streamlit spike
  alongside it, which was spike S3's whole question.

---

## 5. Testing shape

729 tests over 34 files, and the two that matter structurally:

* **The chaos suite runs as its own CI step.** "The system degraded" is a different claim
  from "the tests passed" and deserves its own green tick.
* **Two CI jobs, not one.** `check` is lint, types, the compiled-policy re-verification,
  the golden set, the suite and chaos. `results` is GATE 2 and GATE 5: run the one
  documented command and fail on any byte that moved.

Everything a document asserts about a number is either generated by a script that CI reruns
or listed in `calibration.md` §5 as unverified, by name. That rule is what
[`prior_art.md`](./prior_art.md) was written to satisfy, and writing it moved two published
figures the project had been quoting for four phases.
