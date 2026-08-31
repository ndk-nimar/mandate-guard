# Limitations

Status: written 2026-09-01, during Phase 3 · Last updated: 2026-09-01

This document was written **before** the build was finished, which is the only reason to
trust it. A limitations section written on the last day is written to survive the results;
this one was written while the results could still change, and most of the entries below
were logged by the section that found them, at the moment it found them, rather than
recalled afterwards.

Nothing here is a number this project measured and disliked. Those are in
[`eval.md`](./eval.md) under their own headings — §6 fails an external check outright and §7
is a negative that was predicted before it was measured. This document is the other half:
what those measurements *rest on*, and what would have to be true before any of them should
move money.

Related: [`calibration.md`](./calibration.md) — every constant and its origin ·
[`model_card.md`](./model_card.md) — the hazard model's own limitations ·
[`eval.md`](./eval.md) — the results these caveats attach to.

---

## 1. The check this project holds itself to

Adyen reports a contextual bandit beating a fixed retry schedule by about **6%**. It is the
most trustworthy public number in payments for a claim of this shape, because it is an A/B
result on live traffic at scale rather than a simulator scoring its own homework. So it is
the yardstick, and the rule that follows from it is blunt: **a simulator reporting a 40%
lift has found a bug, not a result.**

[`results.md` §5](./results.md) computes this project's own figure against that line. It is
generated rather than typed, so it re-checks itself on every CI run and will say so in its
own words if a future parameter change pushes it into red-flag territory.

The reading, as of this snapshot:

* Against the campaign-tool default (`P1`, the fixed-schedule analogue), `P4` retains
  **+7.42%** more mandates. That is the figure comparable to Adyen's, and it sits next to it
  rather than an order of magnitude above it.
* Against doing nothing (`P0`), the lift is **+0.05%**. `P0` is what most of these mandates
  already get, so this is the honest figure for what selection buys on this book.

That looks like the check passing. Three things stop it from being one.

**It is the same number `eval.md` §6 calls a failure.** [`eval.md` §6.1](./eval.md) measures
the identical `P1` → `P4` contrast (1,131.9 → 1,215.9 retained) as its engagement axis, and
there it is a *problem*: LinkedIn cut volume and **lost** 1.8% of engagement, this arm cuts
volume and gains 7.4%. Cutting asks is not supposed to raise the thing the asks were for.
One figure, passing Adyen's magnitude test and failing LinkedIn's direction test, and both
readings are correct. This document does not get to quote the flattering one alone.

**It is the most parameter-sensitive figure in the project.** The engagement axis is
governed by `intervention.backfire_first_ask`, which has no public measurement
([`calibration.md` §4](./calibration.md)). Across its swept range the same contrast runs
from **−0.2%** at the bottom to **+36.5%** at the top ([`eval.md` §6.2](./eval.md)) — so at
the top of the sweep this project *would* be reporting a lift in the same territory as the
40% that marks a broken simulator. The check passes at the shipped parameter. It is not
robust to it, and the parameter it depends on is one nobody has measured.

**And the mechanism is not Adyen's.** `P1` causes 90.6 revocations and `P4` causes 0.3, so
90.3 revocations are avoided across a gap of 84.0 retained mandates — more avoided harm than
there is net gain. Adyen's bandit wins by *recovering more payments*. This arm wins by *not
destroying consent that the other arm destroys*. Two different claims that happen to produce
a similar-looking percentage, and treating them as the same result would be the dishonest
way to pass this check.

The defensible summary is narrow: **this project's lift is not in red-flag territory at the
shipped parameters, and it has no independent evidence that the shipped parameters are
right.**

---

## 2. The four things the rupee numbers rest on, none of which is measured

Ordered by how much they should worry a reader. §2.1 and §2.2 are the two largest risks and
neither has a fix available inside this repository; §2.3 is the first thing that can actually
be *fixed*. Those are different questions, and collapsing them into one ranking is how the
unfixable ones get quietly dropped.

### 2.1 `intervention.backfire_first_ask` — the number §1 turns on, recorded nowhere

`backfire_first_ask: 0.006` (with `backfire_twelfth_ask: 0.06`, holding the ten-to-one
ladder [`problem.md` §5.1](./problem.md) gives) is the probability that a first ask itself
kills a mandate. It is the parameter that decides §1's verdict, it decides the direction of
[`eval.md` §6](./eval.md)'s engagement axis, and **it has no public measurement.**

Two things make it worse than the other three entries in this section.

**It is not in `calibration.md` §4.** `eval.md` §6.2 cites that section for it — "no public
measurement (`calibration.md` §4)" — but the table there does not carry a row for it. So the
project's own register of unsourced constants is missing the constant its headline comparison
depends on. [`CLAUDE.md`](../CLAUDE.md) §3 allows a number four origins and *"it is in
`params.yaml`"* is not one of them. **This is the first thing to fix in the documentation,
and it is cheap: add the row.**

**It is swept, and the sweep does not rescue it.** [`eval.md` §6.2](./eval.md) runs the full
range and finds that *no* value reproduces LinkedIn's shape — so the mismatch is not a knob
that was mis-set. But the same sweep moves this project's own headline lift from −0.2% to
+36.5%. A parameter that cannot be fitted to the one external observation available, and
that swings the headline across the entire credible range, is carrying more weight than any
unmeasured constant should.

### 2.2 Chrome's 17–31%, applied seven times

`value.backfire_avoided_per_softer_step: 0.24` is the midpoint of a genuinely published
range — Chrome's quieter permission surface avoided 17–31% of permanent refusals across
~300M users. That range is real.

What is **not** published is that the same discount applies at *every rung* of a
seven-channel ladder running from an in-app nudge to an agent call. Chrome measured **one**
step between **two** UIs. Compounding it puts an email's backfire at `0.76⁵ ≈ 25%` of an
agent call's.

**This extrapolation is load-bearing.** It is what moved the shipped `(uplift, backfire)`
point from the wrong side of [`results.md` §4](./results.md)'s frontier to the right one.
Before this project claims that asking pays at the shipped parameters, this constant needs
either a measurement or its own sweep axis. It has neither.

§2.1 and this entry are the two the project cannot argue its way out of, and they fail
differently: §2.1 decides the **sign** of the headline comparison, while this one decides
whether asking pays **at all** — it is what put the shipped point on the profitable side of
the frontier. ([`calibration.md` §5](./calibration.md))

### 2.3 The hazard model is over-confident exactly where the money is spent

The model is correctly *ordered* but too *spread out*. It under-predicts in its lowest-risk
buckets (bucket 1: 0.019% predicted against 0.21% observed) and over-predicts in its highest
(bucket 20: 8.5% against 5.2%) — the top bucket's risk is overstated by about 60%.

The top bucket is precisely the population the allocator spends its budget on. Every rupee
figure derived from `p × L` for a high-risk mandate is overstated with it, so **the error
biases the optimiser toward asking** — the wrong direction for a project whose central claim
is that over-asking is expensive.

The fix is standard: a calibration layer (Platt scaling or isotonic regression) fitted on a
third slice, held out from both the fit and the test. It is not in because carving that
slice changes every number in [`eval.md`](./eval.md) §1 and §2, and GATE 1 passes without it.
**This is the first thing to do if the allocator's rupee numbers are to be taken
seriously.** ([`eval.md` §3](./eval.md), [`model_card.md`](./model_card.md))

### 2.4 `efficacy_prior` treats persuadability as a property of the channel, not the customer

Every mandate in this system responds to email exactly as well as every other mandate
responds to email. That is a real simplification, and [`eval.md` §7](./eval.md) is where it
becomes visible: the segment plot's shape is governed by risk alone, because nothing in the
value function lets one customer be more persuadable than another.

A per-mandate response model is the natural fix. It was out of scope before the deadline, and
it is named here as the second thing the model itself is missing, after §2.3.

The values themselves are **priors, not measurements**. They are labelled as priors in
`params.yaml`, and T2.8's sensitivity grid exists precisely so the project can carry them
without claiming them.

---

## 3. What the evaluation cannot tell you

### 3.1 This is not LinkedIn-shaped validation, and must not be presented as one

[`eval.md` §6](./eval.md) compares this system's shape against LinkedIn's published result
(−64.5% volume, −1.8% sessions, −47% complaints) and **fails the check**. The *direction*
matches on volume and complaints; the magnitudes are far more extreme; and the retention axis
points the **wrong way** at the shipped parameters.

The sentence "our result matches a published industry result" would be false. The true
sentence is narrower, and this project only gets the narrower one.

### 3.2 The −99.3% volume cut is a claim about the book, not an achievement

An allocator that declines 99.3% of possible asks, on a book where 99.3% of asks are
worthless, has done its job — and the number says more about the mandate population at this
snapshot (median projected hazard ≈ **0.0016 per week**) than about the optimiser. Almost
nothing in this book is near its coverage end, which is exactly the population an Indian
re-consent wave *would* have and KKBox does not.

The honest headline is the rupee gain over doing nothing, which [`eval.md` §5](./eval.md)
puts at **INR 212** — small, and ours.

### 3.3 Expectations, not samples

Every mandate carries a survival probability rather than a sampled outcome, so every result
is a mean with no variance attached. The harness cannot answer *"how often does this policy
do worse than doing nothing?"* — a question a merchant is entitled to ask before switching
one on. ([ADR 0003](./adr/0003-determinism-of-derived-data.md))

### 3.4 The online rule meets mandates in the wrong order

[`eval.md` §5](./eval.md)'s online serving rule walks mandates by `mandate_id`, so the spend
meter runs down in an order with no relation to when customers would actually arrive. A real
deployment meets them in traffic order, which correlates with engagement, which correlates
with value — and that correlation could help or hurt. No public dataset here carries arrival
times, so this is logged rather than estimated.

### 3.5 One book, one horizon, one snapshot

Everything above is the committed sample at one set of swept parameters and one snapshot
date. The staleness result in particular is a property of how fast *this* book moves; a book
with faster churn would punish a held price considerably harder.

---

## 4. What the data is not

**KKBox is Taiwanese music-streaming data wearing an Indian mandate costume.** The rail mix,
mandate validity and the recovery rate `R` are overlays ([`mapping.md` §3.1](./mapping.md)),
assigned rather than observed — the rail in particular comes from a **hash** of
`payment_method_id`, because KKBox never published what that column means. Nothing in this
repository is evidence about Indian mandate behaviour, and no number here should be quoted as
though it were.

`india.ntd_to_inr: 1.0` is a **decision**, not an exchange rate: KKBox's price ladder
(149/129/119/99 NTD) is read as India's (149/129/119/99 INR), argued at length in
[`mapping.md` §3.4](./mapping.md).

**Two of the strongest features are about our own data, not about customers.**
`frequency_imputed` (+2.48, the second-largest coefficient) flags rows whose billing cycle
*we* had to guess, and `member_known` (−1.15) flags whether a demographic row existed. Both
are real signal on KKBox and neither would survive a move to a merchant's own book, where
those fields are populated. Any transfer of this model must drop them and refit.

---

## 5. The citation chain has a hole in it

`prior_art.md` **does not exist.** [`calibration.md` §5](./calibration.md) and
[`problem.md`](./problem.md) both send every prior-art number there for its exact claim and
page reference — LinkedIn's −64.5% / −1.8% / −47%, Pinterest's inverted U, Chrome's
~300M-user quiet-UI result, Duolingo's fatigue half-life, ARMMAN's Whittle arm, and Adyen's
~6% — and the link terminates nowhere.

Those numbers currently reach the code from this project's own build plan, and
[`CLAUDE.md`](../CLAUDE.md) §3 is explicit that a build plan is not a source. The most
load-bearing of the six are the LinkedIn triple, which `eval.md` §6 now measures every result
against, and Adyen's ~6%, which §1 of this document uses as its yardstick.

Two further numbers are used nowhere but have appeared in drafts and must not reappear:

* **"Card post-2021 failure 20%+ in some categories."** Carried from the build plan, not
  found on 2026-08-29. It must not enter the pitch, the video, or `problem.md`. The verified
  2021 figures in `calibration.md` §2.2 say enough on their own.
* **"No transition period" for the April 2026 framework.** The secondary reporting neither
  states nor denies a transition window. *Absence of a stated transition is not a stated
  absence*, and the difference matters to a "why now" argument. To be settled from the
  circular text in T4.1.

---

## 6. Three things needed before this runs in production

Not a wish list. These are the three gates, in order, and none of them is satisfiable inside
this repository.

### 6.1 Real Indian mandate data

A merchant's own book, on real rails, with real mandate validity dates and real revocations.
Everything in §4 is downstream of not having this. Until then the model transfers nothing:
two of its strongest features are artefacts of KKBox's gaps, and the rail mix is a hash.

This also settles §3.2. The −99.3% volume cut and the near-zero lift over `P0` are both
consequences of a book whose mandates are nowhere near expiry. An Indian re-consent wave is
the opposite population by definition, and this allocator has never been run against one.

### 6.2 An intervention holdout

`P(active | do(send)) − P(active | do(drop))`, with assignment randomised **inside the
policy's own selections** rather than across the book. The harness is already built in that
shape ([`eval.md` §9](./eval.md), `eval/holdout.py`), so this gate is about running it on
real traffic, not about building it.

[`eval.md` §9](./eval.md) also establishes *why* it is non-negotiable. On the same run, with
the same allocator, the naive contacted-vs-untouched contrast reports **−0.1852** and the
randomised contrast reports **+0.0039**. The naive number has the wrong sign, because the
allocator contacts the mandates most likely to die and the contacted group is therefore
sicker before anything is sent. That naive number is what a notification dashboard shows by
default — so without this gate, a system that works will be measured as harmful and switched
off.

### 6.3 A shadow-mode merchant pilot — sized before it is run

Propose, never act; log every decision including the not-asked ones; compare against what the
merchant's existing process did. Two constraints on the design, both of which came out of
measurements rather than caution:

**It needs roughly five times this book's selected population.** Over 8 independent draws of
the assignment, the holdout effect is +0.0128 against a spread of 0.0142 — a ratio of
**0.9**, where roughly 2 is the minimum before an effect is distinguishable from zero. `P4`
selects only 107 mandates here, so each arm of the experiment holds about 53. Standard error
falls with `√n`, so reaching a ratio of 2 needs about **521 selected mandates**.

A pilot sized like this book would return a number indistinguishable from zero *however well
the system worked*. That is a fact about the pilot, not about the allocator, and it is worth
knowing before spending twelve weeks rather than after. ([`eval.md` §9](./eval.md))

**Measure the recalibration cadence first, before the model.** Holding one price for the
whole horizon drops the online rule to **51.65%** of the batch gain in the worst case,
against **76.82%** when it is refreshed weekly — and the price is the only thing that
changed; the rule, the value function and the book are identical. On this book the refresh
schedule is worth more than the choice between batch and online allocation
([`eval.md` §5](./eval.md)). It looks like an operations detail and it is the largest single
lever in the deployment.

---

## 7. Sentences this project has not earned

Written down because each of these is a sentence a pitch wants to say, and each one is false
or unsupported at the shipped parameters.

| tempting sentence | why it is not available |
|---|---|
| "99% fewer messages" | A claim about the **book**, not the system. 99.3% of asks on this book are worthless; declining them is arithmetic. §3.2 |
| "Our results match LinkedIn's published shape" | The check was run and **failed**. Direction matches on two axes, magnitudes do not, and retention points the wrong way. §3.1 |
| "We retain X% more subscribers" | Only against `P1`, and there it is mostly harm **not done** rather than retention created. Against `P0` it is +0.05%. §1 |
| "Validated against real customer response" | No holdout has ever been run on real traffic. Everything is a simulator scoring itself. §6.2 |
| "Calibrated to Indian mandate behaviour" | The rail mix is a hash; validity and `R` are overlays. §4 |
| "Asking pays" | Rests on two unmeasured constants: the backfire rate that decides the sign (§2.1) and the Chrome extrapolation that moved the point across the frontier (§2.2). |

The claims that **are** available: the pipeline is reproducible to the byte; the shadow price
is computed two independent ways and agrees to 0.00%; the ladder isolates allocation from
information; and the measurement apparatus is built correctly with its statistical power
known. Those are smaller sentences. They are also true.
