# Prior art — the exact claim, and whether this project has read it

Status: written 2026-09-02, T5.7 · Verification pass: 2026-09-02

This document existed as a link before it existed as a file. [`calibration.md`](./calibration.md)
§5 sent every prior-art number here for "the exact claim and page reference", and
[`problem.md`](./problem.md) linked to it too, so for the whole of Phases 1–4 the citation
chain for six load-bearing numbers terminated in a dead link. `calibration.md` §6 carried
that as job #5. This is that job.

**Two of the numbers this project has been quoting are wrong, and both were found by
reading the papers rather than by reasoning about them.** They are §1 and §2 below. Neither
is a transcription slip; both are a misreading of what a published table was reporting, and
both make this project's position *weaker* rather than stronger.

Every row in §7's table carries one of three statuses, and the difference is the point:

| status | meaning |
|---|---|
| **read** | the paper or its abstract was retrieved on the date shown and the number below is quoted from it |
| **partial** | the paper's identity is confirmed; the specific figure this project uses was not found in what was retrieved |
| **unread** | carried from this project's build plan. `CLAUDE.md` §3: a build plan is not a source |

---

## 1. LinkedIn — the triple this project checks itself against, corrected

**Rupesh Gupta, Guanfeng Liang, Hsiao-Ping Tseng, Ravi Kiran Holur Vijay, Xiaoyu Chen,
Rómer Rosales. "Email Volume Optimization at LinkedIn." KDD '16, pages 97–106.**
[DOI 10.1145/2939672.2939692](https://dl.acm.org/doi/10.1145/2939672.2939692) ·
[PDF](https://www.kdd.org/kdd2016/papers/files/adf0710-guptaA.pdf) · **read 2026-09-02**

Table 3, row `All`, columns headed **A/B Test (%)**:

| | send | complaint | session |
|---|---:|---:|---:|
| A/B test | **64.51** | **46.97** | **98.16** |

**Those are retained levels, not reductions.** The send-all control bucket is 100. Three
things in the paper make that unambiguous:

* the Constraint column reads *"no more than 60% of the maximum possible complaints"* and
  *"at least 98.5% of the maximum achievable sessions"* — ceilings and floors on a level;
* the A/B complaint figure 46.97 satisfies the ≤60 tolerance, and the session figure 98.16
  **misses** the ≥98.5 target;
* the paper says so in its own words: *"All constraints are being satisfied in the A/B test
  results, except for a minor violation of the global sessions constraint."*

So the deltas are:

| axis | this project has been quoting | **the paper** |
|---|---:|---:|
| email volume | −64.5% | **−35.5%** |
| downstream sessions | −1.8% | **−1.8%** (−1.84%) |
| complaints | −47% | **−53.0%** |

**The mechanism of the error is worth recording.** `64.51` and `46.97` were read as
reductions; `98.16` was correctly turned into `100 − 98.16 = 1.84`. The same table, read
two different ways, inside one triple — which is why the sessions figure survived the
correction and the other two did not.

**It makes this project's external check worse.** [`eval.md`](./eval.md) §6 compares this
allocator's shape against LinkedIn's and already fails on magnitude. Against the corrected
numbers the volume gap widens from 1.5× to **2.8×** — LinkedIn cut 35.5% of sends, this
allocator cuts 99.3% — and the complaints gap widens too. The one axis that agreed in
direction and magnitude, sessions, is unchanged; the sign flip on retention is unchanged.

**Also worth having, and not previously quoted here:** the paper's cost-benefit experiment
(§3, Table 1) randomly dropped about half of all emails for a bucket of members and measured
**−2.6% total page views** against the send-all bucket, with the loss concentrated in Jobs
(−4%), Profile (−4.5%), PYMK (−4.5%) and Search (−4%). That is the cleanest published
statement of how little a marginal notification is worth, and it is a better anchor for
`problem.md` §5 than the optimiser triple is.

---

## 2. Chrome — the constant that moved the result, and the range that is not in the paper

**Igor Bilogrevic, Balazs Engedy, Jud Porter, Nina Taft, Kamila Hasanbega, Andrew
Paseltiner, Hwi Lee, Edward Jung, Meggyn Watkins, PJ McLachlan, Jason James. "'Shhh...be
Quiet!' Reducing the Unwanted Interruptions of Notification Permission Prompts on Chrome."
30th USENIX Security Symposium (USENIX Security '21).**
[Google Research](https://research.google/pubs/pub49767) ·
[USENIX](https://www.usenix.org/conference/usenixsecurity21/presentation/bilogrevic) ·
**read 2026-09-02 (abstract and reported figures; the PDF returned 403)**

What the abstract states, verbatim:

> "...an A/B test using behavioral data from more than 40 million users who interacted with
> more than 100 million prompts on more than 70 thousand websites, show that the new UI is
> very effective at reducing the unwanted interruptions and their frequency (**up to 30%
> fewer unnecessary actions on the prompts**), with a **minimal impact (less than 5%) on the
> grant rates**, across all types of users and websites."

And on the baseline: *"74% of all permission prompts are about notifications... only a 10%
grant rate on desktop and 21% grant rate on Android."* Per-client average grant rates in the
A/B were 10% → 9.8% on desktop and 20.1% → 19.1% on Android.

**The correction.** `config/params.yaml` carries
`value.backfire_avoided_per_softer_step: 0.24`, and `calibration.md` §5 describes it as *"the
midpoint of the published 17–31%"*.

**There is no published 17–31% range.** The paper says *up to 30%*, a single upper bound, and
it says it about *"unnecessary actions on the prompts"* — not about permanent refusals. The
grant-loss half of the project's claim does hold: 10 → 9.8 is a 2% relative drop and 20.1 →
19.1 is about 5%, so "2–5% of grants lost" is a fair reading of "less than 5%".

So 0.24 is **a chosen value below a published ceiling**, not the midpoint of a published
range. That is a weaker provenance than the one this project has been claiming, and it makes
`limitations.md` §2.2 — which already says the seven-fold compounding of this constant is
load-bearing and unmeasured — more pointed rather than less: the thing being compounded was
not what it was said to be either.

**What the paper does not support at all**, and what `limitations.md` §2.2 already flags: it
measured **one** step between **two** UIs. Applying a Chrome-sized discount at every rung of a
seven-channel ladder is this project's extrapolation.

---

## 3. Pinterest — the inverted U

**Bo Zhao, Koichiro Narita, Burkay Orten, John Egan. "Notification Volume Control and
Optimization System at Pinterest." KDD '18.**
[DOI 10.1145/3219819.3219906](https://dl.acm.org/doi/10.1145/3219819.3219906) ·
**partial, 2026-09-02**

Identity, venue and year confirmed. This project uses the paper for its *shape* claim —
that engagement against notification volume is an inverted U, so both too few and too many
are worse than the peak — and that claim is what the paper's title and abstract describe. The
specific curve and its peak were **not** retrieved, and `eval.md`'s segment plot (T3.7) does
not quote a number from it.

Note the venue: **KDD 2018**, not 2016. `problem.md` and `tasks.md` do not date it, which is
the safe form; anything that dates it should say 2018.

---

## 4. Duolingo — fatigue, and a half-life this project chose

**KDD 2020, Duolingo's notification/scheduling work.** **unread.**

`config/params.yaml` carries `value.fatigue_half_life_days: 15` and
`value.rho_template_reuse: 5.0` with "Duolingo KDD 2020" beside them. Neither number was
found in a retrieved paper during this pass, and neither is quoted from one here. They are
**swept** parameters (`calibration.md` §4), which is the correct status for them, and the
attribution in the YAML comment overstates what has been read. Until someone reads the paper,
the comment should say *"the idea of a fatigue half-life comes from Duolingo's notification
work; the value is ours and is swept."*

---

## 5. ARMMAN — the Whittle index arm

**Restless multi-armed bandits for maternal and child health, AAAI 2022 (ARMMAN /
Google Research India).** **unread.**

Used as the precedent for P5 (`allocator/whittle.py`, T3.8): a Whittle-index policy on a
restless bandit, deployed on a real intervention-scheduling problem at scale. This project
cites it for the *approach*, not for a number, and `results.md` §2 reports P5's own measured
performance rather than ARMMAN's. That is the honest use of an unread citation — but it is
still unread.

---

## 6. Adyen — the sanity ceiling

**Adyen, contextual bandit versus a fixed retry schedule, ~6% improvement.** **unread.**

This is the single most consequential unread number in the project, because
[`limitations.md`](./limitations.md) §1 turns it into a rule — *a simulator reporting a 40%
lift has found a bug, not a result* — and `results.md` §5 generates a verdict against it on
every CI run.

The rule survives the citation being weak, because it is used as an **order-of-magnitude
ceiling** rather than as a target: any published A/B lift in payments retry optimisation is
single-digit percent, and the rule only needs that much to do its work. But the specific
"~6%" should not appear in the pitch or the video as a citation until somebody has read it.

**Twitter/X 2022, reachability as a separate asset** (`value.alpha_reachability`) is in the
same position: **unread**, used for a modelling idea rather than a number, and the number it
governs is swept.

---

## 7. The table

| # | system | venue | what this project takes from it | status | date |
|---|---|---|---|---|---|
| 1 | LinkedIn email volume | KDD '16 | the three-axis shape check (`eval.md` §6) — **corrected here** | **read** | 2026-09-02 |
| 2 | Chrome quiet permission UI | USENIX Sec '21 | `backfire_avoided_per_softer_step` — **provenance corrected here** | **read** | 2026-09-02 |
| 3 | Pinterest volume control | KDD '18 | the inverted-U shape (T3.7) | partial | 2026-09-02 |
| 4 | Duolingo notifications | KDD '20 | the idea of a fatigue half-life | unread | — |
| 5 | ARMMAN restless bandits | AAAI '22 | the Whittle-index arm, P5 | unread | — |
| 6 | Adyen retry optimisation | industry | the ~6% sanity ceiling (`limitations.md` §1) | unread | — |
| 7 | Twitter/X reachability | industry, 2022 | reachability as a separate asset | unread | — |
| 8 | RBI e-mandate framework | RBI, 2026 | the 20 compiled rules | **read** — [`policy/sources/`](../policy/sources/rbi-2026-04-21-e-mandate-framework.md) | 2026-09-01 |
| 9 | RBI KYC (Amendment) Directions | RBI, 2025 | why the channel ladder needs a letter | read (secondary) | 2026-08-29 |
| 10 | CCPA dark patterns guidelines | CCPA, 2023 | the six patterns the notice linter checks | read (secondary) | 2026-09-02 |

Rows 8–10 are the regulatory sources and are documented in [`calibration.md`](./calibration.md)
§1, §1.2 and §1.3. Rows 1–7 are the modelling prior art, and **four of the seven are
unread.**

---

## 8. What this document is for

Not completeness. A hackathon does not get to read seven papers properly, and pretending
otherwise is the failure this file was created to fix — the previous state was *worse* than
an incomplete bibliography, because a link to a document that does not exist reads exactly
like a citation that does.

What it is for is **making the status of each number visible where the number is used.** Two
were wrong. Both were found within an hour of actually opening the papers, and both had
survived four phases of a project that is otherwise unusually careful about provenance —
which is the strongest possible argument for the rule that produced this file.

Related: [`calibration.md`](./calibration.md) — every constant and its origin ·
[`limitations.md`](./limitations.md) §2 — the four things the rupee numbers rest on ·
[`eval.md`](./eval.md) §6 — the check these corrections change.
