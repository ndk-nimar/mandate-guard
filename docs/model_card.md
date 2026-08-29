# Model card — MandateGuard lapse hazard

One model, one job: given a live recurring mandate and the week ahead, return the
probability it dies in that week if nobody contacts the customer.

Everything downstream multiplies that number by rupees, so this card is written around
the question "when is this number wrong, and in which direction".

| | |
|---|---|
| **Name** | `mandateguard.risk.hazard` |
| **Type** | Discrete-time survival model — logistic regression on person-weeks |
| **Version** | T1.7, 2026-08-29 |
| **Output** | `P(death in this week \| alive at the start of it)`, in (0, 1) |
| **Seed** | `params.seed = 20260905` |
| **Fitted on** | 1,000,935 person-weeks (13,897 deaths), a deterministic subsample of 44M |
| **Evaluated on** | 6,354,281 held-out person-weeks (46,251 deaths), out of time |
| **Reproduce** | `uv run python scripts/fit_hazard.py --plot` |

---

## 1. Intended use

**In scope.** Ranking and pricing *re-consent asks* on a book of recurring mandates: which
mandates are close enough to death, and valuable enough, to be worth one of a limited
number of contacts this week.

**Out of scope, and the boundary matters.**

* **Not a causal model.** It answers "how likely is this mandate to die", not "how much
  would an ask change that". Those are different questions and this data cannot answer the
  second — nobody ran the experiment. `eval/holdout.py` (T3.9) is built in the shape of
  the design that *would* answer it.
* **Not a customer-facing decision.** Nothing here should deny anyone service, pricing, or
  credit. It decides who gets a message.
* **Not a churn model for a merchant's own book** without refitting. §5 explains why two
  of its strongest features would not survive the move.

---

## 2. Data

KKBox WSDM 2018 subscription data (Taiwan, 2015-01-01 to 2017-02-28), reshaped into a
mandate portfolio by the India mapping layer in [`mapping.md`](./mapping.md) §3, then
expanded into person-weeks in §5.

| | |
|---|---|
| Source | `kkbox-churn-prediction-challenge`, `transactions` and `members_v3` |
| Mandate book | 1,392,175 mandates — the auto-renewing 58.9% of 2.36M subscribers |
| Frame | 58,079,041 person-weeks over 1,379,341 spells |
| Unit | one subscriber-week |
| Label | `event = 1` on the week of the first **confirmed** coverage gap |
| Base rate | 0.0130 per person-week over the frame; 0.0139 train, 0.0073 test |

**The book is not a sample of KKBox subscribers.** The auto-renew filter removes 967,685
subscribers (41%), deliberately: a one-off purchaser has no standing authorisation to
protect. No result here transfers to that 41% without an argument.

**Three fields are invented, not measured** (`mapping.md` §3.1): the payment rail, mandate
validity, and the reachability value `R`. The rail is excluded from the model for exactly
this reason (§3).

**A death is the first confirmed coverage gap**, using T1.2's 7-day renewal tolerance.
Deaths are split into `lapse` (coverage ran out) and `revocation` (a cancel came first),
because they recover at different rates — measured `q = 0.407` against an `r` ceiling of
0.293. The model predicts either; the distinction is carried for the value layer.

**Recurrent deaths are discarded.** One spell per subscriber, ending at the first death,
which drops roughly a quarter of the coverage gaps in the data. `mapping.md` §5.2 argues
it: a returning customer is a different population, and pooling estimates a hazard for
neither.

---

## 3. Features

56 inputs. Duration and the expiry clock enter as one-hot bins rather than linear terms,
because neither effect is remotely linear — the hazard runs at 0.0740 in weeks 4-7 and
0.0066 at week 52+, and a mandate expiring within three days is 38 times more likely to
die than one with a month left.

| group | examples |
|---|---|
| duration | `week_index` bins: 2-3, 4, 5, 6-7, 8-12, 13-25, 26-51, 52+ (weeks 0-1 is the reference) |
| expiry clock | `days_to_coverage_end` bins: already expired, 0-3, 4-7, 8-14, 15-30, 31-60, 61-120, plus an explicit *unknown* indicator |
| billing | `log` of amount, cycle length, debits so far, cancels so far, rupees paid so far, days since last transaction |
| flags | `auto_renew`, `frequency_imputed`, `left_truncated`, `member_record_found`, discounted |
| demographic | city (17 dummies), signup channel (5), gender (2) |

Every feature is computed **as of the start of the week** — `mapping.md` §5.1. The
snapshot book's own `amount_inr` and `debit_frequency_days` describe 2017-02-28 and are
deliberately *not* copied down; they are recomputed from the transaction log at each week.
A test builds a subscriber who changes plan mid-spell and asserts the earlier weeks cannot
see the later plan.

**Five columns are excluded on purpose**, and a test asserts none of them appears in any
feature expression:

| excluded | why |
|---|---|
| `age_years` | missing non-randomly; its missingness encodes signup channel (`mapping.md` §3.6). A model given it learns the channel and gets credited with learning about age. |
| `method` (rail) | assigned from a hash, so it carries no information by construction. Any coefficient would be overfit, and the UI would render a per-rail effect as a finding. |
| `week_start` | calendar time. "November 2016 was bad" cannot be carried into a held-out future. |
| `tenure_days` | exactly `7 × week_index`. |
| `death_kind` | a label. |

---

## 4. Performance

Out-of-time split: train before 2016-12-06, test the 12 weeks after (which is
`horizon.weeks`). Weeks whose outcome could not be observed before the log ended are
dropped from both slices.

| model | Brier | log loss | mean p | calibration | Brier skill |
|---|---:|---:|---:|---:|---:|
| `base_rate` | 0.007269 | 0.0450 | 0.0139 | 1.903 | +0.0000 |
| `expiry_rule` | 0.212228 | 7.3303 | 0.2159 | 29.658 | −28.1964 |
| `expiry_bins` | 0.007400 | 0.0401 | 0.0145 | 1.992 | −0.0181 |
| **`hazard`** | **0.006817** | **0.0369** | 0.0085 | **1.164** | **+0.0621** |

**GATE 1 passed.** The model beats the strongest naive baseline on log loss and is the
only model here with positive Brier skill.

**Accuracy is not reported anywhere.** At a 0.7% base rate, "this mandate survives" is
99.3% accurate and worth nothing.

The reliability diagram is in [`eval.md`](./eval.md) §3 and
[`img/reliability.png`](./img/reliability.png).

| | `hazard` | `expiry_bins` |
|---|---:|---:|
| expected calibration error | 0.00363 | 0.00722 |
| worst bucket | 0.03247 | 0.05506 |

---

## 5. Limitations

Ordered by how much they should worry a reader.

**1. The predictions are too spread out, and the error runs the wrong way.** The model
under-predicts in its lowest-risk buckets (bucket 1: 0.019% predicted, 0.21% observed) and
over-predicts in its highest (bucket 20: 8.5% predicted, 5.2% observed). The top bucket is
exactly the population the allocator spends its budget on, so its risk is overstated by
about 60% — and every rupee figure derived from `p × L` for a high-risk mandate is
overstated with it. **This biases the optimiser toward asking**, which is the wrong
direction for a project whose central claim is that over-asking is expensive. A
calibration layer (Platt or isotonic, on a third held-out slice) is the fix; it is logged
in [`limitations.md`](./limitations.md) and not yet built.

**2. Two of the strongest features are about our own data, not about customers.**
`frequency_imputed` (+2.48, the second-largest coefficient) flags rows whose billing cycle
*we* had to guess, and `member_known` (−1.15) flags whether a demographic row existed.
Both are real signal on KKBox and neither would survive a move to a merchant's own book,
where those fields are populated. Any transfer of this model must drop them and refit.

**3. It is Taiwanese music-streaming data wearing an Indian mandate costume.** The rail
mix, mandate validity and `R` are overlays (`mapping.md` §3.1). Nothing here is evidence
about UPI AutoPay behaviour.

**4. The population is conditioned on the snapshot.** The book keeps subscribers whose
*latest* transaction was auto-renewing — a fact about 2017-02-28 applied to a frame
starting in 2015. It does not condition on being alive (dead mandates are in the book) but
it does condition on the instrument.

**5. It is not causal, and the data cannot make it so.** See §1.

**6. `week_index` is not always duration since origination.** 334,048 spells (24%) are
flagged `left_truncated`: their mandate may predate the transaction log. The flag is a
feature so the model can condition on it, but the underlying clock is still wrong for
those rows.

**7. Demographics are missing for a quarter of the book**, non-randomly. Any segment-level
claim has to state which population it is over.

---

## 6. Fairness

The model consumes `city`, `registered_via` and `gender`, and its output decides who
receives a retention message. Three things about that:

* **The decision is a message, not a denial.** Nothing here withholds service or changes
  a price. The worst outcome for a customer scored high is an extra contact; the worst for
  one scored low is not being warned before a mandate lapses.
* **`age_years` is excluded**, but not for fairness reasons — it is excluded because its
  missingness leaks signup channel (§3). The fairness benefit is a side effect and should
  not be claimed as a design goal.
* **No subgroup performance breakdown has been run.** That is a gap, not a clean bill of
  health. Before this went anywhere near production, calibration would need checking
  within each `city` and `registered_via` level, because a model calibrated on average can
  be badly wrong on a segment — and §5's spread problem makes that more likely, not less.

---

## 7. Reproduction

```
uv run python scripts/ingest.py            # CSVs -> typed parquet
uv run python scripts/build_sample.py      # the committed 5k CI slice
uv run python scripts/build_periods.py     # the person-period frame
uv run python scripts/fit_hazard.py --plot # this model, its scores, the diagram
```

Add `--sample` to every step after the first to run the whole chain on the committed
sample with no download. Every derived file is written deterministically
([ADR 0003](./adr/0003-determinism-of-derived-data.md)): the same input produces the same
bytes, and a test asserts it for the book, the frame, the sample and the diagram.
