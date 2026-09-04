# Limitations

Status: written 2026-09-01, during Phase 3 · Last updated: 2026-09-04 (§9, first CI run)

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

**It was not in `calibration.md` §4 — ~~fixed 2026-09-02 (T5.7)~~.** `eval.md` §6.2 cited
that section for it — "no public measurement (`calibration.md` §4)" — while the table there
carried no row for it. For three phases the project's own register of unsourced constants
was missing the constant its headline comparison depends on, and every reader who followed
the citation landed on a table that did not mention it. [`CLAUDE.md`](../CLAUDE.md) §3
allows a number four origins and *"it is in `params.yaml`"* is not one of them. The row is
there now, along with `backfire_twelfth_ask` and `uplift_scale`. **The fix was one table
row and it took four days to make, because nothing fails when a document is silent.**

**It is swept, and the sweep does not rescue it.** [`eval.md` §6.2](./eval.md) runs the full
range and finds that *no* value reproduces LinkedIn's shape — so the mismatch is not a knob
that was mis-set. But the same sweep moves this project's own headline lift from −0.2% to
+36.5%. A parameter that cannot be fitted to the one external observation available, and
that swings the headline across the entire credible range, is carrying more weight than any
unmeasured constant should.

### 2.2 A chosen constant under a published ceiling, applied seven times

**This entry was itself wrong until 2026-09-02, and the correction makes it worse.** It
read: *"`value.backfire_avoided_per_softer_step: 0.24` is the midpoint of a genuinely
published range — Chrome's quieter permission surface avoided 17–31% of permanent refusals
across ~300M users. That range is real."*

**That range is not real.** T5.7 opened the paper. The abstract reports an A/B test over
~40M users and *"up to 30% fewer unnecessary actions on the prompts"* — one upper bound,
about actions taken on a prompt, not about permanent refusals. There is no 17–31%, no
midpoint, and no ~300M. [`prior_art.md` §2](./prior_art.md) quotes the sentences.

So 0.24 is **a value chosen under a published ceiling**. That is a legitimate thing to do
and an illegitimate thing to describe as sourced, and this section described it as sourced
for four phases. The half of the claim that survives is the grant loss: 10% → 9.8% desktop
and 20.1% → 19.1% Android, against the paper's "less than 5%".

What is **not** published at all is that the same discount applies at *every rung* of a
seven-channel ladder running from an in-app nudge to an agent call. Chrome measured **one**
step between **two** UIs. Compounding it puts an email's backfire at `0.76⁵ ≈ 25%` of an
agent call's.

**This extrapolation is load-bearing.** It is what moved the shipped `(uplift, backfire)`
point from the wrong side of [`results.md` §4](./results.md)'s frontier to the right one.
Before this project claims that asking pays at the shipped parameters, this constant needs
either a measurement or its own sweep axis. It has neither — and after the correction
above, it does not have the provenance it was said to have either. That is now
[`calibration.md` §6](./calibration.md) item 6.

§2.1 and this entry are the two the project cannot argue its way out of, and they fail
differently: §2.1 decides the **sign** of the headline comparison, while this one decides
whether asking pays **at all** — it is what put the shipped point on the profitable side of
the frontier. ([`calibration.md` §5](./calibration.md) · [`prior_art.md` §2](./prior_art.md))

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
(−35.5% volume, −1.8% sessions, −53.0% complaints — corrected from the paper on 2026-09-02,
§5) and **fails the check**. The *direction* matches on volume and complaints; the
magnitudes are far more extreme, and the correction made them *more* so; and the retention
axis points the **wrong way** at the shipped parameters.

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

## 5. The citation chain had a hole in it, and closing it cost two numbers

**Closed 2026-09-02 (T5.7).** Until that date this section read *"`prior_art.md` **does not
exist**"* — and it did not, while [`calibration.md` §5](./calibration.md) and
[`problem.md`](./problem.md) both sent every prior-art number there for its exact claim and
page reference. Six load-bearing figures had a citation chain that terminated in a dead
link, which reads exactly like a citation that resolves.

[`prior_art.md`](./prior_art.md) exists now. **Two of the six were wrong**, and both
corrections go against this project:

| number | quoted until 2026-09-02 | what the paper says |
|---|---|---|
| LinkedIn volume (KDD '16) | −64.5% | **−35.5%** |
| LinkedIn complaints | −47% | **−53.0%** |
| Chrome's backfire discount | "midpoint of a published 17–31%" | **no such range**; the paper says "up to 30%", about actions on prompts rather than refusals |

Neither is a transcription slip. Both are a published table read as reporting something
other than what it reported — LinkedIn's Table 3 gives *retained levels* against a control
of 100, and this project read two of the three as reductions while correctly converting the
third. The LinkedIn correction widens `eval.md` §6's mismatch from 1.5x to 2.8x. The Chrome
correction demotes `value.backfire_avoided_per_softer_step: 0.24` from a sourced midpoint to
a chosen value under a ceiling, which makes §2.2 of this document — already flagging that
constant as load-bearing and unmeasured — sharper rather than softer.

**What is still open.** Four of the seven modelling sources remain unread: Duolingo's
fatigue half-life, ARMMAN's Whittle arm, Adyen's ~6% (which §1 of this document uses as its
yardstick), and Twitter/X reachability. `prior_art.md` §7 marks each one `unread` rather
than dressing it as a citation, and that is the current honest state, not a completed job.

Two further numbers are used nowhere but have appeared in drafts and must not reappear:

* **"Card post-2021 failure 20%+ in some categories."** Carried from the build plan, not
  found on 2026-08-29. It must not enter the pitch, the video, or `problem.md`. The verified
  2021 figures in `calibration.md` §2.2 say enough on their own.
* ~~**"No transition period" for the April 2026 framework.**~~ **Settled 2026-09-01 in
  T4.1**, from the circular text rather than from the secondary reporting: clause 1(b) is
  "effective immediately" and clause 11's repeal carries no savings provision. What the text
  is *silent* on — whether mandates registered under the repealed circulars must be
  re-registered — is §8.4 of this document.

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

---

## 8. What reading the actual regulation cost us (Phase 4)

Added 2026-09-02, on the day T4.1 read the circular text instead of reading about it. Three
of the four entries here are worse for this project than what it believed the day before,
which is the reason they get a section rather than a footnote.

### 8.1 Clause 2 does not reach eNACH, and eNACH is 15% of the book

The framework applies to recurring transactions "using cards / PPI / UPI". eNACH is not in
that list; eNACH and NACH mandates run under NPCI's procedural guidelines instead.
`config/params.yaml` assigns eNACH a **15%** share of `india.rail_mix`, so roughly a seventh
of the modelled book sits outside the regulation whose arrival is this project's entire "why
now" argument.

This is not fatal and it is not cosmetic. The optimiser does not care — it allocates asks
against hazard and rupees, and a mandate's rail affects its cost, not its legality. What it
changes is the pitch: "the RBI just changed the rules for this book" is true of 85% of the
book as modelled, and the sentence has to say so. The auditor returns `needs_human` for an
eNACH mandate rather than grading it, and `scope_cards_ppi_upi` is the only rule in the
compiled book that fails into `needs_human` rather than `non_compliant`.

**What would close it:** a second compiled rulebook for the NACH guidelines, cited the same
way. That is a Phase 4-sized task on its own and is not in this build.

### 8.2 The pre-debit notice is the issuer's obligation, not the merchant's

Clause 6(a) reads "An issuer shall send a pre-transaction notification"; clause 3(a) takes
'issuer' from the 2025 authentication directions, where it means the card, PPI or account
issuer. This project is merchant-side. It therefore does not discharge clause 6 at all.

The consequence lands squarely on T4.3, whose stated design is "an RBI-compliant pre-debit
notice with a piggybacked re-consent CTA". The notice is composed *for an issuer or payment
aggregator to send*, and the piggybacked ask is a commercial arrangement with that party —
not a regulatory entitlement, and not something a merchant can do unilaterally. Clause 10(c)
("An acquirer shall ensure compliance ... by merchants on-boarded by them") is the only
sentence in the framework that reaches a merchant, and it reaches them through the acquirer.

**Sentence not available:** "MandateGuard sends the RBI-mandated pre-debit notice."
**Sentence available:** "MandateGuard composes a notice that passes a deterministic
compliance linter, for the party whose obligation it is to send it."

### 8.3 One compiled rule is an inference, and is labelled as one in the YAML

`debit_within_customer_cap` asserts that a variable-amount debit above the customer's stated
maximum is a breach. Clause 4(c) grants the customer *a facility to specify* that maximum; it
contains no sentence saying a debit above it is non-compliant. This project reads the facility
as binding — a cap that can be exceeded is not a cap — but the reading is ours, and clause
5(b) shows the framework is willing to say the opposite about other customer-set controls
("Payments under e-mandates shall not be subject to any other limits / controls set by the
customer"). The rule's `description` field says so in the file itself, which is where a
reviewer will actually read it.

### 8.4 The framework is silent on mandates that predate it

Clause 1(b) is "effective immediately" and clause 11 repeals eight circulars with no savings
clause — so `calibration.md` §5's "no transition period" is now settled *from the text*. What
the text does not do is say what happens to mandates registered under the repealed circulars.
Clause 10(b) is the only sentence touching them and it covers card re-issuance only. Silence
is not permission and it is not a requirement. Anyone sizing "how much of the book must be
re-consented and by when" is reading an intention into a gap, and this project does not.

### 8.5 The compile is checkable but has not been reproduced

The twenty rules in `policy/mandate_policy.yaml` were compiled during the T4.1 session and
then reviewed clause by clause. Every one of them is *checked* on every load: the clause
number must exist in the circular, the quote must appear in it verbatim, the circular's
SHA-256 must match the hash pinned in the policy, and the expression must parse under a
call-free whitelist over a declared field vocabulary.

What has **not** happened is a re-run of the compiler job against the API, because no
Anthropic credential was available on the machine where Phase 4 was built. So
`tests/cassettes/policy_compiler/` is empty and `scripts/compile_policy.py` exits 2 with a
cassette miss. The claim this task can make is that the rules are verifiable against their
source; the claim it cannot yet make is that an independent run of the same prompt produces
them. `--check` is the half that works without a credential, and it is the half that matters
for trusting the rulebook.

### 8.6 The circular text is retrieved, not certified

`policy/sources/rbi-2026-04-21-e-mandate-framework.md` was fetched from `rbi.org.in` and
converted from HTML to markdown, then compared against a second verbatim reproduction before
being committed. The RBI PDF was not parsed byte for byte. Clause numbering and the wording
of every quoted obligation agree across the two renderings, and the one deliberate
substitution — writing the rupee sign as `Rs.` in clause 8 so that Windows and Linux agree on
one encoding — is stated in the file. For a hackathon this is enough; for anything that
matters, the PDF is the artefact, and the clause numbers are what tell a reader where to look.


### 8.7 The golden set scores 100%, and the same reader wrote both sides

`docs/llm_eval.md` reports 114/114 exact on the rules arm — verdict *and* citations. That
number is real and it is not validation.

The twenty compiled rules and the 120 expectations were written in the same session, from
the same reading of the same circular, by the same reader. A misreading of clause 8(b) would
have gone into the rule and into the expectation, and the table would show 100% either way.
An independent test set is one where the person writing the expectations has not seen the
implementation; that is not what this is, and the document says so where the number is
rather than in a footnote.

What the set *is* worth is forward-looking: 120 statements about what the circular requires,
written down before anyone had a reason to prefer a particular answer, each carrying the
clause it came from. The next time a threshold moves or a guard loosens, this file is what
notices. That is a regression suite with citations. It is a real thing to have and it is a
smaller thing than the headline suggests.

**What would close it:** a second reader compiling expectations from the circular without
seeing `policy/mandate_policy.yaml`, and the disagreements being the finding. That is a
person-day this build did not have.

### 8.8 The adversarial gap is +0.0, on a set this project wrote to break itself

T4.8's honesty metric is the gap between natural-set and adversarial-set accuracy. It is
**+0.0 points**: 59 natural cases and 55 adversarial ones, both at 100%.

Read carefully, that is weaker evidence than it looks, for the same reason as §8.7 and one
more. The adversarial cases are hand-written, by the person who wrote the rules, against the
boundaries that person already knew were boundaries. An adversarial set's job is to contain
the cases nobody thought of, and a set written by the implementer is definitionally made of
cases somebody thought of.

**T4.8's generator is cut** (CUT #2 in `tasks.md`), and the cut is honest rather than
convenient: a generator is the one part of that task that could not have been faked without
a credential, because its output is supposed to be surprising. What survives is the
measurement apparatus and a declared split criterion in `scripts/build_golden.py`, so the
gap becomes meaningful the moment someone runs a generator against it.

Until then the right reading is: **this gap is a floor on the true one, not an estimate of
it.** A pitch may say the measurement exists. It may not say the system is robust to
adversarial input.

### 8.9 What the linter cannot see

`agent/linter.py` reads text, and three of its limits are structural rather than fixable:

* **It cannot see a UI.** The visual form of interface interference — a greyed-out decline
  next to a bold accept — is invisible to a text checker. Only the wording form is caught.
* **It matches literal phrases.** A dark pattern written in words nobody listed passes.
  `policy/dark_patterns.yaml` is a detector, not a definition, and the regulator publishes
  no list of banned wordings.
* **It checks amounts for fabrication but not dates.** A date can be written a dozen ways,
  and a half-working date parser produces false failures — which, inside a
  regenerate-then-escalate loop, is a queue of humans reviewing correct notices. The amount
  check is precise because currency-marked numbers are unambiguous; the date check is
  presence-only.

### 8.10 The safety layer's limits are process-shaped

`safety/guard.py` is the only path to acting, and three of its properties are weaker than
the words "spend cap" and "rate limiter" suggest:

* **The counters are in-process.** Two workers each get the whole allowance, so the rate
  limit and the spend cap are per-process rather than per-system. Making them global needs
  shared state this project does not have. A limiter that *looked* global and was not would
  be worse than one that says so.
* **The kill switch has no authorisation.** It is a file, and anyone with write access to
  the directory can create or delete it. That is the point at 02:00, when the person who
  needs to stop a live system has a shell and may not have a deploy pipeline — and it is a
  weakness at every other hour.
* **Nothing records who flipped the mode.** `safety.mode: live` is a line in a YAML file.
  Git says who committed it; nothing in the running system says who deployed it.

**What the guard does guarantee**, and it is tested rather than asserted: a refused action
is never charged, the cap is checked before the spend rather than after it, shadow mode
consumes the allowance so a dry run is informative about the live one, and the worst rung of
the degradation ladder wins when several apply.

### 8.11 A stale model drops the system to the floor, on an unmeasured threshold

`safety.max_model_age_days: 30` decides when the hazard model is too old to spend money on.
It is a **decision, not a measurement**: the KKBox frame gives no basis for a drift
half-life, and inventing one would be a number without an origin. Thirty days is a
plausible operational default and nothing more, which is why it is in `calibration.md` §5.

The rung it triggers is the counter-intuitive one and worth defending explicitly. The
instinct is that an old model beats no model. It does not: an old model still outputs a
confident hazard, the allocator still spends real rupees against it, and nothing in the
output looks stale. Not asking is the only action whose cost stays bounded when the input
cannot be trusted.

### 8.12 The refusal explainer accepted a non-answer, and a chaos test found it

Recorded because of how it was found rather than what it was.

The explainer lets a model rewrite its deterministic sentence and checks the rewrite for
**invented rupee figures**. That check asks "is every number here a real one" — and a
rewrite containing *no* numbers passes it trivially. A chaos client returning control
characters and unrelated prose was therefore accepted and would have been written into the
ledger as the reason a customer was not contacted.

The general shape: **a checker that only looks for wrong answers accepts every non-answer.**

Three cheap plausibility checks now sit beside the fabrication one — no control characters,
length bounds, and at least one of the figures it was given when the refusal turns on any.
What none of them can check is whether the sentence says the *right* thing about the right
numbers; that needs a second model grading the first, which is a different system and not
something a fallback path should depend on.

---

## 9. "Byte-identical" holds on one machine, and nobody had checked a second one

Added 2026-09-04, on the day it was found.

[ADR 0003](./adr/0003-determinism-of-derived-data.md) is one of this project's three
standing rules and the strongest claim it makes about its own outputs: every derived file
is byte-identical across runs, checked by comparing bytes rather than counts. `repro
--check` enforces it, CI runs `repro --check`, and [`architecture.md`
§2.5](./architecture.md) describes the guarantee.

The claim is true. Its scope was written wider than the evidence.

### 9.1 What was actually being checked

This repository had **no git remote until 2026-09-04**. `ci.yml` had been in the tree since
Phase 1 and had never executed once — not a failing workflow, an unrun one. Every
determinism result this project reported came from `repro --check` on the author's Windows
machine, including the "fresh tree, byte-identical, exit 0" run recorded for GATE 5.

Same command, same lockfile, same committed sample, one operating system.

### 9.2 What the first CI run found

Run [`33877849865`](https://github.com/gurkanwaldeep927/mandate-guard/actions/runs/33877849865),
`ubuntu-latest`. `check` passed in 51s — lint, format, scoped types, the full suite, the
policy re-compile and the chaos suite all behave identically on Linux. `results` failed:

```
docs/img/segments.png | Bin 47075 -> 54315 bytes
docs/img/sweeps.png   | Bin 80382 -> 89069 bytes
docs/results.md       |  64 +++++++++++------------
```

The first diagnosis was line endings — a Windows checkout with `core.autocrlf=input` and no
`.gitattributes` is the obvious suspect. **It was wrong, and the diff's shape is what says
so:** 32 changed lines rather than a whole-file rewrite. Downloading the Linux-built
artifact and comparing with `\r` stripped isolates the real difference:

| `results.md` | committed (Windows) | rebuilt (Linux) |
|---|---|---|
| `P0` total value | INR 413,2**19** | INR 413,2**18** |
| `P5` contact rate | 89.80**5**% | 89.80**4**% |

**One rupee in 413,219 — 0.00024% — and one digit in the fourth decimal of a percentage.**
Floating-point summation is not associative, and the order a platform's math library and
BLAS accumulate in is not part of any promise this project can make. The PNGs differ far
more visibly in bytes for an unrelated reason: matplotlib resolves different fonts on Linux,
so the same chart rasterises different text pixels.

### 9.3 What did not move

Every figure this project quotes. `P4` net **+81**, `P1` **−302,204**, the uplift columns
**4.5% / 0.9%**, the `P1` → `P4` retention contrast **1,131.9 → 1,215.9** that §1 above and
[`eval.md` §6](./eval.md) both turn on — identical on both platforms. The drift is confined
to the last significant digit of the largest sums.

This is the distinction the finding actually turns on: **the tightness of a gate and the
significance of a number are separate properties.** A byte gate is stricter than any
decision made from these numbers requires, which is why it is worth having and also why it
fails on a difference that changes nothing.

### 9.4 The obvious fix was tried, and it failed too

GATE 5 was moved to `windows-latest` — the platform the committed artifacts were produced
on — and run
[`33879308817`](https://github.com/gurkanwaldeep927/mandate-guard/actions/runs/33879308817)
**failed as well**. Smaller, but failed:

| | this laptop | GitHub `windows-latest` | GitHub `ubuntu-latest` |
|---|---|---|---|
| `P1` ARR retained | 384,906 | 384,90**7** | 384,90**5** |
| `P4` ARR retained | 413,470 | 413,47**1** | 413,46**9** |
| `P2` rate | 83.604% | 83.60**5**% | 83.604% |
| `segments.png` | — | **byte-identical** | +7,240 bytes |
| `sweeps.png` | — | −13 bytes | +8,687 bytes |
| `results.md` lines changed | — | 20 | 32 |

So this is not a platform property. **It is a machine property.** Two Windows machines
running the same command on the same lockfile against the same committed sample produce
different last digits, because the order a CPU and its math library accumulate a sum in is
not something a lockfile pins. No CI runner can be configured out of this, and the previous
version of this section — which called the tolerance-based fix a Phase 6 change on the
grounds that pinning the platform would do — was wrong within twenty minutes of being
written.

### What CI gates on now

`scripts/check_drift.py`, run on **both** `windows-latest` and `ubuntu-latest`, both
blocking. The byte comparison still runs and still reports; it no longer decides the build,
because a measured, documented difference is not a regression. The rule is named rather
than loose:

| what | rule |
|---|---|
| prose lines | byte-identical. Every quoted figure in this document and in `results.md` is one |
| table cells, named drifting columns | at most **one unit in the last printed digit** |
| table cells, everything else | exact — including all of `results.md` §5, the Adyen comparison §1 above turns on |
| PNGs | identical dimensions, not identical bytes |
| any other artifact | byte-identical, no allowance |

The drifting columns are named in the script: `rate`, `ARR retained (INR)`, `profit at
optimum`, the budget grid, and any column whose *heading is itself a number* — §4's uplift ×
backfire plane. That last clause is in the script because the first version of it did not
have it and three cells of §4 failed a test written to pass. The allowance was measured
against the artifacts both runners actually produced, not reasoned about.

`tests/test_drift_check.py` pins the boundary from both sides: 413,470 → 413,471 passes and
413,470 → 413,472 fails. An allowance tested only in the middle of its range is an allowance
nobody has measured.

The exact-byte gate is **not weakened, only relocated**. `uv run mandateguard repro --check`
still fails on a single byte and is still what runs before a commit, on the machine the
artifacts are committed from. That is the check that caught the mandate book returning 1,053
and 1,054 on identical input, and it would still catch it.

### What was rejected

* **Regenerating the artifacts on Linux and committing those.** Turns CI green and breaks
  `repro --check` permanently for the author, who develops and demonstrates on Windows. It
  moves the failure rather than removing it — and after `windows-latest` also failed, it
  would not even have worked.
* **Making GATE 5 non-blocking.** The cheapest option and the worst one: a reproducibility
  gate that cannot fail is not a gate, and "our CI never goes red here" is the weakest
  sentence this project could offer a reader.
* **Summing in a fixed order — `math.fsum`, or integer rupees.** This would genuinely remove
  the arithmetic drift rather than tolerate it, and it is the better fix. It also changes
  every rupee total by up to a unit, which means regenerating and re-arguing every committed
  artifact and every number quoted from them, in the value layer, the day before a deadline.
  It is recorded here as the thing to do next rather than done badly now. It would not touch
  the PNG difference.
* **Pinning fonts and a BLAS backend.** Plausible, unbounded, and it makes the guarantee
  depend on pins nobody re-verifies.


### 9.5 The general failure, which is the part worth keeping

A workflow that has never run is not a gate. It is a file that describes one.

The determinism claim was not unverified — it was verified repeatedly, thoroughly, and
always against the same machine. The gap was invisible precisely *because* the checking was
diligent: `repro --check` passing on a fresh export, on a clean clone, twice, still only
ever answered the question "is this deterministic **here**". The one input that was never
varied was the one the claim quietly depended on.

This is the same shape as §2.5's timing finding in [`architecture.md`](./architecture.md),
where a gitignored `.env` made a fresh tree 45× slower and nothing failed, because
determinism was being checked and tractability was not. Both times the missing check was
invisible from inside the machine that had already passed it.
