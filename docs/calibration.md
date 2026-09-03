# Calibration — where every number comes from

The rule this document enforces: **no number anywhere in this repository is allowed to
exist without either a source, a measurement, or a `swept: true`.** Those three are the
only legitimate origins for a figure here.

* **Sourced** — a public figure, with the publication, the date, and the exact period it
  describes. §1 and §2.
* **Measured** — computed from the KKBox data by code in this repository, with the file
  that computes it named. §3.
* **Swept** — we do not know it, no public measurement exists, and `eval/sweep.py` varies
  it rather than asserting a value. §4.

Anything that fits none of those is in §5, which is the list of numbers this project uses
and could **not** verify. That section exists because a document like this is worthless if
it only records the successes.

Verification pass run 2026-08-29. Every URL below was fetched or returned by search on
that date. A **second pass on 2026-09-02** (T5.7) read the prior-art papers themselves and
moved two numbers — see [`prior_art.md`](./prior_art.md), and §5 and §6 below for what it
cost this project to find out.

---

## 1. The regulation

**RBI Circular RBI/DPSS/2026-27/396, 21 April 2026 — "Digital Payments – E-Mandate
Framework, 2026".** Verified: the circular exists, with that number and that date. It
consolidates and repeals eight of the RBI's recurring-transaction circulars issued between
August 2019 and August 2024, listed by number and date in its clause 11.

**Upgraded in T4.1 (2026-09-01): the circular text itself has now been read.** Until then
every row below came from three secondary law-firm summaries, and this section said so.
The text is retrieved from `rbi.org.in` and committed at
[`policy/sources/rbi-2026-04-21-e-mandate-framework.md`](../policy/sources/rbi-2026-04-21-e-mandate-framework.md),
with its SHA-256 pinned in `policy/mandate_policy.yaml` — every one of the twenty compiled
rules quotes it verbatim, and the loader refuses to start if a quote or the hash does not
match. What was *not* done is parsing the RBI PDF byte for byte; the committed text is an
HTML-to-markdown conversion cross-checked against a second reproduction, and that file
states the distinction in its own words.

| claim | figure | status |
|---|---|---|
| Pre-debit notification lead time | **at least 24 hours** before the debit | verified |
| Pre-debit notification contents | merchant name, amount, date and time of debit, transaction and e-mandate reference, reason for debit, grievance-redressal details | verified |
| AFA-free ceiling, general | **₹15,000** per transaction; above it, AFA applies | verified |
| AFA-free ceiling, insurance premiums / mutual fund subscriptions / credit-card bills | **₹1,00,000** per transaction | verified |
| FASTag and NCMC auto-replenishment | inside the e-mandate framework | verified |
| Transition period | none stated | **verified from the text** — clause 1(b) is "effective immediately" and clause 11 carries no savings or grandfathering provision |
| Re-registration of mandates predating the framework | not addressed | **the text is silent** — clause 10(b) covers card re-issuance only; see `limitations.md` |
| Applicability | cards / PPI / UPI, domestic **and cross-border** | verified — clause 2. eNACH is **not** in that list |
| Post-transaction notification | required, seven fields including grievance redressal | verified — clause 7, and it carries no FASTag/NCMC carve-out |
| Opt-out of a debit or a mandate | required, and the opt-out itself must be AFA-validated | verified — clause 6(c) |
| Charges to the customer for the facility | none permitted | verified — clause 10(a) |
| Velocity check | **no limit stated**, despite clause 8's heading naming one | verified absence — no rule compiled |

**What this pins in code.** `config/params.yaml`'s
`india.upi_autopay_afa_threshold_inr: 15000.0` is this circular's general AFA ceiling, not
a guess. The ₹1 lakh exemption is the source of the red-team case in T4.8. The 24-hour
lead time is what `agent/`'s deterministic compliance linter (T4.4) asserts on every
generated notice — a linter checking a rule nobody sourced would be theatre.

Sources: [Conventus Law](https://conventuslaw.com/report/rbis-digital-payments-e-mandate-framework-2026-consolidated-directions-for-recurring-digital-transactions/) ·
[AMLEGALS](https://amlegals.com/digital-payments-e-mandate-framework-2026-rbis-new-rules-for-auto-debit-transactions/) ·
[World Trade Scanner](https://worldtradescanner.com/RBI%20Issues%20Digital%20Payments%E2%80%93E-Mandate%20Framework%202026.htm)

T4.1 compiled the text into `policy/mandate_policy.yaml`: **20 rules across 17 clauses**,
each carrying a clause number and a verbatim quote. Run
`uv run python scripts/compile_policy.py --check` to regenerate that table from the files
themselves; it calls no model and needs no credential.

Two findings from the read that go against this project rather than for it, both now in
[`limitations.md`](./limitations.md):

* **Clause 2 does not cover eNACH**, which is 15% of `india.rail_mix`. That share of the
  modelled book sits outside the regulation whose arrival is this project's "why now".
* **Clause 6(a)'s pre-transaction notification is the *issuer's* obligation**, not the
  merchant's. A merchant-side allocator does not discharge it; clause 10(c) is the only
  sentence that reaches merchants, and it routes through their acquirer.

### 1.2 An adjacent obligation: KYC periodic updation

Not this system's own regulation -- it governs **KYC periodic updation**, not e-mandate
re-consent -- but it is what establishes that a channel ladder including a physical letter
is a requirement of regulated Indian contact rather than a modelling flourish.

**RBI (Know Your Customer) (Amendment) Directions, 2025**, dated 12 June 2025, to be
implemented by 1 January 2026:

| requirement | figure | status |
|---|---|---|
| Advance intimations before the due date | **at least three**, including **at least one by letter** | verified |
| Reminders after the due date | **at least three**, including **at least one by letter** | verified |
| Content of each communication | instructions, escalation mechanism, consequences of non-compliance | verified |
| Record of every intimation and reminder, per customer | required, for audit trail | verified |
| Periodic updation frequency | 2 years high-risk, 8 medium, 10 low | verified |

Two things this pins.

**The `letter` channel at INR 25 is load-bearing** (`config/params.yaml`, T3.1). A
regulator that mandates at least one letter per escalation phase is a regulator whose
world cannot be modelled with SMS alone, and an allocator that can only choose *whether*
to contact rather than *how* cannot express the constraint at all.

**The audit trail is the refusal ledger.** "Record the intimation sent to each customer in
their system" is the same requirement T5.1 was already designed around, arrived at from
the opposite direction. The ledger records not-asked decisions too, which is more than
this direction demands -- but the direction is why "we log what we sent" is a compliance
feature and not merely good engineering.

**What this is not.** It does not say anything about how often a merchant may contact a
customer about a *mandate*, and it must not be quoted as if it did.

### 1.3 A second regulator: what a notice may not say

Added 2026-09-02, for T4.4. This one is not the RBI at all, and that is the point: the
notice this system composes has to satisfy a payments regulator on its *contents* and a
consumer regulator on its *wording*, and nothing checks both unless something is built to.

**Guidelines for Prevention and Regulation of Dark Patterns, 2023**, notified by the
**Central Consumer Protection Authority** on **30 November 2023** under the Consumer
Protection Act, 2019. They prohibit the thirteen patterns listed in Annexure I and apply to
platforms systemically offering goods or services in India (foreign platforms included),
advertisers, and sellers/service providers — business-to-consumer only.

| pattern | the CCPA's own definition, abridged | why a pre-debit notice can commit it |
|---|---|---|
| Confirm shaming | emotionally charged design creating fear, shame, ridicule or guilt to guilt-trip a user into a transaction | a re-consent ask puts the guilt in the decline option; the CCPA's own illustration is an airline labelling it "I will stay unsecured" |
| False urgency | creating or implying a false sense of urgency or scarcity | the notice already carries a real deadline at least 24 hours out; anything beyond it is manufactured |
| Subscription trap | making cancellation impossible or complex, hiding the cancellation option, or making its instructions cumbersome | collides with RBI clause 6(c), which requires an opt-out from the debit *or* the mandate |
| Nagging | disrupting users through repetitive and persistent requests to effectuate a transaction | this project's own subject: `problem.md` §5.1 prices repeated asks as backfire, and the CCPA prices them as an unfair trade practice |
| Trick question | vague or confusing language, typically double negatives, misdirecting users | an opt-out sentence built on a double negative |
| Interface interference | highlighting or obscuring information to misdirect | in plain text, only the wording form is detectable |

**Six of the thirteen are checked.** Basket sneaking, bait and switch, drip pricing,
disguised advertisement, SaaS billing and rogue malware describe properties of a checkout
flow or a billing system rather than of a sentence, and a text linter claiming to check them
would be theatre.

**What is sourced and what is ours.** The patterns and their definitions are the
regulator's, and they are in `policy/dark_patterns.yaml` with the citation. The **phrase
lists** that detect them are this project's, they are not exhaustive, and no regulation
publishes such a list — that file says so in its own header, and `limitations.md` carries
the consequence: a dark pattern written in words nobody thought of passes the linter.

**One CCPA pattern is a direct hit on this project's own design.** *SaaS billing* —
"covertly billing users on a recurrent basis without any notifications" — is what the entire
pre-debit notice regime exists to prevent, and it means the RBI's clause 6 obligation and
the CCPA's consumer-protection regime are pointed at the same failure from two directions.
That is a good sentence for the pitch and it is not a number, so it lives here rather than
in a table.

Sources: [PIB, Ministry of Consumer Affairs, 30 November 2023](https://pib.gov.in/PressReleasePage.aspx?PRID=1983994) ·
[Trilegal, "Guidelines for Prevention and Regulation of Dark Patterns, 2023", 26 December 2023](https://trilegal.com/wp-content/uploads/2023/12/Guidelines-for-Prevention-and-Regulation-of-Dark-Patterns-2023.pdf)
— the definitions above are quoted from the second, whose Annexure-I summary was retrieved
and read on 2026-09-02. The CCPA notification itself was not parsed; same status as §1's
circular, and for the same reason.

Source: [RBI (Know Your Customer) (Amendment) Directions, 2025, 12 June
2025](https://pdicai.org/Docs/RBI-2025-26-51_1262025151347878.pdf) ·
[Saraf & Partners summary](https://sarafpartners.com/rbi-notifies-amendments-to-reserve-bank-of-india-know-your-customer-kyc-directions-2016/)

---

## 2. The market

### 2.1 UPI AutoPay volumes

**Every figure here is July 2025, not 2026.** The build plan carried them undated, which
would have let a 2026 pitch quote a 2025 number as current. They are the most recent
public figures found on 2026-08-29.

| figure | value | period |
|---|---:|---|
| New AutoPay mandate registrations | **50 million+** | July 2025 |
| — same figure a year earlier | 26 million | July 2024 |
| Mandate executions | **808 million** | July 2025 |
| Mandate revocations | **~20 million per month** | reported Sept 2025 |
| Business declines, top 50 remitter banks | **~74%** average | reported Sept 2025 |
| SBI auto-debit approval rate | **~30%** (so ~70% fail) | reported Sept 2025 |

The stated cause of the revocations is **debit-execution failure from insufficient balance
at the moment the mandate fires** — not customer intent. That detail is load-bearing for
this project: it means a large share of the 20M is a *recoverable* population, which is
what makes a retention system worth building rather than a resignation letter.

Source: [Business Standard, "UPI autopay revocations hit 20 mn per month on low customer
balance", Sept 2025](https://www.business-standard.com/finance/news/upi-autopay-revocations-hit-20-mn-monthly-over-low-customer-balances-125090700500_1.html)
(citing NPCI data and payments-industry sources).

**Verified as instructed.** `tasks.md` T1.10 said: "Verify the 20M/month figure yourself —
you will be asked about it." It is real, it is monthly, it is attributed to NPCI via
industry sources rather than published directly by NPCI, and it is a *revocation* count,
not a churn count. If asked, that last distinction is the answer: a revocation triggered by
an insufficient balance is not a customer who decided to leave.

### 2.2 The 2021 migration — the base rate for what happens next

| figure | value | date |
|---|---:|---|
| Recurring-payment decline at the peak | **~70%** | after 1 Oct 2021 |
| E-mandates registered in the first weeks | ~2 million | reported 26 Oct 2021 |
| Banks compliant | 29, covering ~70% of credit cards and ~50% of debit cards | Oct 2021 |
| Success rates across compliant banks | 30% to 75% | Oct 2021 |
| Mandates registered by the RBI's own count | **62.5 million+**, across domestic and 3,400+ international merchants | RBI, June 2022 |

**One correction to `problem.md`.** It says the 62.5 million took "about nine months". The
RBI's figure is from **June 2022**, which is about **eight months** after the 1 October
2021 enforcement date. The claim is directionally right and the number was rounded in the
wrong direction; `problem.md` §2 should read "about eight months".

Sources: [Inc42 on the ~70% decline](https://inc42.com/features/recurring-payment-conundrum-how-guidelines-have-shaken-indias-subscription-economy/) ·
[Business Standard, "Banks process nearly 2 mn e-mandates after RBI order", 26 Oct 2021](https://www.business-standard.com/article/finance/banks-process-nearly-2-mn-e-mandates-for-auto-payment-after-rbi-order-121102601557_1.html) ·
[Business Standard on the RBI's 62.5M figure and the ₹15,000 limit, 8 June 2022](https://www.business-standard.com/article/finance/new-e-mandate-guidelines-rbi-enhances-limit-for-e-mandates-on-credit-debit-cards-to-rs-15-000-122060800417_1.html)

---

## 3. What this project measured itself

These are not sourced from anywhere. They are computed from the KKBox data by code in this
repository, and the file that computes each one is named so a reader can re-derive it.

| parameter | value | measured by | written up in |
|---|---:|---|---|
| `recovery.after_lapse` (`q`) | **0.407 → 0.41** | `data/cancel.py::analyse_lapses` | [`mapping.md`](./mapping.md) §2.4 |
| `recovery.swept_ceiling_after_revocation` | **0.293 → 0.29** | `data/cancel.py::analyse` | §2.3 |
| Mandate book size | 1,392,175 (58.9% of subscribers) | `data/mandates.py::build_book` | §3.7 |
| Person-weeks in the frame | 58,079,041 over 1,379,341 spells | `data/periods.py::build` | §5.6 |
| Per-week death rate | 0.0130 | same | §5.6 |
| Baseline hazard, weeks 4-7 | 0.0740 (4.5x the average) | same | §5.7 |
| Hazard model Brier / log loss | 0.006817 / 0.0369 | `risk/hazard.py` | [`eval.md`](./eval.md) §2.4 |
| Expected calibration error | 0.00363 | `risk/calibration.py` | §3 |

**`q` is measured and it moved against this project's interests.** It replaced a
provisional 0.35, and 0.41 means lapsed mandates self-heal *more* often than the plan
assumed — so every saving figure downstream is smaller. That is recorded here because a
calibration document that only contains numbers that helped would not be a calibration
document.

**`r` is measured only as a ceiling.** KKBox's nearest analogue is recovery after an in-app
cancellation, at 0.293. Re-subscribing to a music app is one tap; a revoked UPI AutoPay
mandate needs a fresh mandate and a fresh bank authentication. So 0.293 bounds `r` from
above and does not measure it, and `r` stays swept over (0, 0.29].

---

## 4. Swept, because nobody knows

Every constant here is one where no public measurement exists and inventing one would be
the fabrication this document is designed to prevent. `eval/sweep.py` varies each of them
and the results are reported as a region, not a point.

| parameter | value in `params.yaml` | why it cannot be sourced |
|---|---:|---|
| `india.rail_mix` | 0.55 / 0.25 / 0.15 / 0.05 | KKBox never published what `payment_method_id` means; the rail is **assigned from a hash** (`mapping.md` §3.3) |
| `india.mandate_validity_days` | 730 | KKBox has no mandate-validity column at all |
| `india.reachability_fraction_of_ltv` | 0.15 | no public measurement exists for what an addressable channel to a customer is worth |
| `recovery.after_revocation` (`r`) | 0.08 | see §3; swept over (0, 0.29] |
| `value.mu_good_outcome`, `value.nu_complaint` | 1.0, 1.0 | LinkedIn (KDD 2016) establishes that the two prices must be *separate*, not what either is |
| `value.alpha_reachability` | 1.0 | Twitter/X (2022) establishes the term, not its magnitude |
| `value.gamma_fatigue`, `fatigue_half_life_days` | 25.0, 15 | Duolingo (KDD 2020) gives the functional form and an approximate half-life, not a rupee magnitude |
| `value.rho_template_reuse` | 5.0 | same |
| `channels[].cost_inr` | ₹0 to ₹40 | Indian channel rate cards, but not from one citable published table — see §5 |
| `intervention.backfire_first_ask` | 0.006 | **the constant the headline turns on**, and no public measurement exists for it. Added here 2026-09-02: `eval.md` §6.2 had been citing this section for it while this table carried no row, which `limitations.md` §2.1 caught |
| `intervention.backfire_twelfth_ask` | 0.06 | the far end of the same ladder. Ten-to-one against the first ask is a *shape* taken from `problem.md` §5.1, not a measured ratio |
| `intervention.uplift_scale` | 1.0 | how much of a death an ask averts. Swept from 0 to 2 in the T2.8 grid, and `results.md` §4's frontier is drawn in this parameter against the one above |
| `channels[].efficacy_prior` | 0.02 to 0.28 | **priors, not measurements**, and the sensitivity grid (T2.8) exists because of it |
| `value.backfire_avoided_per_softer_step` | 0.24 | **neither sourced nor swept.** Chosen under Chrome's published *"up to 30%"* ceiling -- the 17-31% range it was said to be the midpoint of does not exist (§5, corrected 2026-09-02) -- and then applied seven times, which is an extrapolation on top of a choice |

`india.ntd_to_inr: 1.0` is a separate case and is neither sourced nor swept: it is a
deliberate **decision** to read KKBox's price ladder (149/129/119/99 NTD) as India's
(149/129/119/99 INR), argued at length in `mapping.md` §3.4. It is not the exchange rate
and does not claim to be.

---

## 5. Numbers this project uses and could not verify

The honest half of the document.

**"Card post-2021 failure 20%+ in some categories."** Carried from the build plan. Not
found on 2026-08-29. Until it is sourced it must not appear in the pitch, the video, or
`problem.md`. The 2021 figures in §2.2 are verified and say enough.

**~~"No transition period" for the April 2026 framework.~~ SETTLED in T4.1 (2026-09-01).**
Kept here rather than deleted, because how it was settled is the point. The secondary
reporting never stated a transition window either way, and absence of a stated transition
is not the same as a stated absence. The circular text closes it: clause 1(b) reads "These
Directions shall be effective immediately", and clause 11's repeal of eight circulars
carries no savings clause. So the "why now" argument stands on the text.

What did **not** get settled, and is now its own line: the framework is *silent* on whether
mandates registered under the repealed circulars must be re-registered. Clause 10(b) is the
only sentence touching existing mandates and it covers card re-issuance. Silence is neither
permission nor requirement, and it stays open in `limitations.md`.

**LLM prices in `config/params.yaml` (`llm.price_*`).** $5.00 / $25.00 per million input /
output tokens for `claude-opus-5`, with cache reads at 0.1x input and cache writes at 1.25x.
These are Anthropic's published list rates as of 2026-09-01 and they are **not converted to
rupees** anywhere: no verified USD/INR rate exists in this repository, and `india.ntd_to_inr:
1.0` is a decision about a subscription price ladder rather than an exchange rate. Every
cost figure in `llm_eval.md` is therefore in USD, deliberately, and stands out against every
other number in this project for that reason.

**Channel cost table (`params.yaml` `channels[]`).** ₹0 in-app, ₹0.05 email, ₹0.15 SMS,
₹0.35 WhatsApp, ₹2 IVR, ₹25 letter, ₹40 agent call. These are plausible Indian rate-card
magnitudes and the *ordering* is not in doubt, but no single published table was found that
gives all seven. They are treated as swept in §4 rather than presented as sourced.

**~~Chrome's 17-31%~~, applied seven times. CORRECTED in T5.7 (2026-09-02).**
`value.backfire_avoided_per_softer_step: 0.24` was described here as *"the midpoint of a
genuinely published range: Chrome's quieter permission surface avoided 17-31% of permanent
refusals"*. **There is no published 17-31% range.** The paper says *"up to 30% fewer
unnecessary actions on the prompts"* — a single upper bound, and about *actions on prompts*
rather than permanent refusals. [`prior_art.md` §2](./prior_art.md) quotes the abstract and
shows the working.

So 0.24 is **a chosen value under a published ceiling**, not the midpoint of a published
range. It moves from "sourced" to "chosen", which is a weaker origin than this document has
been claiming for it, and it stays in this section rather than §1 for that reason. The
grant-loss half of the claim does survive: per-client grant rates went 10% → 9.8% on
desktop and 20.1% → 19.1% on Android, so "2-5% of grants lost" is a fair reading of the
paper's "less than 5%".

What is *still* not published, and was the original point of this paragraph: that the same
discount applies at every rung of a seven-channel ladder from an in-app nudge to an agent
call. Chrome measured **one** step between **two** UIs. Compounding it puts an email's
backfire at 0.76⁵ ≈ 25% of an agent call's, and **that extrapolation is load-bearing** — it
is what moved the shipped `(uplift, backfire)` point from the wrong side of `results.md`
§4's frontier to the right one. Before this project claims that asking pays at the shipped
parameters, this constant needs either a measurement or its own sweep axis. It currently
has neither, and now it does not have the provenance it was said to have either.

**`safety.max_model_age_days: 30`.** The age at which the hazard model is considered stale
and the system drops to the conservative floor. A decision, not a measurement: the KKBox
frame gives no basis for a drift half-life, and inventing one would be a number without an
origin. Thirty days is a plausible operational default and nothing more. What it triggers is
defended in `limitations.md` §8.11; what it rests on is this paragraph.

**Channel efficacy priors.** Not measurements of anything. They are priors, they are
labelled as priors in `params.yaml`, and T2.8's sensitivity grid is the entire reason the
project can carry them without claiming them.

**The prior-art results** — LinkedIn's volume / sessions / complaints triple, Pinterest's
inverted U, Chrome's ~40M-user quiet UI result, Duolingo's fatigue half-life, ARMMAN's
Whittle-index arm, Adyen's ~6% contextual-bandit lift — now live in
[`prior_art.md`](./prior_art.md), written 2026-09-02, which carries each one's exact claim,
its link, and whether the paper was actually read. They are not re-verified here; this
document covers the numbers this project *asserts*, and those are numbers it *cites*.

Two things that document found, because they belong in this section too:

* **The LinkedIn triple was misread, and this document quoted the misreading.** It said
  −64.5% volume / −1.8% sessions / −47% complaints. The paper's Table 3 reports *retained
  levels* against a send-all control of 100 — send 64.51, complaint 46.97, session 98.16 —
  so the deltas are **−35.5% / −1.8% / −53.0%**. Two of the three were the retained level
  read as a reduction; the third was computed correctly. `eval.md` §6 is the only consumer
  and the correction makes its mismatch **worse**, not better.
* **Four of the seven modelling sources are still unread** — Duolingo, ARMMAN, Adyen and
  Twitter/X. `prior_art.md` §7 marks each one, and this is what "cited" means for them
  today: the identity of the work is known and the specific figure is not verified.

---

## 6. What to fix

1. `problem.md` §2: "about nine months" → **"about eight months"** (§2.2).
2. `problem.md` §2: the UPI AutoPay volumes need the words **"July 2025"** attached, or a
   2026 reader will take them as current (§2.1).
3. Drop the unverified "card post-2021 failure 20%+" claim wherever it appears, or source
   it (§5).
4. ~~Settle the transition-period question from the circular text during T4.1 (§1).~~
   **Done, 2026-09-01.** Settled against the text; see §1 and §5.
5. ~~**Write [`prior_art.md`](./prior_art.md), or stop linking to it.**~~ **Done, 2026-09-02
   (T5.7).** Kept here because what it found is the point of the whole rule. The file had
   never existed while §5 above and [`problem.md`](./problem.md) both sent every prior-art
   number to it for "the exact claim and page reference", so the citation chain for six
   load-bearing figures terminated in a dead link and those figures reached the code from
   this project's own build plan. Writing it took under two hours and **two of the six
   numbers turned out to be wrong** — the LinkedIn triple and Chrome's
   "17-31%", both in §5 above. Both corrections make this project's position weaker, which is the
   direction that deserves the least suspicion. Four of the seven modelling sources remain
   unread and `prior_art.md` §7 says which.

6. **Measure or sweep `backfire_avoided_per_softer_step`.** Newly opened by item 5. The
   constant is compounded across seven channel rungs, it is what puts the shipped point on
   the profitable side of `results.md` §4's frontier, and as of the correction above it has
   neither a measurement, a sweep axis, nor the published range it was said to sit in the
   middle of. It is the largest unaddressed hole in this document.
