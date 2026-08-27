# Data mapping

How the KKBox WSDM 2018 competition data becomes the mandate portfolio this system
reasons about. Section 1 is finished (T1.1). Sections 2 and 3 are open questions with
the evidence needed to answer them, not answers.

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

Counting both of the above is the first task of section 2.

---

## 2. `is_cancel` semantics (T1.2) -- open

Cancel is not churn. A cancellation followed by another transaction is a **recovery**,
and the rate at which that happens is the `q` in `config/params.yaml`
(`recovery.after_lapse`, currently the provisional `0.35`).

Questions this section must answer with numbers:

- How many `is_cancel = 1` rows are followed by a later transaction for the same `msno`?
- Within what window? (a next-day cancel-and-repurchase is a plan change, not a recovery)
- How does that rate differ for `is_auto_renew = 1` versus `0`?
- How many rows carry the 1970-01-01 sentinel, and how many carry far-future expiries?

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
