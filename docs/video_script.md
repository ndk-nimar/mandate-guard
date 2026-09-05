# Video script — 5:00, word for word

Status: rewritten 2026-09-04. This is a **delivery script**, not a beat sheet: every line
between the `>` marks is said aloud as written, and the timings are measured against a
150-words-per-minute read with the terminal running underneath.

The rule for the whole thing: **numbers, not adjectives.** Nothing here says "powerful",
"robust" or "intelligent". Every claim on screen is either a generated document or a
command running live.

Three things go against the grain of a normal pitch and all three are deliberate:

* **The chaos test runs live at 3:50.** A recorded green tick proves nothing a screenshot
  could not fake. Killing the model on camera and watching the system keep answering is
  the only version of that claim worth making.
* **The correction is volunteered at 1:00.** This project quoted two published figures
  wrongly for four phases and found it by opening the papers. Saying so early sets the
  standard every number after it is read against.
* **The limitations are said out loud, unprompted, at 4:35.** A judge who finds a weakness
  the pitch hid discounts everything else in it. A judge who is handed it does not.

---

## The four judged dimensions, and where each one is earned

The buildathon names four. A five-minute video that leaves one unaddressed is scored on
three.

| dimension | the beat that earns it | the artefact on screen |
|---|---|---|
| **Problem Taste** | 0:00–0:55 | `problem.md` §2, the three gaps |
| **Build Quality** | 0:55–2:55 | the hazard model, the ladder, theta, the page |
| **AI Judgment** | 2:55–3:20 | `llm_eval.md`, and ADR 0004: the model does not decide |
| **Failure Recovery** | 3:20–4:20 | the refusal ledger, the replay that refuses, chaos |

---

## Beat sheet

| time | length | on screen | what it earns |
|---|---|---|---|
| 0:00 | 0:35 | `problem.md` §2 | the problem, in dated numbers |
| 0:35 | 0:20 | three-line list | why nothing existing solves it |
| 0:55 | 0:25 | `prior_art.md` §2 | the correction, volunteered |
| 1:20 | 0:35 | `eval.md` §2 + `reliability.png` | the model, and where it is wrong |
| 1:55 | 0:30 | terminal | policy + audit, live |
| 2:25 | 0:30 | terminal + browser | allocate, the dial, theta |
| 2:55 | 0:25 | `llm_eval.md` | AI judgment, and its own caveat |
| 3:20 | 0:30 | terminal | the refusal ledger and a replay that refuses |
| 3:50 | 0:30 | terminal | **live chaos test** |
| 4:20 | 0:15 | one slide | the Subscription Recovery collision |
| 4:35 | 0:25 | `limitations.md` §7 | what this has not earned |

**5:00 exactly.** Narration is ~690 words; the rest is terminal time.

---

## 0:00 — The problem, in dated numbers  *(0:35)*

**Show:** title card for three seconds, then `docs/problem.md` §2, scrolled to the 2021
figures.

> India's recurring payments run on e-mandates — a standing authorisation to debit
> someone on a schedule. On the 21st of April 2026 the RBI replaced eight circulars with
> one framework. Clause 1(b): effective immediately. Clause 11 repeals the old ones with
> no savings provision. There is no transition period.
>
> The last time this book had to move — the 2021 card migration — sixty-two and a half
> million mandates took about eight months, and recurring transactions declined about
> seventy percent at the peak.
>
> As of July 2025, UPI AutoPay ran about 808 million executions a month, and more than
> twenty million revocations a month.
>
> So a merchant has a book that needs re-consent, and a limited number of times they can
> ask before the asking is the problem.

**Do not say:** "20%+ card failure post-2021". That number could not be sourced and is
struck — `calibration.md` §5.

---

## 0:35 — Why nothing existing solves it  *(0:20)*

**Show:** three lines appearing one at a time. No terminal.

> Three gaps.
>
> One. **Everyone caps, nobody chooses.** Braze, MoEngage, CleverTap, Klaviyo — all of
> them cap and drop in arrival order. Arrival order decides what gets dropped, not value.
>
> Two. **Nobody prices a message in rupees.** Klaviyo holds churn risk and lifetime value
> on the same profile and wires neither into the cap.
>
> Three. **There is no controlled evidence in payments** for how many messages cause a
> cancellation. The entire fatigue narrative is vendor content.

---

## 0:55 — The correction, volunteered  *(0:25)*

**Show:** `docs/prior_art.md` §2, on the quoted abstract.

> The public evidence that irritation is real and durable is Chrome's quiet permission
> prompt — USENIX Security 2021, forty million users, a hundred million prompts. Less
> than five percent of grants lost, up to thirty percent fewer unnecessary actions.
>
> That paragraph is on screen because this project quoted it **wrongly** for four phases —
> as "seventeen to thirty-one percent of permanent refusals across three hundred million
> users". The paper says neither. Somebody opened it on the 2nd of September, and it is
> corrected in five files.
>
> The correction made our own case weaker. Every number after this one is read against
> that standard.

---

## 1:20 — The model, and where it is wrong  *(0:35)*

**Show:** `docs/eval.md` §2.4 table, then `docs/img/reliability.png`.

> The lapse hazard is fit on real subscriber data — twenty-one million transactions,
> fifty-eight million person-weeks, a discrete-time survival model on person-weeks.
>
> Held out of time, it scores a Brier of 0.0068 against the base rate's 0.0073 — the only
> model here with positive Brier skill. Accuracy is not reported anywhere, because at a
> point-seven-percent base rate, "this mandate survives" is 99.3% accurate and worth
> nothing.
>
> And it is wrong in a specific direction. It is **over-confident exactly where the money
> is spent** — the top-risk bucket's hazard is overstated by about sixty percent, which
> biases the optimiser toward asking. That is the wrong direction for a project whose
> whole claim is that over-asking is expensive. It is in the model card, not a footnote.

---

## 1:55 — Policy and audit, live  *(0:30)*

Terminal. One command at a time. Nothing pre-run.

```bash
uv run mandateguard policy
```

> Twenty rules compiled from the circular. Each carries a clause number and a verbatim
> quote, and the hash on screen is re-verified against the source text on every load.

```bash
uv run mandateguard audit --rail enach --amount 20000
```

> A twenty-thousand-rupee eNACH debit. It names the clause it fails and the remedy.
>
> And notice what it says about scope: **eNACH is not inside clause 2 at all** — fifteen
> percent of the modelled book sits outside the regulation this project calls its
> "why now". Reading the circular cost us that.

---

## 2:25 — Allocate, the dial, and theta  *(0:30)*

Terminal in one pane, `http://127.0.0.1:8000/` already open in the other.

```bash
curl -s localhost:8000/allocate -d '{"budget_inr": 500}' | jq
```

> One thousand three hundred and fifty-four live mandates, a weekly budget, and the
> allocator asks a hundred and nine of them. `acted: false` is a **field** in that
> response, not a line in the documentation — shadow mode is the default.

**Switch to the browser. Drag the budget dial.**

> The dial is theta — the shadow price on the budget, the LP dual of the knapsack, not a
> heuristic. It means: give me one more rupee of attention budget and the objective rises
> by theta. At a budget of sixty-eight paise it is four-and-a-half. At nineteen rupees it
> is **zero** — the system saying *stop giving me money, I have asked everyone worth
> asking.*
>
> Two independent algorithms compute it — the solver's dual, and a bisection that uses no
> solver at all. They agree to within zero point zero zero percent.

---

## 2:55 — AI judgment, and its own caveat  *(0:25)*

**Show:** `docs/llm_eval.md` §2 and §6.

> The model does four jobs — compile the circular, audit a mandate, compose a notice,
> explain a refusal — and **none of them is the decision.** Selection is a knapsack,
> deliberately, because the headline has to replay and a hosted model's ranking cannot.
>
> A hundred and twenty golden cases. On the rules arm: a hundred and fourteen out of a
> hundred and fourteen, on verdict **and** citations — a right verdict resting on the
> wrong clause is a finding nobody can act on. Abstain precision and recall both a hundred
> percent. Fifty-five of those cases were written adversarially.
>
> **And section 6 says what that is worth.** The same reader wrote the rules and the
> expectations, so a misreading would score a hundred percent either way. It is a
> regression suite with citations. It is not external validation, and the document says so.

---

## 3:20 — The refusal ledger, and a replay that refuses  *(0:30)*

```bash
uv run mandateguard verify-ledger data/ledger/<run>.jsonl
uv run mandateguard replay --decision-id <id>
```

> Every decision is logged — **asked and not-asked**. The not-asked ones are the point:
> each one carries the rupee reason it was not worth making. A hundred and nine asks, and
> a record of the twelve hundred refusals.
>
> The ledger is a hash chain, and it is re-walked on every read — a tampered row is a 409
> with the row number, not a cached verdict.
>
> Any decision replays: same policy hash, same model version, same seed, same snapshot,
> byte-identical. And if the policy file has changed since, the replay **refuses** rather
> than producing a new answer that looks like an old one.

**Do not speed this up.** `replay` takes about six seconds because it re-runs the whole
allocation. That slowness is what makes it a replay rather than a new decision.

---

## 3:50 — Live chaos test  *(0:30)*

Run it on camera. Do not cut.

```bash
uv run pytest tests/test_chaos.py -q
```

> Kill the model. Corrupt the policy file. Feed it nulls.
>
> It degrades along four ordered rungs — model down goes to rules-only, a stale model goes
> to the conservative floor, a policy-hash mismatch halts. Worst rung wins.
>
> This works for one reason: **the deterministic path is the ordinary path here, not the
> emergency one.** The model never decides anything, so losing it costs explanations, not
> decisions.
>
> Six hundred and seventy-three tests. Chaos runs as its own CI step, because "the system
> degraded" is a different claim from "the tests passed".

---

## 4:20 — The collision  *(0:15)*

The only slide in the video. Say it before a judge does.

> Razorpay already ships Subscription Recovery. It retries **after a payment fails**. This
> runs **before a mandate lapses**. Same book, different point in the lifecycle — and this
> one sits on the April 2026 compliance surface, which no retention agent currently models.

---

## 4:35 — What this has not earned  *(0:25)*

**Show:** `docs/limitations.md` §7 — the table of sentences this project will not say.

> Four things, and they are in `limitations.md`, written during Phase 3 rather than on the
> last day.
>
> The data is Taiwanese music streaming. The rail mix is assigned from a hash.
>
> The headline turns on two constants nobody has measured — and across their swept range
> its sign changes.
>
> We fail the one external check available. LinkedIn cut a third of their sends and lost
> engagement; we cut ninety-nine percent and **gain** it. That is the wrong direction, it
> is in `eval.md` §6 under its own heading, and no backfire rate reproduces their shape.
>
> Against the campaign default, this retains seven-point-four percent more. Against doing
> nothing — which is what most of these mandates already get — it is **plus nought point
> nought five percent**, worth about two hundred rupees on this book. Small, and ours.

**Close on:** the terminal, running:

```bash
uv run mandateguard repro --check
```

> One command. Three minutes. Every generated document rebuilt and compared **byte by
> byte** — on a different operating system from the one they were committed on.

Let it end on the word *byte-identical* on screen. No outro card, no music sting.

---

## Pre-flight checklist

Run this list once, on camera-day, before recording anything.

- [ ] `MANDATEGUARD_DATA_DIR` points at a **plain local path**, not the OneDrive checkout.
      81s against 3,668s, byte-identical output either way. Check the first line of the
      repro output on screen.
- [ ] The API is **already running** in a second pane. Booting it on camera costs fifteen
      seconds and proves nothing.
- [ ] A ledger file exists and its `<run>` and `<decision-id>` are **pasted into the
      script above** — fumbling a UUID on camera costs ten seconds of the four minutes
      that are not slack.
- [ ] `uv run pytest tests/test_chaos.py -q` has been run once already today. Live means
      unedited, not unrehearsed.
- [ ] Terminal font at 16pt or larger. A judge watching at 720p cannot read 11pt.
- [ ] Screen recording at 1080p, 30fps, and the browser at 100% zoom so the dial's numbers
      are legible.
- [ ] Total runtime **under 5:00**. Check it before uploading, not after.

## What to cut if it runs long

In order. The first item goes first.

1. **The `audit` command at 1:55** (−12s) — `policy` alone carries the compliance claim.
2. **The theta bisection sentence at 2:25** (−8s) — the dial carries it visually.
3. **The 2021 migration numbers at 0:00** (−10s) — the RBI dates alone establish why now.

**Never cut:** 0:55 (the correction), 3:50 (live chaos), or 4:35 (the limitations). Those
three are the whole reason this pitch is different from the other four hundred.
