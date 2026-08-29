# Data mapping

How the KKBox WSDM 2018 competition data becomes the mandate portfolio this system
reasons about. Sections 1 and 2 are measurements (T1.1, T1.2): the data is asked a
question and answers it. Section 3 is different in kind (T1.3) -- it is a chain of
*decisions* about data that cannot answer, so each one carries its alternative and the
cost of being wrong. The output is `data/processed/mandates.parquet`. Section 4 (T1.5) is
about reproducibility rather than about the data: it is the committed slice CI regenerates
all of this from without the download. Section 5 (T1.4) reshapes the book into the
person-period frame the hazard model is fit on, and its output is
`data/processed/person_periods.parquet`.

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

**Result: 5,676 of 1,392,175 mandates (0.4%) have an imputed cycle.** The 4% figure on
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

**Result: 431,844 of 1,392,175 mandates (31.0%) carry a usable age.** The bounds are
inclusive at both ends, which `tests/test_mandates.py` pins -- an off-by-one here would
quietly discard the edges of the age distribution.

### 3.7 The join is a filter, and here is the exact filter

Section 1 committed to stating this with surviving counts. The counts below come from the
code that does the filtering (`FilterStep` in `mandates.py`), so they cannot drift from
it:

| step | why | subscribers |
|---|---|---:|
| subscribers in `transactions` | at least one dated transaction at or before the snapshot | 2,363,626 |
| on an auto-renewing instrument | a standing authorisation, not a one-off purchase | 1,395,941 |
| with a recoverable debit amount | paid, else list price, else the subscriber's typical payment | 1,392,283 |
| with a real coverage end | 1970-01-01 is a missing value, and a mandate needs a cycle end | 1,392,175 |
| final mandate book | `members` joined LEFT | 1,392,175 |

**1,392,175 mandates -- 58.9% of the starting population, 104.5 MB.**

Two things this table is saying that are easy to miss:

**The auto-renew filter is the whole cost.** It removes 967,685 subscribers, 41% of the
population, and everything after it removes 3,766 more. This system is about standing
authorisations, and a subscriber who only ever made one-off purchases has no mandate to
protect -- so the filter is right. But it means the book is *not* representative of KKBox
subscribers, and no result here transfers to the other 41% without an argument.

**The `members` join is LEFT, and it matters more than expected.** 357,802 mandates
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

**"The latest transaction" is not always one transaction.** About 0.14% of subscribers
have two rows written on their last day, and for some of those the two rows grant the
same coverage while disagreeing about whether the mandate was cancelled. Ordering by
expiry alone leaves them tied, and a tied sort is resolved by whatever order the scan
produced -- so the same input built `cancelled: 1,053` on one run of the CI sample and
`1,054` on the next, with nothing changed. That is a quiet failure of GATE 2, which asks
a stranger's fork for a byte-identical regeneration.

`mandates.SAME_DAY_TIE_BREAK` now orders on every column the book reads off that row, so
two rows that still tie are identical rows. Its first term carries a decision rather than
just breaking a tie: of a cancel and a non-cancel granting the same coverage, **the
cancel wins**. Which one really happened is not recoverable, so the rule is chosen for
its direction -- treating an ambiguous customer as cancelled under-states retention, and
this system exists to claim retention. A rule that resolves ambiguity in favour of its
own headline is not a rule. The same reasoning replaced `mode(payment_plan_days)` in the
cycle fallback (3.5), which has no defined answer when two cycle lengths are equally
common; a tie there takes the longer cycle, which shrinks the imputed `L` rather than
inflating it.

The fix moved the book by 244 mandates out of 1.39M, and the output is now written
`ORDER BY mandate_id` -- unordered output writes different files from the same rows.

| status | mandates |
|---|---:|
| `active` | 874,679 |
| `cancelled` | 484,516 |
| `expired` | 32,980 |

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
L at risk across the book is **INR 520,284,400**.

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

---

## 4. The committed CI sample (T1.5) -- done

`scripts/build_sample.py` cuts `data/sample/` out of `data/interim/`. Sections 1 to 3 run
on ~1 GB of parquet that lives outside the repository; GitHub Actions is never going to
fetch that. So every number this project regenerates in CI comes from a **0.63 MB** slice
that is committed and small enough to read in a diff.

### 4.1 What the sample is

**5,079 subscribers** drawn from 2,363,626 (target 5,000; hash buckets do not divide
evenly, so the realised count lands near the target and not on it -- the report states
what it actually was rather than rounding the claim).

| table | rows | subscribers | size | share of source |
|---|---:|---:|---:|---:|
| `transactions` | 47,128 | 5,079 | 307 KB | 0.219% |
| `transactions_v2` | 2,906 | 2,423 | 97 KB | 0.203% |
| `members` | 4,142 | 4,142 | 155 KB | 0.061% |
| `labels` | 2,085 | 2,085 | 70 KB | 0.215% |

`members` is 0.061% of source rather than ~0.2% because it is one row per subscriber
across the *whole* service, while the sampled subscribers are drawn from those who
transacted. The share is smaller; the subscriber set is the same.

### 4.2 Three decisions, each with a cost

**The unit is the subscriber, not the row.** Taking the first N rows, or a random N% of
rows, would hand back subscribers holding three of their nine transactions -- and a
mandate with a partial history is not a smaller mandate, it is a corrupted one. Its
coverage timeline gains a hole that never existed, so it lapses in the frame and did not
lapse in life, and 2.4's `q` would be measured against fiction. Every row of every
sampled subscriber comes along. The cost is that sample size is not controllable to the
row: a subscriber with 40 transactions arrives with all 40.

**Membership is a salted hash of the id, not a random draw.** Hashing `msno` with a salt
is a property of the key, so the sample is stable without an RNG whose state would have
to be threaded through every caller to stay reproducible. Re-running the script on the
same download reproduces the same 5,079 subscribers, byte for byte. The cost is that the
realised count cannot be pinned to exactly 5,000.

The rows are also written `ORDER BY ALL`, which sorts on every column. Without it the
sample holds the right rows in whatever order the scan produced, so every rebuild shows
four changed binaries in `git diff` for no reason -- and these files are committed, so a
spurious diff on them is a spurious diff in the history. Sorting turned out to shrink the
sample by 18% as well (0.77 MB to 0.63 MB): sorted columns compress better.

**The draw is uniform, and deliberately not stratified.** Topping the sample up with
subscribers carrying rare sentinels would exercise more code paths -- at the cost of
making every rate computed on the sample wrong in a way nobody could see, because a
stratified sample looks exactly like a uniform one from downstream. The sample stays
uniform; 4.3 states which rare cases it happened to catch, and any branch it misses is
covered by hand-built fixtures in `tests/` instead. **A sample is for reproducing the
pipeline, not for re-deriving the population.**

### 4.3 Which branches CI actually enters

A sample that silently lacks a case is worse than one that lacks it loudly: the CI run
stays green and nobody learns the branch was never executed. Each probe names a branch
the pipeline actually has, so a zero here reads as "CI never enters that branch" rather
than as a curiosity about the data.

| case the pipeline handles | why it matters | rows in the sample |
|---|---|---:|
| cancellations (`is_cancel`) | the active-death rate `r` (2.3) | 1,901 |
| one-off purchases (`is_auto_renew = 0`) | the auto-renew filter, which drops 41% of the book (3.7) | 6,448 |
| zero-day plans (`payment_plan_days = 0`) | the debit-frequency imputation chain (3.5) | 1,865 |
| free rows (`actual_amount_paid = 0`) | the amount fallback to list price and typical payment (3.5) | 2,546 |
| epoch expiry (1970-01-01) | the missing-coverage-end filter (2.7, 3.7) | 3 |
| far-future expiry (past 2018-12-31, `transactions_v2`) | the implausible-cycle bound on imputation (3.5) | 48 |
| subscribers with no `members` row | the LEFT join that keeps a quarter of the book alive (3.7) | 937 |
| implausible `bd` (age) | the age-nulling rule (3.6) | 2,102 |

At this size every branch is entered. The epoch-expiry row is 3 rows out of 47,128 and is
the one to watch: it is the probe most likely to come back empty if the target is ever
lowered, and if it does, the correct response is to say so in this table, not to top the
sample up.

The far-future probe deliberately runs against `transactions_v2`. Section 2.7 measured
that those expiries live *only* in that table, so probing `transactions` for them would
report a confident zero and blame the sample for a property of the source.

### 4.4 Does the sample behave like the book?

Not the same numbers -- the same *shape*. The sample is uniform over subscribers, so
rates should land near the full run's, and a sample that drifted far from them would be a
sample of something else.

| quantity | full run | sample | section |
|---|---:|---:|---|
| retention into the mandate book | 58.9% | 59.6% | 3.7 |
| `upi_autopay` share | 0.550 | 0.549 | 3.3 |
| `card` share | 0.250 | 0.249 | 3.3 |
| `enach` share | 0.150 | 0.145 | 3.3 |
| `ppi` share | 0.050 | 0.058 | 3.3 |
| `q` at 84 days | 0.407 | 0.387 | 2.4 |
| `r` ceiling at 84 days (strict) | 0.293 | 0.294 | 2.3 |
| member record matched | 74.3% | 74.2% | 3.7 |
| per-week death rate | 0.0130 | 0.0127 | 5.3 |
| hazard, weeks 4-7 | 0.0740 | 0.0735 | 5.3 |

`q` is the widest gap, at two points. That is the expected direction of noise on 3,205
eligible lapses against 1,568,023, and it is why **the sample is where CI proves the
pipeline runs, not where this project's calibration constants come from.** `q` and `r` in
`config/params.yaml` are the full-run numbers, sourced in 2.6. Nothing reads them from the
sample.

### 4.5 One code path, not two

`--sample` swaps a directory and nothing downstream branches on it:

```
uv run python scripts/build_mandates.py --sample
uv run python scripts/analyse_cancel.py --sample
uv run python scripts/build_periods.py --sample
```

`data/sample/` holds the same four file names as `data/interim/`, so `source_dir(sample)`
in `paths.py` is the entire difference between a full run and a CI run. A second code path
would be free to drift, and the point of the sample is that CI exercises the code the full
run exercises. Only the *input* moves -- output still goes to the gitignored
`processed_dir()`, because a derived frame committed next to the sample would go stale the
first time the code changed and nobody would notice.

### 4.6 The bug this section exists to remember

Both the sampler and the rail assignment (3.3) hash `msno`. When both used a bare
unsalted hash they were not two independent draws -- they were the same draw twice. The
sampler kept exactly the subscribers in the lowest hash buckets, and those are precisely
the buckets the rail ladder hands to its first rail. The full book came out at the
configured mix; **the CI sample came out 100% UPI AutoPay**, and nothing failed. Every
per-rail number CI produced would have been silently degenerate.

The fix is a distinct salt per purpose (`SAMPLE_SALT`, `RAIL_SALT`), and two tests hold
it: one asserts the salts differ, one builds a mandate book from a sample and demands more
than one rail. Anything that hashes `msno` for a new purpose needs its own salt.

### 4.7 Known limits of the sample

* **It is 0.2% of the book.** Any rate computed on it carries roughly 20x the standard
  error of the full-run figure. It is for reproducing the pipeline, not for publishing
  numbers.
* **It is uniform, so rare branches are thin.** Three epoch-expiry rows is coverage, not
  confidence; the branch is entered, but it is not stressed.
* **It inherits every limit of 2.8 and 3.9**, because it is the same data. Nothing about
  being small makes it less of a proxy for an Indian mandate book.
* **It is committed, so it is history.** Changing `TARGET_SUBSCRIBERS` rewrites the data
  every committed result was computed on. That constant deliberately lives in code rather
  than in `config/params.yaml`: a sweep-able parameter invites a tweak, and this one should
  always arrive as a reviewed diff.

---

## 5. Person-period expansion (T1.4) -- done

`scripts/build_periods.py` turns the one-row-per-mandate book of section 3 into
`person_periods.parquet`: one row per subscriber per week they were still alive, with
`event = 1` on the week they died. That is the shape a discrete-time survival model
consumes, and fitting a logistic regression on it returns a per-week hazard directly --
which is the quantity the allocator actually needs, because the allocator's question is
"if I do nothing, what is the chance this mandate dies before my next budget arrives?"

Why discrete-time rather than Cox: a Cox model returns a hazard *ratio* and leaves the
baseline unspecified, but the allocator needs an absolute probability in [0,1] to
multiply against rupees. The baseline is the part that matters most, and Cox is the model
that declines to give it. The person-period trick turns survival into ordinary binary
classification, so `week_index` becomes a covariate like any other and T1.8's calibration
tools work unchanged.

### 5.1 The one rule

**Features may only use what was known at the start of the week. Labels may use the
future.**

That asymmetry is the whole discipline, and it is the reason this section is longer than
the code deserves. A feature that peeks one week ahead makes a model look excellent in
cross-validation and useless in production, and nothing crashes on the way there.

So no column of the mandate book is copied down into the frame unless it is genuinely
time-invariant. `amount_inr` and `debit_frequency_days` in `mandates.parquet` describe
2017-02-28; using them for a week in March 2015 would be telling the model what plan the
subscriber would eventually be on. They are recomputed as-of each week from the
transaction log instead. Only the assigned rail and the demographic columns are carried
across, because those do not move.

The fallback chains of 3.5 are recomputed the same way. Where the book falls back to a
subscriber's median payment and modal cycle over their *whole* history, the frame falls
back to the last non-zero value seen **so far** -- the same idea with the future removed.

### 5.2 Four decisions, each with a cost

**One spell per subscriber, ending at their first death.** A subscriber who lapsed,
recovered, and lapsed again contributes only the first spell. The cost is not small: on
the CI sample there are 1.37 coverage gaps per gapped subscriber, so single-spell
discards roughly a quarter of the death events. The reason is that a returning customer
is a different population from a first-time one -- `q` says 41% of lapsed mandates
self-heal, and the ones that come back have already demonstrated something the others
have not. Pooling the two estimates a hazard for neither. The alternative, multiple
spells with a spell index, is a genuine option and is left on the table for T1.7 if the
first-spell fit turns out to be data-starved.

**The week clock starts at the subscriber's first observed transaction.** `week_index` is
then duration since origination, which is the covariate a survival model is built around.
The cost is left truncation: the log opens on 2015-01-01, so a subscriber already
subscribed on that date has a `week_index` counting weeks since *observation*, not since
origination. They are flagged rather than dropped -- see 5.4.

**A death is the first confirmed coverage gap,** reusing T1.2's definitions and its 7-day
renewal tolerance, so `q` and the frame's labels cannot disagree about what a lapse is. A
gap too close to the snapshot to confirm is censored, not counted -- confirming a death
needs the full tolerance to elapse, and calling an unconfirmed gap a death would turn the
end of the observation window into an event, which is the censoring mistake 2.3 refuses
to make one layer up.

**Censored spells contribute only whole weeks; a dying spell keeps its partial last
week.** The asymmetry is deliberate. A subscriber observed for three days of a week was
not at risk for that week, and counting it as a survived week biases the hazard down. A
subscriber who died three days into a week did die in it. Treating the two the same
biases the estimate in whichever direction was chosen by accident.

### 5.3 The frame

| column | kind | as of | notes |
|---|---|---|---|
| `mandate_id` | key | -- | the subscriber; one spell each |
| `week_index` | duration | -- | 0-based weeks since `first_seen`; the baseline hazard |
| `week_start` | date | -- | carried as a real date so the frame can be aligned to the harness's calendar weeks, not only to duration |
| `event` | **label** | end of week | 1 on the death week |
| `death_kind` | **label** | end of week | `lapse` or `revocation`; null unless `event` |
| `tenure_days` | feature | week start | days since `first_seen` |
| `days_to_coverage_end` | feature | week start | the whole of T1.6's naive baseline |
| `days_since_last_txn` | feature | week start | any transaction, debit or cancel |
| `amount_inr` | feature | week start | the 3.5 chain, with the future removed |
| `debit_frequency_days` | feature | week start | ditto; `frequency_imputed` flags the fallback |
| `auto_renew` | feature | week start | time-varying, and switching it off is a signal |
| `discount_inr` | feature | week start | list price less amount actually paid |
| `debits_so_far` | feature | week start | non-cancel transactions only -- a cancel is a transaction but it is not a debit |
| `cancels_so_far` | feature | week start | |
| `paid_so_far_inr` | feature | week start | |
| `method` | feature | static | **assigned, not observed** (3.3) |
| `city`, `registered_via`, `gender` | feature | static | from `members` |
| `age_years` | feature | static | nulled outside [13, 90] (3.6); missing non-randomly |
| `member_record_found` | feature | static | false for a quarter of the book (3.7) |
| `account_age_days` | feature | week start | since `registration_init_time`; null without a members row |
| `left_truncated` | flag | static | see 5.4 |

### 5.4 Left truncation, made decidable

A subscriber's first observed transaction is a genuine origination only if no earlier
cycle existed. That is not directly observable -- but it is decidable one way. If the
first transaction lands at least one billing cycle after the log opens, then a preceding
cycle *would* have fallen inside the window and would have been seen; its absence is
evidence. Closer than that and the two cases cannot be told apart.

So `left_truncated` is `first_seen < data_start + debit_frequency_days`, which is a rule
derived from the data rather than a date chosen by hand. Flagged rather than dropped:
dropping them would discard the longest-tenured subscribers systematically, which is
worse than a covariate that says "this one's clock may have started earlier".

`account_age_days` is the separate, complementary column: `registration_init_time` says
how long the *person* has been a customer, which is not the same fact as how long the
*mandate* has been running. Both are in the frame, and neither is a substitute for the
other.

### 5.5 Expanded from the transaction side, not the week side

Worth recording because the obvious implementation does not finish.

The definition says: generate every week, and for each one look up the last transaction
at or before its `week_start`. Written that way it is an ASOF join with 46M probes across
1.4M partitions, and on the full book it ran for over twenty minutes without producing a
file. Materialising the intermediate frames on top of that spilled 16 GB.

Inverting it is much cheaper and gives the identical frame. A transaction's state holds
from its own day until the next transaction, so each row of the per-day running state
already *owns* a contiguous run of weeks -- every week whose `week_start` falls in
`[day, next_day)`. Computing that run is arithmetic on two dates, and the expansion
becomes an ordinary hash join followed by an `unnest`. The runs are contiguous and
disjoint by construction (one row's last week is the next row's first, minus one), so
every week of every spell appears exactly once; `tests/test_periods.py` asserts that
directly rather than trusting the argument.

The leakage barrier survives the inversion: a week is assigned the transaction whose
interval contains its `week_start`, and that transaction happened at or before it.

Two mechanical notes for anyone re-running this. DuckDB needs `temp_directory` set or an
in-memory database has nowhere to spill -- the first full run died after seven minutes
with no file and no error message. And the output is written `ORDER BY mandate_id,
week_index`, because unordered output writes a different file from the same rows.

### 5.6 What came out

| step | why | subscribers |
|---|---|---:|
| final mandate book (3.7) | where section 3 left off | 1,392,175 |
| with at least one observable week | a spell ending on or before its first transaction has no week to expand | 1,379,341 |

**58,079,041 person-weeks** over 1,379,341 spells, 183.3 MB. Median spell 32 weeks, longest
112 -- which is the whole log, so the longest spells are subscribers who never died.

| quantity | value |
|---|---:|
| spells ending in a death | 756,457 (54.8%) |
| spells censored at the snapshot | 622,884 |
| gaps too close to the horizon to confirm -- censored, not counted | 6,846 |
| spells whose mandate predates the log (`left_truncated`) | 334,048 |
| per-week death rate | **0.0130** |

| death | events |
|---|---:|
| `revocation` | 478,962 |
| `lapse` | 277,495 |

**Revocations outnumber lapses roughly two to one**, which inverts section 2.4's ratio
(1.74M passive lapses against 854k cancel events) and is not a contradiction. Two filters
do it. The book is the auto-renewing 59% of subscribers (3.7), and a cancellation is
something only a live auto-renewing subscription can have. And this frame keeps each
subscriber's *first* death only, while section 2 counts every gap -- and repeat gaps skew
passive. The reading is that for a standing mandate, the first ending is more often a
deliberate one. That matters for pricing, because `r < q`.

The per-week rate of 0.0130 is the intercept a hazard model has to beat: a model that
predicts 1.3% for everyone is already "right" 98.7% of the time, which is why T1.6 scores
Brier and calibration rather than accuracy.

### 5.7 The baseline hazard is not flat

| duration | person-weeks | deaths | hazard |
|---|---:|---:|---:|
| weeks 0-3 | 5,339,657 | 87,066 | 0.0163 |
| **weeks 4-7** | 4,156,924 | 307,511 | **0.0740** |
| weeks 8-12 | 4,526,546 | 49,719 | 0.0110 |
| weeks 13-25 | 10,531,425 | 67,472 | 0.0064 |
| weeks 26-51 | 16,514,592 | 132,077 | 0.0080 |
| weeks 52+ | 17,009,897 | 112,612 | 0.0066 |

Death is concentrated at weeks 4 to 7 -- **4.5 times the average rate**, and 41% of all
deaths in 7% of the person-weeks. That window is the first renewal of a 30-day plan, which
is the modal cycle in this book. A mandate that survives its first renewal is roughly an
order of magnitude safer per week than one approaching it.

This is the readout the section was built to produce. A flat hazard would have meant
`week_index` carried no signal and the survival framing had bought nothing over a plain
cross-section; it is not flat, so it did. It also gives the allocator a shape to exploit
that a static risk score cannot express: the same mandate is worth contacting at week 5
and not at week 30.

The CI sample reproduces the shape closely -- 0.0160 / 0.0735 / 0.0109 / 0.0057 / 0.0079 /
0.0067 against the full run's column above, on 0.2% of the data. That is the strongest
evidence so far that section 4's sample is a sample and not a different dataset.

### 5.8 Cost, stated plainly

The full expansion takes **89 minutes**, almost all of it in the final sort, and spills
several GB. That is accepted rather than optimised away: it runs once per dataset, and CI
never runs it -- the sample expands in **2.4 seconds**, which is what every committed
number is regenerated from.

58M rows is also more than a laptop can hand to `sklearn` as a dense float matrix. T1.7
will fit on a subsample or in chunks, and that decision belongs to T1.7; the frame's job
is to be the complete, honest shape, not to be small.

### 5.9 Known limits of the frame

* **Recurrent deaths are discarded** (5.2). Roughly a quarter of the coverage gaps in the
  data belong to subscribers who had already had one, and none of them are here.
* **The population is conditioned on the snapshot.** The book keeps subscribers whose
  *latest* transaction was auto-renewing (3.7), which is a fact about 2017-02-28 applied
  to a frame that starts in 2015. It does not condition on being alive -- dead mandates
  are in the book -- but it does condition on the instrument.
* **`week_index` is not always duration since origination** (5.4), and the flag marks
  which rows.
* **The rail is assigned** (3.3), so any per-rail hazard is a statement about this
  project's overlay and not about KKBox.
* **Nothing here is causal.** The frame supports "how likely is this mandate to die",
  not "how much would an ask change that". The second question is what T3.9's
  random-drop holdout is shaped for, and it is not answerable from this data at all.
