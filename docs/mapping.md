# Data mapping

How the KKBox WSDM 2018 competition data becomes the mandate portfolio this system
reasons about. Sections 1 and 2 are measurements (T1.1, T1.2): the data is asked a
question and answers it. Section 3 is different in kind (T1.3) -- it is a chain of
*decisions* about data that cannot answer, so each one carries its alternative and the
cost of being wrong. The output is `data/processed/mandates.parquet`.

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

## 3. India mapping layer (T1.3) -- done

KKBox is a Taiwanese music service in 2017. This system is about Indian recurring
mandates in 2026. Everything in this section is that bridge, and **the bridge is made of
decisions, not measurements.** Sections 1 and 2 could point at a number in the data and
say "this is what it is". Section 3 mostly cannot, so its job is instead to say, for each
decision: what was chosen, what the alternative was, and what it would cost to be wrong.

Reproduce with `uv run python scripts/build_mandates.py` (**9 minutes 12 seconds** over
the full 21.5M transaction rows; `--dry-run` reports the same counts without writing).
Code: `src/mandateguard/data/mandates.py`. Definitions are pinned by
`tests/test_mandates.py`, which runs on a 23-row fixture rather than the download.

### 3.1 What is recovered, and what is invented

The single most important thing a reader needs from this section is which columns are
facts about KKBox subscribers and which are things this project made up:

| field | source | honest label |
|---|---|---|
| `amount_inr`, `debit_frequency_days`, `current_end`, `status`, `tenure_days`, `transactions`, `lifetime_paid_inr` | the transaction log | **recovered** -- billing facts, nothing invented |
| `city`, `registered_via`, `gender`, `age_years` | `members` | **recovered**, with known gaps (3.6, 3.7) |
| `recovery_after_lapse` (q), `recovery_after_revocation` (r) | T1.2 measurement, copied onto every row | **measured elsewhere** (2.3, 2.4) |
| `method` (the rail) | a hash of `msno` against a configured mix | **assigned** -- synthetic, see 3.3 |
| `expire_by` | `current_end` + a configured constant | **overlay** -- KKBox has no such column |
| `reachability_value_inr` (R) | a configured fraction of L | **overlay**, `swept: true` |
| `ltv_remaining_inr` (L) | amount x horizon / cycle | **derived**, see 3.8 |

Three of these are inventions. They are kept because the system cannot be demonstrated
without them -- a mandate with no rail cannot be reasoned about under rail-specific
rules -- and each is `swept: true` so that no headline result rests on a particular value
of it. What is *not* acceptable is an invention that reads like a measurement, which is
why this table exists before any number does.

### 3.2 The snapshot, and why `transactions_v2` stays out

**Decision: the book is built as of `2017-02-28`, from `transactions` alone.**

A mandate is a standing authorisation, so the unit is the subscriber and its state is
whatever the subscriber's most recent transaction *at or before the snapshot* says.
Transactions after the snapshot are not read at all. That is not tidiness: reading them
would be look-ahead, and the book would know outcomes the policy could not have known.

`2017-02-28` is the last `transaction_date` in `transactions` (section 1).
`transactions_v2` runs one month further, to `2017-03-31`, and merging the two was left
open by section 2.8. It stays out, for three reasons that compound:

1. **The two tables behave differently.** Section 2.7 measured it: the 1970-01-01 epoch
   expiry appears only in `transactions` (1,776 rows), and the 2036 far-future expiry
   appears only in `transactions_v2` (9,718 rows, 0.68%). Two tables with different
   corruption profiles are not one table.
2. **`transactions_v2` is a different population.** 1.43M rows across 1.20M subscribers
   -- 1.2 rows per subscriber, against 9.1 in `transactions`. It is a competition-period
   slice, not a continuation of the same log.
3. **It is worth more held back than merged.** March 2017 is the only month of data this
   project has never fit anything on. Kept out, it is out-of-time validation for T2.x;
   merged, it is one extra month of training data and the validation set is gone.

**Cost of being wrong:** the book loses one month of freshness, and any mandate whose
only March activity would have changed its status is stale by up to 31 days.
**Reversible:** yes -- one config line (`snapshot_date`) plus adding the second file to
the source view. **When to revisit:** if T2.x needs more recent labels than an
out-of-time validation month is worth.

### 3.3 `payment_method_id` -> rail: assigned, not decoded

**Decision: the rail is assigned deterministically from a hash of `msno`, to match a
configured mix. It is synthetic and labelled as such everywhere it surfaces.**

`payment_method_id` holds ~40 opaque integers. KKBox never published a legend, and no
public source decodes them. Three options existed:

| option | benefit | downside |
|---|---|---|
| **Invent a legend** ("method 41 = UPI AutoPay") | rails look data-derived; per-rail results read as findings | it is a fabrication that *reads like* a measurement -- the worst of the three, because a later reader cannot tell |
| **Drop rails entirely** | nothing is invented at all | kills the rail-specific policy rules, which are half the point of the system |
| **Assign to a configured mix** (chosen) | rails exist, the mix is one visible knob, and its synthetic status is stated in the schema, the report, and this table | the mix is a guess; no per-rail claim is evidence about KKBox |

The mix (`config/params.yaml`, `india.rail_mix`) is 0.55 UPI AutoPay / 0.25 card / 0.15
e-NACH / 0.05 PPI -- roughly the shape of Indian recurring-payment volume, and
`swept: true`. `policy/loader.py` rejects a mix that does not sum to 1 at load time,
because a mix summing to 0.9 would silently pile the remainder onto the last rail in the
ladder and nothing downstream would notice.

Assignment is by `hash(msno) % 100000`, not by a random number generator: an RNG would
need its state threaded through every caller to stay reproducible, whereas a hash of the
key is stable by construction. The realised mix on 1.39M mandates is 0.550 / 0.250 /
0.150 / 0.050 -- exact to three places.

One rule rides on top: **UPI AutoPay is withheld above
`india.upi_autopay_afa_threshold_inr` (15,000)**, because UPI AutoPay requires
additional-factor authentication above a per-debit ceiling, and the remaining rails are
renormalised over what is left. At `ntd_to_inr: 1.0` the largest per-debit amount in the
book is **210**, so this rule binds on zero mandates today. It is kept because
`ntd_to_inr` is a knob, and `tests/test_mandates.py` lowers the threshold to force the
branch to bind -- a rule that only holds at today's scale is a bug waiting for someone to
change the scale.

**Cost of being wrong:** every per-rail number in this project describes the assumed mix,
not reality. **Not reversible into truth** -- no amount of work recovers the real legend
from this data. What is reversible is the mix itself.

### 3.4 NTD -> INR: deliberately 1.0, which is not the exchange rate

**Decision: `ntd_to_inr: 1.0`. The NTD numeral is read as rupees.**

The real rate is roughly 2.7 INR per TWD. Using it is the obvious choice and it is wrong
here, because of what the two price ladders look like side by side:

| KKBox (NTD) | at FX ~2.7 (INR) | actual Indian music subscriptions (INR) |
|---:|---:|---|
| 149 | ~402 | Spotify Individual 119, YouTube Premium 129 |
| 129 | ~348 | JioSaavn Pro 99, Gaana Plus 99 |
| 99 | ~267 | Amazon Music 99, Apple Music 99 |

At the FX rate every plan becomes a ~400/month music subscription, which no Indian
service charges, and every rupee figure downstream becomes implausible on sight. Taken as
a numeral, the ladder lands directly on real Indian prices.

The reason this is defensible rather than a fudge: **it is a uniform linear scale on
every rupee quantity derived from the data.** Multiply L and R by any constant and the
allocator's ranking is unchanged and the budget curve keeps its shape; only the headline
moves. `tests/test_mandates.py::test_the_ntd_to_inr_scale_is_uniform` pins exactly that.

The honest caveat, and it is not a small one: **channel costs do not scale with it.** An
SMS costs INR 0.15 whatever the FX rate is. So `ntd_to_inr` does move the ratio between
what a mandate is worth and what contacting it costs -- and that ratio is the entire
economics of the allocator. This constant is therefore not cosmetic. It is called out
here rather than buried, and it is a sweep axis for that reason.

**Cost of being wrong:** every rupee headline is off by the factor, and the
value-to-cost ratio with it, which moves how many mandates are worth contacting.
**Reversible:** yes, one config line and a rebuild.

### 3.5 `payment_plan_days` -> debit frequency

**Decision: use the stated plan length; when it is 0 or missing, impute, and flag every
imputation in a `frequency_imputed` column.**

`payment_plan_days` is 0 on 870,124 transaction rows (4%), 93% of which are
auto-renewing and paid. A zero-day billing cycle that renews and collects money is not a
plan, it is a missing field. The fallback chain is:

1. the stated `payment_plan_days`, if non-zero;
2. else the subscriber's own **modal** non-zero plan length -- their history knows their
   cycle better than any global default does;
3. else the observed expiry span (`membership_expire_date - transaction_date`), if it is
   between 1 and 400 days;
4. else `india.default_debit_frequency_days` (30).

Step 3's 400-day bound is why step 4 is reached at all: `default_cycle` in the test
fixture buys coverage to 2036, and a 19-year "billing cycle" would price that mandate at
one debit every two decades. The bound applies to *imputation only* -- a genuinely stated
410-day plan is used as stated, and exactly one mandate in the book has one.

**Result: 5,672 of 1,391,931 mandates (0.4%) have an imputed cycle.** The 4% figure on
raw rows collapses to 0.4% on mandates because the zero-day rows cluster in histories
that also contain stated plan lengths -- step 2 catches almost all of them. 1,378,359
mandates (99.0%) are on a 30-day cycle.

**Cost of being wrong:** L is `amount x horizon / cycle`, so a cycle wrong by a factor of
two makes L wrong by a factor of two for that mandate. Confined to 0.4% of the book, and
that 0.4% is queryable rather than invisible, which is the point of the flag.

### 3.6 `bd` (age): nulled, not repaired, and not used

**Decision: `bd` outside [13, 90] becomes NULL. The subscriber is kept. The field is not
a model feature.**

67.2% of `members` rows have a `bd` outside any plausible human age -- zeros, negatives,
and values like 1051. Ingestion (T1.1) deliberately preserved them so that this decision
would be made here rather than silently upstream.

Dropping those subscribers was never an option: it would delete a real mandate over a
demographic typo, and the typo has nothing to do with whether the mandate is worth
saving. Repairing them (imputing an age) was rejected for a stronger reason:

**the missingness is not random -- it encodes the signup channel.**

| `registered_via` | members | share with a usable age |
|---:|---:|---:|
| 4 | 2,793,213 | 0.091 |
| 3 | 1,643,208 | 0.666 |
| 9 | 1,482,863 | 0.506 |
| 7 | 805,895 | 0.141 |

A seven-fold swing between the two largest cohorts. Any imputation would smear that
structure into the age column, and any model using age would then be partly reading
signup channel through it. So the field is nulled and left out of feature sets;
`registered_via` is kept as its own column, where what it is doing is visible.

**Result: 431,627 of 1,391,931 mandates (31.0%) carry a usable age.** The bounds are
inclusive at both ends, which `tests/test_mandates.py` pins -- an off-by-one here would
quietly discard the edges of the age distribution.

### 3.7 The join is a filter, and here is the exact filter

Section 1 committed to stating this with surviving counts. The counts below come from the
code that does the filtering (`FilterStep` in `mandates.py`), so they cannot drift from
it:

| step | why | subscribers |
|---|---|---:|
| subscribers in `transactions` | at least one dated transaction at or before the snapshot | 2,363,626 |
| on an auto-renewing instrument | a standing authorisation, not a one-off purchase | 1,395,697 |
| with a recoverable debit amount | paid, else list price, else the subscriber's typical payment | 1,392,039 |
| with a real coverage end | 1970-01-01 is a missing value, and a mandate needs a cycle end | 1,391,931 |
| final mandate book | `members` joined LEFT | 1,391,931 |

**1,391,931 mandates -- 58.9% of the starting population, 105.0 MB.**

Two things this table is saying that are easy to miss:

**The auto-renew filter is the whole cost.** It removes 967,929 subscribers, 41% of the
population, and everything after it removes 3,766 more. This system is about standing
authorisations, and a subscriber who only ever made one-off purchases has no mandate to
protect -- so the filter is right. But it means the book is *not* representative of KKBox
subscribers, and no result here transfers to the other 41% without an argument.

**The `members` join is LEFT, and it matters more than expected.** 357,801 mandates
(25.7%) have no `members` row at all. `members` is 6.77M rows and looked like a superset
of the 2.36M transacting subscribers; it is not -- 432,623 transacting subscribers are
missing from it. An INNER join would have quietly deleted a quarter of the book over
missing demographics. `member_record_found` is a column so this stays visible rather than
becoming a silent bias in every segment cut.

### 3.8 Status, validity, and the two derived values

**Status** is read from the latest transaction as of the snapshot, not from the history:
`is_cancel` on that row means `cancelled`, coverage ending before the snapshot means
`expired`, otherwise `active`. A history-wide "did they ever cancel" would write off
every subscriber who cancelled once and renewed -- and section 2.3 counted 245,782 such
recoveries at 84 days, so that is not a rare case.

| status | mandates |
|---|---:|
| `active` | 874,816 |
| `cancelled` | 482,504 |
| `expired` | 34,611 |

`expired` is small because KKBox usually ends coverage *at* cancellation (section 2.3):
the passive-lapse population section 2.4 measured mostly appears here as `cancelled`, or
has already churned out of the book entirely. Five mandates carry a `current_end` before
2010, one as early as 1999-12-30 -- another missing value wearing a date's clothes, too
rare (5 rows) to model and left in rather than filtered, because a special-case rule for
five rows is something a later reader has to understand for no gain.

**`expire_by` = `current_end` + `india.mandate_validity_days` (730).** Pure overlay:
KKBox has no mandate-validity column, and mandate validity is a real, load-bearing
concept for UPI AutoPay -- a mandate has an end date independent of the current cycle.
`swept: true`. What is testable, and tested, is that it is derived from the cycle end and
never precedes it.

**L = `amount_inr x (horizon_days / debit_frequency_days)`**, horizon-bounded rather than
lifetime. A lifetime L would price decisions against revenue the 12-week simulation never
observes, and the optimiser would spend real budget chasing it. Median L is 417.20; total
L at risk across the book is **INR 520,186,581**.

**R = `india.reachability_fraction_of_ltv` x L (0.15).** No public measurement exists for
what an addressable channel to a customer is worth. Pinning R to L is the honest version
of not knowing: one visible knob, derived from a quantity that *is* grounded, rather than
a second invented rupee number floating free. `swept: true`.

`q` and `r` are copied onto every row from `recovery` in the config, so a row read back
out of the parquet reconstructs a valid `models.Mandate` without needing the config file
-- and `q > r` holds by construction on every row.

### 3.9 Known limits of the book

* **Three of its fields are invented** (3.1). Any result that depends on the rail mix, on
  mandate validity, or on R is a result about this project's assumptions, not about
  KKBox.
* **`ntd_to_inr` is not economically neutral.** It scales mandate value but not channel
  costs (3.4), so it moves the value-to-cost ratio the whole system optimises.
* **The book is 59% of the subscriber population**, and deliberately the auto-renewing
  59%. It is not a sample of KKBox subscribers (3.7).
* **A quarter of the book has no demographics** (3.7), and the age column is missing
  non-randomly on top of that (3.6). Any segment-level claim has to state which
  population it is over.
* **March 2017 is excluded** (3.2), on purpose, and the book is stale by up to 31 days in
  exchange for having an out-of-time validation month.
* **The snapshot is a single point in time.** Every mandate is described as of
  2017-02-28. T1.4's person-period expansion is what gives this a time dimension; until
  then nothing here supports a statement about *when* a mandate dies.
