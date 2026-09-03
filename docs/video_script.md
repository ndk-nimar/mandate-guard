# Video script — 5:00, shot by shot

Status: written 2026-09-03, T5.9. **The recording itself is not done.** This document is
the script and the shot list; the deliverable it serves is a file under 5:00.

The rule for the whole thing: **numbers, not adjectives.** Nothing here says "powerful",
"robust" or "intelligent". Every claim on screen is either a generated document or a
command running live.

Two things go against the grain of a normal pitch and both are deliberate:

* **The chaos test is run live at 3:40.** A recorded green tick proves nothing a screenshot
  could not fake. Killing the model on camera and watching the system keep answering is the
  only version of that claim worth making.
* **The limitations are said out loud, unprompted, at 4:45.** A judge who finds a weakness
  the pitch hid discounts everything else in it. A judge who is handed the weakness by the
  presenter does not.

---

## Beat sheet

| time | on screen | what is said |
|---|---|---|
| 0:00 | Title, then `problem.md` §2 | the problem, in numbers |
| 0:40 | Three-line list | the three gaps |
| 1:05 | `prior_art.md` §2 | what the Chrome paper actually says |
| 1:10 | `eval.md` §2, `model_card.md` | the evidence stack |
| 1:40 | Terminal | live demo |
| 3:00 | `ledger` output | the refusal ledger |
| 3:40 | Terminal | **live chaos test** |
| 4:15 | Slide | the Subscription Recovery collision |
| 4:45 | `limitations.md` | limitations, unprompted |

---

## 0:00 — The problem, in numbers

> India's recurring-payments book runs on e-mandates. In April 2026 the RBI consolidated
> eight circulars into one framework, effective immediately, with no transition period —
> clause 1(b), and clause 11 repeals the old ones with no savings provision.
>
> The last time this book had to move — the 2021 card-tokenisation migration — it took
> about eight months and the top fifty banks saw recurring business decline while it
> happened.
>
> So a merchant has a book of mandates that need re-consent, and a limited number of times
> they can ask before the asking itself becomes the problem.

**Show:** `docs/problem.md` §2, scrolled to the 2021 figures.

**Do not say:** "20%+ card failure". That number could not be sourced and is struck —
`calibration.md` §5.

## 0:40 — The three gaps

> Three things nobody has built for this.
>
> One: **it is an allocation problem, not a campaign.** Every existing tool answers "send or
> don't". The question is *who, on which channel, under a budget*.
>
> Two: **the cost of asking is real and nobody prices it.** A message costs money, costs
> patience, and can cause the revocation it was sent to prevent.
>
> Three: **the compliance surface is new.** The rules arrived in April 2026 and no
> retention agent knows they exist.

## 1:05 — What the Chrome paper actually says

> There is public evidence that a softer ask costs almost nothing and avoids a lot of harm.
> Chrome's quieter permission prompt, USENIX Security 2021, across about 40 million users:
> **under 5% of grants lost, up to 30% fewer unnecessary actions on the prompt.**

**Show:** `docs/prior_art.md` §2, on the quoted abstract.

> That figure is on screen for a reason. This project quoted it wrongly for four phases —
> as "17 to 31 percent of permanent refusals" — until somebody opened the paper on the
> second of September. It is corrected in five files and the correction made our own case
> weaker, not stronger.

**Why this beat is here.** The original plan opened with the Chrome result as a headline.
It cannot be that any more, and the honest version — a corrected number, shown as
corrected — is a stronger opening than the wrong one was. Fifteen seconds, and it sets the
standard for every number after it.

## 1:10 — The evidence stack

> The lapse hazard is fit on real subscriber data — 21 million transactions, a person-week
> frame, a discrete-time survival model. It beats the naive "risk equals closeness to
> expiry" baseline on Brier score, and it beats predicting the base rate, which the binned
> baseline does not.
>
> It is also over-confident exactly where the money is spent: the top bucket's risk is
> overstated by about 60%. That is in the model card, not in a footnote.

**Show:** `docs/eval.md` §2 table, then `docs/img/reliability.png`.

## 1:40 — Live demo

Terminal, one command at a time. Nothing pre-run.

```bash
uv run mandateguard policy
```
> Twenty rules, compiled from the circular, each carrying a clause number and a verbatim
> quote. The hash on screen is checked against the source text on every load.

```bash
uv run mandateguard audit --rail enach --amount 20000
```
> A twenty-thousand-rupee eNACH debit. It names the clause it fails and the remedy — and
> notice eNACH is not inside clause 2's scope at all, which is 15% of the modelled book
> sitting outside the regulation this project calls its "why now".

```bash
curl -s localhost:8000/allocate -d '{"budget_inr": 500}' | jq
```
> 1,354 live mandates, a weekly budget, and the allocator asks 109 of them. `acted: false`
> is a field in that response, not a line in the documentation — shadow mode is the
> default.

## 3:00 — The refusal ledger

```bash
uv run mandateguard verify-ledger data/ledger/<run>.jsonl
uv run mandateguard replay --decision-id <id>
```

> Every decision is logged — **asked and not asked**. The not-asked ones are the point:
> each carries the rupee reason it was not worth making.
>
> And any of them replays. Same policy hash, same model version, same seed, same snapshot,
> byte-identical. If the policy file has changed since, the replay **refuses** rather than
> producing a new answer that looks like an old one.

## 3:40 — Live chaos test

Run it on camera.

```bash
uv run pytest tests/test_chaos.py -q
```

> Kill the model, corrupt the policy file, feed nulls. The system degrades along four
> ordered rungs — model down goes to rules-only, stale model goes to the conservative
> floor, hash mismatch halts. Worst rung wins.
>
> The deterministic path is the *ordinary* path here, not the emergency one, which is why
> this works: the model never decides anything.

## 4:15 — The Subscription Recovery collision

Say it before a judge does.

> Razorpay already ships Subscription Recovery. It retries **after a payment fails**. This
> runs **before a mandate lapses**. Same book, different point in the lifecycle — and this
> one sits on the April 2026 compliance surface, which no retention agent currently models.

## 4:45 — Limitations, unprompted

> Three, and they are in `limitations.md`, written during Phase 3 rather than on the last
> day.
>
> The data is Taiwanese music streaming. The rail mix is assigned from a hash.
>
> The two constants the headline turns on — backfire and the channel softness discount —
> have no public measurement. One of them turned out not to have the provenance we claimed
> either.
>
> And we fail the one external check available: LinkedIn cut sends and lost engagement, we
> cut sends and gain it. That is in `eval.md` §6 under its own heading, with a sweep showing
> no backfire rate reproduces their shape.
>
> Against doing nothing at all, the lift on this book is 0.05%.

**Close on:** `uv run mandateguard repro --check`, running, ending in *byte-identical*.

---

## Production notes

* **Terminal only, no slides except 4:15.** A slide is a claim; a terminal is a
  demonstration.
* **Do not speed up the terminal.** `replay` takes about six seconds because it re-runs the
  whole allocation — that slowness is what makes it a replay rather than a new decision.
* **Have the API already running** in a second pane before 1:40. Booting it on camera costs
  fifteen seconds and proves nothing.
* **Budget check:** 0:40 + 0:25 + 0:05 + 0:30 + 1:20 + 0:40 + 0:35 + 0:30 + 0:15 = 5:00
  exactly. The first thing to cut if it runs long is the `audit` command at 1:40; the last
  thing to cut is 4:45.
