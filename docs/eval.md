# Evaluation

What each model in this project scores, on what, and against what. Section 1 is the
naive baselines (T1.6), which are scored *before* the real model exists so that the
number it has to beat was fixed before anyone knew what it would produce.

Reproduce with:

```
uv run python scripts/build_periods.py --sample   # the frame, from the committed sample
uv run python scripts/score_baseline.py --sample
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
