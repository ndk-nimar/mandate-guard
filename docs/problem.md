# Problem Formulation

Status: draft · Last updated: 2026-08-26

This document states the problem MandateGuard solves, the notation used throughout the
codebase, and — most importantly — what the word "budget" actually means here. Section 4
exists because "why not just message everyone?" is the first question a reader should ask,
and answering it loosely is how this kind of system gets built wrong.

Related: [`prior_art.md`](./prior_art.md) · [`calibration.md`](./calibration.md) ·
[`limitations.md`](./limitations.md)

---

## 1. Setting

An **e-mandate** is a standing authorisation a customer gives so a merchant can debit them
on a recurring schedule without per-transaction approval. In India this runs over three
rails: UPI AutoPay, card e-mandates, and e-NACH.

Mandates end in one of two ways, and the distinction matters more than anything else in
this document:

- **Lapse** — the mandate reaches its validity end, or fails silently, without the customer
  taking any action. The customer is typically unaware and neutral.
- **Revocation** — the customer actively goes and cancels the authorisation.

Both stop the revenue. They are not the same event, and Section 5.2 explains why.

## 2. Why now

The RBI notified the Digital Payments E-Mandate Framework on **21 April 2026**, with no
transition period. It changes mandate validity, authentication on modification, the
₹15,000 threshold and its exemptions, and pre-debit notice requirements.

The last comparable migration (2021) is the base rate for what happens next: roughly **70%
transaction decline** at the peak, and **62.5 million mandates** taking about **eight
months** to migrate.

Current UPI AutoPay volumes give the scale of the exposed book. **These are July 2025
figures**, which are the most recent public ones — dated here on purpose, because an
undated volume number in a 2026 document reads as a current one: ~50M new mandates/month,
~808M executions/month, and **20M+ revocations/month**. The revocations are attributed to
debit failures from insufficient balance rather than to customers deciding to leave, which
is what makes them a population worth trying to save.

Every figure in this section is sourced in [`calibration.md`](./calibration.md), with the
publication, the date, and the exact period it describes; none of them are estimates of
ours, and the ones this project could **not** verify are listed there too.

## 3. The decision problem

A merchant holds a book of mandates. Some fraction will end over the next quarter. The
merchant can reach customers and ask them to re-consent — but asking is not free, and
asking too much causes the exact outcome it was meant to prevent.

> Given a book of at-risk mandates and a constrained ability to contact customers:
> **which mandates to ask, through which channel, and in which week?**

Note that all three parts are decisions. Most existing systems answer only the first, and
answer it by arrival order rather than by value (Section 8).

## 4. Notation

| Symbol | Meaning |
|---|---|
| `i` | a mandate, `i ∈ {1..N}` |
| `c` | a channel, `c ∈ C` = {in-app, email, SMS, WhatsApp, IVR, letter, agent call} |
| `t` | a week, `t ∈ {1..T}`, `T = 12` |
| `x[i,c,t]` | decision variable ∈ {0,1} — ask mandate `i` via channel `c` in week `t` |
| `k[c]` | cost of one contact through channel `c` |
| `B[t]` | contact budget available in week `t` |
| `h[i,t]` | **hazard** — P(mandate `i` ends in week `t` \| alive at start of `t`) |
| `u[i,c,t]` | **uplift** — change in P(survive) caused by the ask |
| `b[i,c,t]` | **backfire** — P(revocation caused by the ask) |
| `L[i]` | expected remaining revenue if the mandate survives |
| `q[i]` | P(re-acquire the customer later \| mandate **lapsed**) |
| `r[i]` | P(re-acquire the customer later \| mandate **revoked**) |
| `R[i]` | reachability value — the option value of still holding a channel to this customer |
| `d[i,t]` | days since this customer was last contacted |
| `μ`, `ν` | separate prices on a good outcome and on a complaint (LinkedIn, KDD 2016) |
| `α` | weight on reachability (Twitter/X, 2022) |
| `γ`, `hl` | fatigue magnitude and half-life (Duolingo, KDD 2020) |
| `θ` | shadow price on the budget constraint — the rupee value of one more ask |

Structural constraints:

```
Σ_c x[i,c,t] ≤ 1          for every i, t     (at most one channel per mandate per week)
Σ_i Σ_c k[c]·x[i,c,t] ≤ B[t]   for every t   (per-week contact budget)
```

`θ` is the dual variable on the second constraint. It is the single most business-legible
output of the whole system: *"the next ask is worth ₹X."*

---

## 5. What "budget" actually means

### 5.1 It is not money

The naive reading is that we ration asks because contacting people is expensive. On the
cheap channels, that is simply false, and the pitch should never claim it:

```
10,000 SMS × ₹0.15  =  ₹1,500
```

₹1,500 is not a constraint for any merchant operating a mandate book. If money were the
only cost, the correct policy would be to contact everyone, every week, forever.

The real price of an ask is not its channel cost. It is:

```
true cost of an ask  =  k[c]  +  b[i,c,t] · (loss when a customer revokes)
```

With an illustrative backfire probability of 0.5% and a customer worth ₹9,000, an SMS that
costs ₹0.15 carries an expected cost near **₹45** — roughly 300× its price. Ten thousand
asks therefore risk about ₹4.5 lakh of expected value, not ₹1,500.

**Backfire is not constant.** It compounds with contact frequency, which is what makes a
12-week horizon behave differently from a single campaign. Illustratively, if per-message
backfire rises from ~0.6% in week 1 to ~6% by week 12, contacting the whole book every week
implies roughly 30% cumulative attrition caused by our own messages — while the SMS spend
across all twelve weeks is still only ~₹18,000. The spend never becomes the binding
constraint; the customer's patience does.

These specific probabilities are **not measured** — no public dataset contains them
(Section 8, Gap 3). They are swept, not assumed; see [`eval.md`](./eval.md).

### 5.2 Where the budget genuinely comes from

Three distinct sources, and the model has to respect all three:

**(a) Customer patience.** A soft constraint, priced rather than capped. This is the term
`ν · b[i,c,t] · (...)` in the objective below.

**(b) Real capacity on the expensive channels.** Here money and headcount are hard limits:

| Channel | One pass over 10,000 | Over 12 weeks |
|---|---|---|
| SMS @ ₹0.15 | ₹1,500 | ₹18,000 |
| WhatsApp @ ₹0.35 | ₹3,500 | ₹42,000 |
| Physical letter @ ₹25 | ₹2,50,000 | ₹30,00,000 |
| Agent call @ ₹40 | ₹4,00,000 | ₹48,00,000 |

Agent calls also carry a headcount limit that cannot be bought on short notice: 10,000
calls at ~5 minutes each is ~833 agent-hours, or five-plus full-time agents for a single
pass. **This is why the multi-channel formulation is load-bearing rather than decorative.**
Cheap channels are constrained by patience; expensive channels are constrained by money and
capacity. A multiple-choice knapsack over channels is the only formulation that expresses
both at once.

**(c) Externally imposed limits.** WhatsApp Business enforces per-account quality ratings
and messaging tiers; a poor rating throttles sending regardless of willingness to pay.
Indian commercial SMS is subject to TRAI/DLT registration of headers and templates and to
restrictions on promotional sending.
*The precise current limits under (c) are not yet verified and must be sourced in
[`calibration.md`](./calibration.md) before any of them is quoted.*

**(d) Organisational allocation.** In practice the messaging calendar is owned by a growth
or CRM team, and mandate re-consent receives an allocation rather than setting one. This is
not a technical constraint but it is the one that actually binds in production — and it is
the reason `θ` matters commercially. `θ` converts "we would like more sends" into "the
501st ask is worth ₹47 and the 800th is worth ₹4; give us 700."

### 5.3 What the budget does *not* cover

The budget applies only to **discretionary, intrusive contact**. It does not gate:

- **Zero-cost channels.** An in-app notification has `k[c] = 0` and does not consume budget.
- **The mandatory pre-debit notice.** RBI requires it, so it is being sent regardless. The
  re-consent call-to-action is piggybacked onto that notice
  ([`architecture.md`](./architecture.md), notice composer). This is a **free ask** — it
  consumes budget only in the fatigue term, not in the cost term.

A mandate that the allocator does not select is therefore still contacted. It is not
contacted through an expensive or intrusive channel.

---

## 6. Three things that are easy to get wrong

These are stated explicitly because each one, misread, changes the design.

### 6.1 "Not selected" means *not this week*, not *never*

The decision variable is `x[i,c,t]` — indexed by week. A per-week budget of `B` over a
12-week horizon supplies `12 × B` slots, and the allocator's job is to place each ask in
the week where it lands best, typically near the mandate's own hazard peak.

Asking a customer in week 1 when their risk concentrates in week 9 wastes the ask **and**
spends the fatigue budget that week 9 will need. `"ask later"` is a first-class output, not
an absence of output. A single-period formulation cannot represent it, which is why the
horizon is multi-period and why the Whittle arm exists at all.

### 6.2 Lapse and revocation are different endings, and only one is reversible

This is the deepest reason that contacting a probably-doomed mandate is not free.

| Outcome | Customer state | Recoverable later? |
|---|---|---|
| **Lapse** | Neutral; often unaware | **Frequently** — a later offer can re-acquire them |
| **Survive** | Positive | — |
| **Revocation** | Actively annoyed; may block the sender | **Rarely**, and the channel may be lost too |

Formally, `q[i] > r[i]`: post-lapse re-acquisition probability exceeds post-revocation
re-acquisition probability. The losses are therefore not equal:

```
loss on lapse       =  L[i] · (1 − q[i])
loss on revocation  =  L[i] · (1 − r[i])  +  α · R[i]
```

An ask that fails to save a mandate has not merely wasted an ask. It can **convert a soft
ending into a hard one** — a customer who would have lapsed quietly and could have been
won back instead cancels in irritation and cannot. The `α · R[i]` term captures the further
loss: on UPI AutoPay, a revocation costs the merchant the rail to that customer, which
carries option value across every future mandate, not just this one.

Modelling revocation as total loss (`r[i] = 0`) is the opposite error and makes the system
pathologically conservative. `L[i]` and `r[i]` are estimated separately for exactly this
reason (Pinterest, KDD 2018).

### 6.3 The at-risk population is heterogeneous — that is the entire premise

"At risk" means *uncertain*, not *doomed*. The book splits, illustratively, into three
groups with very different economics:

| Segment | Survives unasked | Uplift from asking | Backfire exposure | Action |
|---|---|---|---|---|
| Healthy | high | ≈ 0 — they renew anyway | **highest** — there is something to lose | do not ask |
| **Uncertain** | ~half | **highest** | moderate | **ask — this is the entire value** |
| Disengaged | very low | ≈ 0 — the message does not land | low | do not ask |

The intuition that a doomed mandate is a free lottery ticket assumes `u > 0` for the
disengaged segment. Empirically that assumption is weak: a customer who has stopped
engaging does not respond to another message either, so the expected prize is near zero
while the ticket still costs a slot, fatigue, and the 6.2 conversion risk.

Pinterest (KDD 2018) observed exactly this shape — an inverted U, with both the most active
and the most dormant users receiving the fewest messages. **If our allocator reproduces
that shape without being told to, it is independent validation of the value function; if it
does not, that needs an explanation.** The plot ships either way
([`eval.md`](./eval.md)).

The load-bearing consequence: **if every at-risk mandate were equally doomed, no risk model
would be needed at all** — spraying would be optimal by construction. The hazard model
exists precisely because the population is mixed, and its job is to separate these three
groups.

---

## 7. Irritation is a real, measurable, durable cost

The premise that over-contact carries lasting cost is not a vendor-blog assertion. Google
Chrome's quiet permission UI (USENIX Security 2021) is the strongest public evidence
available: over an A/B test on **~40M users and ~100M prompts**, the softer prompt cost
**less than 5% of grants** (10% → 9.8% desktop, 20.1% → 19.1% Android) while producing
**up to 30% fewer unnecessary actions on the prompts**.

> **Corrected 2026-09-02 (T5.7).** This paragraph previously said *"~300M users"* and
> *"reduced permanent denials by 17.5–31.4%"*. The paper reports neither figure. It was
> read on 2026-09-02, and [`prior_art.md` §2](./prior_art.md) quotes what it does say. The
> distinction that matters downstream: *fewer unnecessary actions on a prompt* is not the
> same claim as *fewer permanent refusals*, and `value.backfire_avoided_per_softer_step`
> was built on the second reading.

Two things follow, and both are load-bearing here:

1. Irritation is real, measurable, and **durable** — a permanent denial does not decay.
   This is the empirical basis for treating patience as a budget at all.
2. A cheaper, less intrusive channel converts slightly worse but avoids far more
   *permanent* refusal. Trying the cheap channel first is therefore not only a cost
   optimisation — it is a **mandate-preservation** strategy. This is what
   `value/channel_priors.py` encodes.

---

## 8. Objective

For each candidate `(mandate, channel, week)`, the net rupee value of asking:

```
V[i,c,t]  =    μ · u[i,c,t] · L[i]                          # value of a saved mandate
             − ν · b[i,c,t] · ( L[i]·(1 − r[i]) + α·R[i] )  # cost of a caused revocation
             − γ · 0.5^(d[i,t] / hl)                        # fatigue
             − ρ · 1[template reused]                       # template-reuse penalty
             − k[c]                                         # channel cost
```

Maximise `Σ V[i,c,t] · x[i,c,t]` subject to the constraints in Section 4.

Two notes on the form:

- `μ` and `ν` are **separate prices**, not a single netted number. Collapsing them
  under-prices the complaint side (LinkedIn, KDD 2016).
- The fatigue term makes the objective depend on *message form*, not just message count:
  reusing a template is penalised. This is what connects the LLM layer to the optimiser
  rather than leaving it decorative (Duolingo, KDD 2020). It also aligns with regulation —
  RBI's KYC directions require at least one physical channel per phase, so channel variation
  is a requirement, not a flourish.

---

## 9. Why existing systems do not solve this

Three gaps, established in the prior-art survey:

**Gap 1 — Everyone caps; nobody chooses.** Braze, Iterable, Klaviyo, Airship, OneSignal,
MoEngage and CleverTap all implement cap-and-drop, in processing or chronological order.
Which message gets dropped is decided by **arrival order, not value**. Only HCL Unica and
SAS Marketing Optimization genuinely allocate, and both are batch, legacy, and unavailable
in India.

**Gap 2 — Nobody prices a message in currency.** No product computes
`P(success)·value − P(churn)·LTV`. Klaviyo holds both churn risk and CLV on the customer
profile, and neither is wired to the cap. The data exists; it is not connected to the
decision.

**Gap 3 — Payments has zero controlled evidence.** There is no published controlled test of
how many dunning or re-consent messages cause cancellation. The entire "message fatigue"
narrative in payments is vendor content. **This is our contribution claim** — the
evaluation harness here is the first public artefact that even asks the question.

---

## 10. What we do not know

`u[i,c,t]` (uplift) and `b[i,c,t]` (backfire) are the two quantities that decide whether
selection beats spraying, and **neither has public measurement in payments** (Gap 3).

We therefore do not assert a point estimate. The evaluation sweeps the `(uplift × backfire)`
plane and reports the region in which selection beats a same-budget round-robin. The claim
the project makes is conditional and stated as such:

> *Selection wins above a backfire rate of approximately X%. Below it, spraying is the
> better policy — and we say so.*

That conditional claim is weaker-sounding and stronger in substance than any single number
would be, because in a field with no measurement, quoting one number is not honesty.

Netflix (2026) hand-tunes its message cost because opt-out data is too sparse to fit. That
is the benchmark being cleared here: Netflix, at its scale, guesses this parameter. We do
not guess it — we sweep it.

Remaining limitations, including the effect-size sanity check against Adyen's ~6% result,
are in [`limitations.md`](./limitations.md).
