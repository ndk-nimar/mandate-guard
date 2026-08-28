# Data mapping

How the KKBox WSDM 2018 competition data becomes the mandate portfolio this system
reasons about. Sections 1 and 2 are finished (T1.1, T1.2). Section 3 is an open
question with the evidence needed to answer it, not an answer.

Source: `kkbox-churn-prediction-challenge`. Fetched with `scripts/fetch_data.py`
(four files, ~1 GB; `user_logs*` is deliberately not downloaded -- this system models
mandates, not listening behaviour).

---

## 1. Ingestion (T1.1) -- done

`scripts/ingest.py` runs DuckDB directly over the CSVs and writes typed parquet to
`data/interim/`. Full run: **1 minute 23 seconds**, bounded memory.

Ingestion is allowed to choose types, parse `YYYYMMDD` integers into dates, and turn
empty strings into NULL. It is not allowed to drop rows, merge tables, or interpret a
column's meaning -- those decisions live in sections 2 and 3 and must be argued here
before they reach code.

### Row counts

| table | rows | distinct `msno` | parquet |
|---|---:|---:|---:|
| `transactions` | 21,547,746 | 2,363,626 | 761.4 MB |
| `transactions_v2` | 1,431,009 | 1,197,050 | 52.7 MB |
| `members` | 6,769,473 | 6,769,473 | 236.3 MB |
| `labels` (`train_v2`) | 970,960 | 970,960 | 32.3 MB |

### Date ranges

| table | column | from | to | unparsed |
|---|---|---|---|---:|
| `transactions` | `transaction_date` | 2015-01-01 | 2017-02-28 | 0 |
| `transactions` | `membership_expire_date` | 1970-01-01 | 2017-03-31 | 0 |
| `transactions_v2` | `transaction_date` | 2015-01-01 | 2017-03-31 | 0 |
| `transactions_v2` | `membership_expire_date` | 2016-04-19 | 2036-10-15 | 0 |
| `members` | `registration_init_time` | 2004-03-26 | 2017-04-29 | 0 |

Zero unparsed dates: every `YYYYMMDD` integer in all four files is a real calendar date.
That rules out one class of silent corruption before any model is fit.

### What the counts already tell us

**The three tables cover different populations.** `members` holds 6.77M subscribers,
`transactions` only 2.36M, and `labels` only 0.97M. Any join is therefore a filter, and
the direction of that filter changes who the model is fit on. Whichever join T1.4 uses
must be stated in this document with its surviving row count.

**`membership_expire_date` reaches back to 1970-01-01.** That is the Unix epoch, which
is what a system writes when it has no date to write. It is a missing value wearing a
date's clothes. Every downstream "days until expiry" calculation must exclude these, or
it will compute a mandate that expired 45 years before it was created.

**`transactions_v2` expiry reaches 2036-10-15.** Nineteen years past the data's own
horizon. Either a data-entry artifact or a genuine long plan; the two are not
distinguishable from this column alone and the volume needs measuring before T1.4.

Both are counted in section 2.7. Both turned out to be small, and -- more usefully --
they live in *different tables*.

---

## 2. `is_cancel` semantics (T1.2) -- done

Reproduce with `uv run python scripts/analyse_cancel.py` (~40 s over the full
21.5M rows). Code: `src/mandateguard/data/cancel.py`. Definitions are pinned by
`tests/test_cancel.py`, which runs on a 20-row fixture rather than the download.

### 2.1 The question changed once the data answered it

The task was written as "how many `is_cancel = 1` rows are followed by another
transaction", on the assumption that this measures `q` --
`recovery.after_lapse`, the rate at which a **lapsed** mandate comes back by itself.
It does not. The first run produced the number and then invalidated the question:

| `is_cancel` | `is_auto_renew` | rows |
|---|---|---:|
| 0 | 0 | 3,189,786 |
| 0 | 1 | 17,501,109 |
| 1 | 0 | **10** |
| 1 | 1 | **856,841** |

A KKBox cancellation is, in 99.9988% of cases, a customer switching off a live
auto-renewing subscription. That is an **active** death -- the analogue of a mandate
*revocation* (`r`), not of a passive *lapse* (`q`). Writing it into
`recovery.after_lapse` would have put a revocation measurement in the lapse slot.

The `is_auto_renew` split the task asked for is therefore not a useful segmentation:
one side of it has ten rows. It is reported anyway, because "we looked and the split is
degenerate" is a finding, and a reader who does not see it will ask for it.

So this section measures **both** deaths separately.

### 2.2 Definitions

Each choice below moves the number, so each is named rather than assumed:

* **Unit is a cancel *event*, not a row** -- rows collapse to `(msno, day)`. 856,851
  rows are 853,944 cancels.
* **"Came back" means a later `is_cancel = 0` transaction.** A cancel following a
  cancel is not a recovery; only a non-cancel row represents money moving.
* **Grace, 1 day.** A cancel-and-repurchase inside a day is one administrative act (an
  upgrade, a card swap), not a lapse. 22,632 events (2.7%) are this, and they are
  excluded from recovery and reported separately.
* **Right-censoring.** An event needs a full window of observation before the horizon
  (`2017-02-28`) to be counted. Events without it are dropped, not counted as failures
  -- a subscriber who cancels five days before the data ends has not "failed to recover
  in twelve weeks", they have not been observed.
* **Windows are week multiples** -- 1, 4, 12, 24 weeks. 84 days is the headline because
  it equals `horizon.weeks: 12`: a recovery the evaluation harness never lives long
  enough to see must not be baked into a parameter the harness consumes.

### 2.3 Active death -- recovery after a cancellation

| window | eligible | censored | recovered | rate | after coverage ended |
|---:|---:|---:|---:|---:|---:|
| 7d | 846,844 | 7,100 | 47,153 | 0.056 | 44,152 (0.052) |
| 28d | 823,435 | 30,509 | 99,222 | 0.120 | 88,087 (0.107) |
| **84d** | 772,139 | 81,805 | 245,782 | **0.318** | 226,255 (**0.293**) |
| 168d | 698,317 | 155,627 | 231,987 | 0.332 | 213,598 (0.306) |

853,944 cancel events across 768,456 subscribers; 528,867 of those events are followed
by no purchase ever again.

The last column is the stricter number. On cancellation KKBox usually ends coverage
immediately (455,685 rows set expiry to the cancel date, 147,200 backdate it) but 30%
cancel at period end and keep coverage running. A subscriber who cancels at period end
and repurchases before that period expires **recovered without ever losing service** --
a save, not a win-back. Counting those would let a retention system claim credit for
customers who never left, so they are separated out. `r`'s ceiling comes from the
strict column: **0.293**.

### 2.4 Passive death -- recovery after coverage ran out (`q`)

Coverage is reconstructed per subscriber: each transaction restates
`membership_expire_date`, so the latest one is the current end of coverage. A gap is a
subscriber-day whose next transaction lands more than the renewal tolerance after
coverage ended. Coverage still live at the horizon is **not** a gap -- those
subscribers are alive, not lost.

2,374,371 coverage gaps in total. 629,517 follow a cancellation (§2.3's population) and
are excluded here, leaving **1,744,854 passive lapses across 1,183,761 subscribers**.

| window | eligible | censored | recovered | rate (`q`) |
|---:|---:|---:|---:|---:|
| 7d | 1,720,515 | 24,339 | 0 | 0.000 |
| 28d | 1,667,022 | 77,832 | 332,311 | 0.199 |
| **84d** | 1,568,023 | 176,831 | 638,931 | **0.407** |
| 168d | 1,198,791 | 546,063 | 716,420 | 0.598 |

The 7-day row is 0.000 *by construction*, not as a finding: with a 7-day tolerance no
gap shorter than 7 days exists, so none can close inside 7 days. It is left in the
table because deleting a structurally-zero row invites someone to re-derive it later
and misread it as a measurement.

### 2.5 The tolerance is a decision, and it is the load-bearing one

`q` is not a fact about the data. It is a fact about the data *under a definition*, and
this definition moves it by a factor of three:

| tolerance | passive lapses | q@7d | q@28d | q@84d | q@168d |
|---:|---:|---:|---:|---:|---:|
| 0d | 4,880,619 | 0.644 | 0.713 | 0.782 | 0.861 |
| 1d | 2,511,642 | 0.307 | 0.446 | 0.587 | 0.730 |
| 3d | 2,022,698 | 0.139 | 0.311 | 0.489 | 0.659 |
| **7d** | **1,744,854** | 0.000 | 0.199 | **0.407** | 0.598 |
| 14d | 1,570,676 | 0.000 | 0.108 | 0.341 | 0.544 |
| 30d | 1,373,487 | 0.000 | 0.000 | 0.240 | 0.458 |

Zero tolerance is clearly wrong -- it turns 4.88M ordinary slow settlements into
churn-and-win-back events. Beyond that the curve has no natural kink to pick, so the
choice has to come from the system rather than the data.

**7 days is chosen because it is this system's own decision cadence.** The harness
hands the policy one budget per week; a gap that opens and closes inside a single week
is one the policy could not have acted on even with perfect foresight. A lapse the
system cannot act on is not a lapse the system should be calibrated against.

### 2.6 What this changed in `config/params.yaml`

| parameter | was | now | basis |
|---|---|---|---|
| `recovery.after_lapse` (`q`) | 0.35 provisional | **0.41** | 638,931 / 1,568,023 measured |
| `recovery.after_revocation` (`r`) | 0.08, swept over (0,1) | 0.08, swept over **(0, 0.29]** | ceiling measured |

Two consequences worth stating plainly:

**`q` moved against this project's interests.** Lapsed mandates self-heal 41% of the
time, not 35%, so the value of intervening on them is *smaller* than the provisional
number claimed. Every saving figure downstream shrinks accordingly.

**`q > r` is now measured, not asserted.** `docs/problem.md` 6.2 argues that a
deliberately-killed mandate is harder to win back than one that merely expired, and
`models.Mandate` enforces it. On this data 0.407 > 0.293 holds with a wide margin, on
1.57M and 772k independently-defined events. The invariant is no longer a modelling
assumption the code merely refuses to violate.

`r` itself stays `swept: true`. Re-subscribing to a music app is one tap; a revoked UPI
AutoPay mandate needs a fresh mandate and a fresh bank authentication. 0.293 is
therefore a ceiling on `r`, not a measurement of it -- but a data-backed ceiling is a
strictly better sweep range than the open `(0, 1)` it replaces.
`policy/loader.py` enforces both the ordering and the ceiling at load time, so a later
hand-edit to the YAML fails immediately instead of three layers downstream.

### 2.7 Sentinels

| table | rows | epoch expiry | far-future expiry | latest expiry |
|---|---:|---:|---:|---|
| `transactions` | 21,547,746 | 1,776 (0.008%) | 0 | 2017-03-31 |
| `transactions_v2` | 1,431,009 | 0 | 9,718 (0.679%) | 2036-10-15 |

Both flagged after T1.1 turn out to be small, and they are in different tables. The
1970-01-01 expiry is 1,776 rows of `transactions` -- rare enough to exclude rather than
model, and the lapse timeline already skips them (`IGNORE NULLS`) so a subscriber never
appears to have lapsed 46 years before subscribing. The far-future expiries are 0.68%
of `transactions_v2` only, so **the 2036 dates are not a property of the data, they are
a property of `transactions_v2`** -- which is another reason not to merge the two tables
casually in T1.3.

### 2.8 Known limits of these numbers

* **`transactions_v2` is not used for follow-up.** It runs one month past
  `transactions`, so a recovery in March 2017 is invisible here. Merging the tables is
  a T1.3 decision and this section refuses to pre-empt it; the cost is that late
  recoveries are censored slightly more aggressively than strictly necessary.
* **Censoring is not random.** Dropping events within one window of the horizon drops
  later cohorts preferentially. If recovery behaviour drifted over 2015-2017, these
  rates lean toward earlier cohorts. Not corrected; noted.
* **A lapse is classified passive if no cancel appears on the last transaction day
  before the gap.** A subscriber who cancelled, renewed anyway, and then lapsed months
  later counts as passive. That is the intended reading, but it is a rule, not a fact.
* **KKBox is a Taiwanese music service, not an Indian mandate book.** Everything here
  is a proxy, and §3 is where that bridge gets argued.

---

## 3. India mapping layer (T1.3) -- open

KKBox is a Taiwanese music service in 2017. This system is about Indian recurring
mandates in 2026. Every bridging decision belongs here, in prose, with its reasoning:

- `payment_method_id` -> rail (UPI AutoPay / card / PPI / e-NACH)
- NTD -> INR
- `payment_plan_days` -> debit frequency
- `membership_expire_date` -> mandate validity
- `bd` (age): the column contains impossible values and ingestion preserves them
  deliberately. What to do about them is decided here, not silently upstream.

Until this section is written, `data/processed/mandates.parquet` must not exist.
