# Evaluation

What each model in this project scores, on what, and against what. Section 1 is the
naive baselines (T1.6), which are scored *before* the real model exists so that the
number it has to beat was fixed before anyone knew what it would produce. Section 2 is
the hazard model (T1.7) and GATE 1. Section 3 is calibration (T1.8), which is where the
model's honest weakness is.

Reproduce with:

```
uv run python scripts/build_periods.py --sample   # the frame, from the committed sample
uv run python scripts/score_baseline.py --sample  # section 1
uv run python scripts/fit_hazard.py --sample --plot   # sections 2 and 3
```

Drop `--sample` for the full 58M-row frame. The numbers below are the full run; the
sample's are in §1.5 and are close enough to be a check on both.

---

## 1. Naive baselines (T1.6) -- done

### 1.1 How everything here is scored

**A model is a SQL expression that returns a probability.** The baselines are expressions
over one column; T1.7's logistic regression will be a sigmoid of a linear combination
whose coefficients were fitted elsewhere. Both are then scored by the same aggregate over
the same rows. If the baseline and the model were scored by different code, "the model
beats the baseline" would be a claim about two scripts as much as about two models -- and
GATE 1 turns on exactly that comparison.

It also means every model is scored on the **whole** held-out frame (6.35M person-weeks),
in about two seconds, rather than on whatever subsample fits in memory.

**The split is out of time.** Training is everything before 2016-12-06; the test set is
the 12 weeks after it, which is `horizon.weeks` -- the window the harness actually rolls
the world forward over. Two alternatives were rejected:

* A **random split of person-weeks** puts week 30 of a subscriber in training and week 31
  in test, so the model predicts a week it has effectively already seen.
* A **split by subscriber** answers "how well does this generalise to strangers?" This
  system is not deployed against strangers. It is deployed against a book it already
  knows, predicting forward.

**The last two weeks are dropped from both slices.** A death is only recorded once the
7-day renewal tolerance has elapsed without a renewal, so a week ending inside the last
`7 + 7` days of the log cannot contain a *confirmed* death. Those rows are not wrong --
their features are real -- but their labels are all zero for a reason that has nothing to
do with the subscribers. Leaving them in dropped the sample's test base rate from 0.0083
to 0.0078 and read as a miscalibrated model rather than as an artefact of the window. This
is `mapping.md` §2.2's right-censoring rule applied one layer down.

**Metrics.** Brier, log loss, mean prediction, and calibration-in-the-large (predicted
deaths over actual). Accuracy appears nowhere in this project: at a base rate of 1.4%, a
model that always says "survives" is 98.6% accurate and worthless.

Brier is reported as a **skill score** against the constant baseline -- the share of its
error removed -- because raw Brier at this base rate is around 0.007 for everything and
carries no information about which model is better.

### 1.2 The three baselines

| baseline | what it is | why it is here |
|---|---|---|
| `base_rate` | the training death rate for every row | the floor, and the reference every skill score is computed against |
| `expiry_rule` | 1 if coverage ends within 7 days, else 0 | the plan's phrasing taken literally; what a team without a model would actually ship |
| `expiry_bins` | the observed death rate per `days_to_coverage_end` bin | the same idea done as well as it can be done without a model -- the honest opponent |

All three are fitted on the training slice only. The bin edges are fixed
(0, 3, 7, 14, 30, 60, 120 days) rather than quantiles of the data, because quantile bins
would move whenever the data moved and a baseline that drifts is not a baseline.

### 1.3 What the billing clock is worth on its own

Training death rate per person-week: **0.0139**.

| `days_to_coverage_end` | person-weeks | deaths | rate |
|---|---:|---:|---:|
| already expired | 49,971 | 10,055 | **0.2012** |
| 0-3 days | 4,514,713 | 428,017 | **0.0948** |
| 4-7 days | 5,519,107 | 143,149 | 0.0259 |
| 8-14 days | 10,546,998 | 48,689 | 0.0046 |
| 15-30 days | 25,507,623 | 64,837 | 0.0025 |
| 31-60 days | 3,756,765 | 12,138 | 0.0032 |
| 61-120 days | 408,196 | 62 | 0.0002 |
| 120+ days | 797,544 | 1,041 | 0.0013 |

The signal is enormous and entirely unsurprising: a mandate whose coverage ends in the
next three days is **38 times** more likely to die this week than one with two to four
weeks left. Closeness to expiry is a real feature and any model that ignored it would be
broken.

That is exactly why it is the baseline. The interesting question is not whether the
billing clock predicts death -- it obviously does -- but whether anything *else* does,
once the clock is accounted for.

### 1.4 The result, and it is not the expected one

Test slice: **6,354,281 person-weeks**, 46,251 deaths, base rate **0.0073**.

| model | Brier | log loss | mean p | calibration | Brier skill |
|---|---:|---:|---:|---:|---:|
| `base_rate` | 0.007269 | 0.0450 | 0.0139 | 1.903 | +0.0000 |
| `expiry_rule` | 0.212228 | 7.3303 | 0.2159 | 29.658 | −28.1964 |
| `expiry_bins` | 0.007400 | **0.0401** | 0.0145 | 1.992 | **−0.0181** |

Three things to read here, and the second is the one that matters.

**`expiry_rule` is catastrophic.** It predicts a death for 21.6% of person-weeks when 0.7%
die, so it is wrong by a factor of 30 and its Brier is 29 times the constant baseline's.
"Contact everything close to expiry" is not a conservative default; it is a policy that
would burn the entire ask budget on mandates that were never going to die. This is the
number that justifies the whole project existing.

**`expiry_bins` discriminates better and scores worse.** Its log loss is 11% below the
constant baseline -- it genuinely separates the risky weeks from the safe ones, as §1.3
shows it must. But its Brier skill is **negative**, because it predicts on average 0.0145
against an actual 0.0073: **twice as many deaths as happened**. Brier is a proper score
and charges for that.

**The cause is the split, not the bins.** The training period's death rate is 0.0139 and
the test period's is 0.0073. That gap is real and is composition, not noise: by the last
12 weeks of the book, everyone who was going to die at their first renewal already has,
so the surviving risk set is enriched with long-tenured, low-hazard mandates. The
`mapping.md` §5.7 hazard table says the same thing -- weeks 4-7 run at 0.0740 and weeks
52+ at 0.0066.

### 1.5 What this sets up for T1.7

The bar the hazard model has to clear is now specific rather than rhetorical:

1. **Beat `expiry_bins` on log loss** (0.0401). Discrimination beyond the billing clock is
   the claim that the extra features carry something.
2. **Beat `base_rate` on Brier skill** (0.0000), which `expiry_bins` does not. That
   requires calibration the binned lookup cannot have, and it is available: the
   composition shift is a shift in *duration mix*, and `week_index` is a column in the
   frame. A model that conditions on duration should track the falling base rate that a
   period-agnostic lookup table cannot.

If the model beats (1) and not (2) it is discriminating without being calibrated, and
`docs/model_card.md` has to say so, because the allocator multiplies these probabilities
by rupees -- a probability that is twice too big prices every decision twice too high.

The committed sample reproduces the shape on 0.2% of the data: training rate 0.0134, test
base rate 0.0083, `expiry_bins` log loss 0.0426 against a constant 0.0490. Its Brier skill
comes out slightly positive (+0.0060) rather than slightly negative, which is what 115
test deaths instead of 46,251 buys you -- the direction of the finding is a full-data
result and the sample is a smoke test for the pipeline, not a second opinion on it.

---

## 2. The hazard model (T1.7) -- done

A discrete-time hazard model *is* a logistic regression on the person-period frame. Each
row asks "did this mandate die in this week, given that it was alive at the start of it",
and the fitted probability is the per-week hazard the allocator will multiply against
rupees. `docs/stack.md` records why nothing more exotic: a Cox model leaves the baseline
unspecified when the baseline is the part that matters, and a gradient-boosted tree wins
on AUC and loses on calibration, which is what GATE 1 measures.

Reproduce with `uv run python scripts/fit_hazard.py --plot`.

### 2.1 Fit small, score whole

The training slice is 44M person-weeks, which is more than this laptop can hand `sklearn`
as a dense matrix. So the model is fitted on **1,000,935 rows** -- a deterministic uniform
subsample, drawn by hashing the row key with `params.seed` rather than by an RNG whose
state would have to be threaded through every caller -- and then scored, as a SQL
expression, over all **6,354,281** held-out rows.

The subsample is uniform over *rows*, not over subscribers. Sampling subscribers and
keeping all their weeks would make the sample's rows more correlated with each other, not
less, and would under-cover the long durations only long spells reach.

One million rows is past the point where more rows move the coefficients; the binding
constraint is the **13,897 deaths** they contain. A test asserts that the SQL expression
reproduces `sklearn`'s own `predict_proba` to 1e-9, because otherwise every number below
would describe a model that was never fitted, and nothing would fail.

### 2.2 What is deliberately not a feature

| excluded | why |
|---|---|
| `age_years` | `mapping.md` §3.6 -- missing non-randomly, and its missingness encodes signup channel. A model given it learns the channel and gets reported as having learned about age. |
| `method` (rail) | assigned from a hash (§3.3), so it carries no information by construction. Any coefficient would be pure overfit, and the UI would render a per-rail effect as a finding. |
| `week_start` | calendar time. "November 2016 was a bad month" cannot be carried into a held-out future, and the split is out of time. Duration is a covariate; the calendar is not. |
| `tenure_days` | exactly `7 * week_index` on every row. |
| `death_kind` | a label. |

A test asserts none of these five strings appears in any feature expression. That is a
guard rather than a comment because the cost of quietly re-adding one is a model that
scores better and means less.

**No `class_weight="balanced"`.** It is the standard reflex at a 1.4% base rate and it
would multiply every predicted probability by roughly the imbalance ratio. §1.4 already
showed a well-discriminating, badly-calibrated model losing on Brier, and the allocator
turns these numbers into rupees.

### 2.3 The model

56 features. Duration enters as one-hot bins rather than a linear term, because §5.7 of
`mapping.md` measured a hazard of 0.0740 at weeks 4-7 against 0.0066 at week 52+; weeks 4
and 5 get their own bins because that is where a 30-day plan's first renewal lands. The
expiry clock is binned with exactly the edges `expiry_bins` uses, so the model is *handed*
the baseline's information and any improvement has to come from somewhere else.

Intercept −1.6752, converged in 83 iterations. Largest coefficients:

| feature | coefficient | reading |
|---|---:|---|
| `expiry_2` | +2.5209 | coverage ends in 0-3 days |
| `frequency_imputed` | +2.4807 | the billing cycle had to be guessed (§3.5) |
| `log_cancels` | +1.5154 | has cancelled before |
| `expiry_3` | +1.4441 | coverage ends in 4-7 days |
| `weeks_4_4` | +1.2371 | week 4 -- the first renewal |
| `member_known` | −1.1489 | has a `members` row |
| `weeks_6_7` | +1.1278 | weeks 6-7 |
| `expiry_6` | −1.0211 | coverage ends in 31-60 days |
| `channel_4` | +0.9993 | signup channel 4 |
| `weeks_5_5` | +0.9629 | week 5 |
| `log_cycle` | −0.9591 | longer billing cycle |
| `channel_9` | +0.9582 | signup channel 9 |

Two of these are worth pausing on.

**`frequency_imputed` at +2.48 is the second-largest coefficient in the model, and it is
not about subscribers at all.** It flags the 0.4% of rows whose billing cycle had to be
inferred rather than read (`mapping.md` §3.5). The model has found that *our own missing
data* predicts death — almost certainly because a row with no stated plan length is a row
whose payment record is unusual for other reasons too. It is a real signal on this data
and it would not transfer to a merchant's own book, where the field is populated. This is
flagged in `docs/model_card.md` rather than quietly enjoyed.

**`member_known` at −1.15 says the same thing from the other side**: the quarter of the
book with no demographic row dies more. That is a property of KKBox's record-keeping, not
of Indian mandates.

### 2.4 The result -- GATE 1

Test slice: **6,354,281 person-weeks**, 46,251 deaths, base rate **0.0073**.

| model | Brier | log loss | mean p | calibration | Brier skill |
|---|---:|---:|---:|---:|---:|
| `base_rate` | 0.007269 | 0.0450 | 0.0139 | 1.903 | +0.0000 |
| `expiry_rule` | 0.212228 | 7.3303 | 0.2159 | 29.658 | −28.1964 |
| `expiry_bins` | 0.007400 | 0.0401 | 0.0145 | 1.992 | −0.0181 |
| **`hazard`** | **0.006817** | **0.0369** | 0.0085 | **1.164** | **+0.0621** |

**Both bars from §1.5 are cleared.** Log loss 0.0369 against `expiry_bins`' 0.0401, so
the model discriminates beyond the billing clock it was handed. Brier skill +0.0621 where
`expiry_bins` scores −0.0181, so it is also the only model here that beats predicting the
base rate for everything.

The mechanism is the one §1.4 predicted. `expiry_bins` inherits the training period's
death rate of 0.0139 and applies it to a period running at 0.0073, so it over-predicts by
a factor of two. The hazard model conditions on `week_index`, and the composition shift
*is* a duration shift, so it tracks most of the decline: calibration 1.164 against 1.99.

> **GATE 1: passed.** The hazard model beats the naive baseline on Brier score.
> `docs/model_card.md` records what it is and is not.

---

## 3. Calibration (T1.8) -- done

Brier mixes two virtues. This section separates them, because for this system they are
not equally important: the allocator multiplies these probabilities by rupees, so being
*ranked* correctly is not enough.

![Reliability diagram](img/reliability.png)

Twenty equal-count buckets of the prediction. Equal-count rather than equal-width because
at a base rate of 0.7% equal-width bins put 99% of the rows in the first one; log axes for
the same reason, because the predictions span two orders of magnitude.

| model | ECE | worst bucket |
|---|---:|---:|
| **`hazard`** | **0.00363** | 0.03247 |
| `expiry_bins` | 0.00722 | 0.05506 |

The hazard model's expected calibration error is **half** the binned baseline's. But
0.00363 against a base rate of 0.0073 is still half the base rate, and the shape of the
error matters more than its size.

### 3.1 The predictions are too spread out

| bucket | person-weeks | deaths | predicted | observed | ratio |
|---:|---:|---:|---:|---:|---:|
| 1 | 317,715 | 670 | 0.00019 | 0.00211 | 0.09 |
| 5 | 317,714 | 754 | 0.00055 | 0.00237 | 0.23 |
| 10 | 317,714 | 785 | 0.00121 | 0.00247 | 0.49 |
| 13 | 317,714 | 520 | 0.00194 | 0.00164 | 1.18 |
| 16 | 317,714 | 1,223 | 0.00680 | 0.00385 | 1.77 |
| 18 | 317,714 | 8,202 | 0.01871 | 0.02582 | 0.72 |
| 20 | 317,714 | 16,598 | 0.08472 | 0.05224 | 1.62 |

(The full twenty rows are in the script's output; these are the shape.)

The model **under-predicts at the bottom** -- bucket 1 says 0.019% and 0.21% die -- and
**over-predicts at the top** -- bucket 20 says 8.5% and 5.2% die. It crosses over around
bucket 13. That is a model whose predictions are too *confident*: correctly ordered, but
stretched at both ends.

**What it costs this system.** The top bucket is exactly the population the allocator will
spend its budget on, and its risk is being overstated by about 60%. Every rupee value
computed from `p x L` for a high-risk mandate is therefore about 60% too large, which
biases the optimiser toward asking. That is the wrong direction of error for a project
whose entire argument is that over-asking is expensive — so it is stated here, in
`docs/model_card.md`, and in `docs/limitations.md`, rather than left for someone to find.

**Why it happens.** Two contributions, and they are separable. The aggregate over-prediction
(calibration 1.164) is the base-rate shift between the training and test periods, and an
intercept correction would remove it. The remaining spread is not an intercept problem:
bucket 20 is off by 1.62 and bucket 18 by 0.72, in opposite directions.

**The fix, and why it is not in yet.** A calibration layer -- Platt scaling or isotonic
regression, fitted on a slice held out from the fit and separate from the test slice --
is the standard remedy and would fit this project's shape well, because the scored model
is already a SQL expression and a monotone rescale is another one. It is not in because
it needs a third slice carved out of the split, which changes every number in §1 and §2,
and GATE 1 is passed without it. It is logged in `docs/limitations.md` as the first thing
to do if the allocator's rupee numbers are taken seriously.
