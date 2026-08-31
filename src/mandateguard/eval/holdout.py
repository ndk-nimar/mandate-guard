"""T3.9 -- Meta's identification design: the experiment this system would need to be believed.

Everything else in `docs/eval.md` compares arms **inside the simulator**. That answers
"which allocator does better in this model" and cannot answer "does the model resemble the
world", because the simulator grades its own homework: the harness applies the same uplift
and backfire that the arms optimise against, so an arm that games those numbers is
rewarded for it.

The only answer that escapes that circle is a **holdout**. Meta's notification work states
the estimand plainly:

```
effect = P(active | do(send)) - P(active | do(drop))
```

`do(...)` rather than a conditional: the contrast is between two *interventions*, and it
is identified because assignment is random rather than because the two groups happen to
look alike. Comparing contacted mandates against uncontacted ones without randomising is
the classic notification-analytics mistake -- the allocator picks the risky ones, so the
contacted group is sicker by construction and the naive contrast reads *negative* no
matter how well the system works. `naive_contrast()` below computes that wrong number
deliberately, because seeing the two side by side is most of the point.

Randomise inside the selection, not across the book
---------------------------------------------------
Dropping half the book at random would put mandates in the control group that the policy
was never going to contact anyway, diluting the estimate toward zero with rows that carry
no information. So the coin is flipped **only among the mandates the policy chose**: it
selects as usual, and then half of what it wanted to send is withheld.

That makes the contrast a clean causal statement about the population the system actually
acts on -- which is also the only population a merchant would agree to hold out.

The coin is a hash, and that is not a shortcut
-----------------------------------------------
ADR 0003 requires every derived file to be byte-identical across runs, and an RNG whose
state has to be threaded through every caller to stay reproducible is exactly what
`data/sample.py` refused for the same reason. Assignment here is `sha256(mandate_id ||
salt)`, so it is a *property of the key*: stable without a generator, and independent
across salts.

**`hashlib`, never the builtin `hash()`.** Python salts string hashing per process unless
`PYTHONHASHSEED` is pinned, so `hash("m001") % 2` gives a different answer in tomorrow's
run. That would make the holdout non-reproducible in a way nothing would flag -- the
numbers would simply be a little different each time, which is exactly what a noisy
experiment is supposed to look like.

What this can and cannot tell you
---------------------------------
The harness carries expectations rather than sampled outcomes (`eval/world.py`), so given
an assignment the outcome is deterministic and there is no sampling noise to put a
standard error on. That does **not** leave the estimate unqualified: the assignment itself
is the only random object in the design, so re-drawing it under different salts and
recomputing gives a genuine **randomisation distribution**. `spread()` does that, and it
is the honest form of uncertainty for this harness -- it answers "how much does this
number depend on which half we held out", which is a real question, rather than "how much
would it move under resampling", which this simulator cannot answer at all.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable

from pydantic import BaseModel, Field

from mandateguard.allocator.base import Policy
from mandateguard.eval.world import BookMandate, RunMetrics, run
from mandateguard.models import AllocationResponse, Decision, DecisionKind, MandateWeek
from mandateguard.policy.loader import Params

HOLDOUT_SALT = "holdout"
"""Salt mixed into the id before hashing.

Its own salt, for the reason `data/sample.py` records: a group selected by the low buckets
of a bare `hash(id)` is not a random group, it is the set of ids that *every other* bare
hash of the same key also puts in its low bucket. The sample's salt and this one must not
be the same string, or the holdout would be correlated with sample membership."""

HASH_BUCKETS = 1_000_000
"""Resolution of the assignment. Fine enough that a 50% split lands within a rounding
error of half on any book worth running."""


def assigned_to_control(mandate_id: str, share: float, salt: str = HOLDOUT_SALT) -> bool:
    """Is this mandate held out? A property of its id, not of a draw."""
    digest = hashlib.sha256(f"{mandate_id}|{salt}".encode()).hexdigest()
    return int(digest[:12], 16) % HASH_BUCKETS < share * HASH_BUCKETS


class RandomDrop(Policy):
    """Wraps any arm and withholds a random share of the asks it wanted to make.

    Deliberately a wrapper rather than a seventh arm. The design has to be applicable to
    *whichever* policy is being validated -- there is no such thing as "the holdout
    policy", only a holdout applied to one -- and wrapping keeps the inner arm's selection
    exactly as it was, so the only difference between the two groups is whether the ask
    went out.
    """

    def __init__(self, inner: Policy, share: float = 0.5, salt: str = HOLDOUT_SALT) -> None:
        if not 0.0 < share < 1.0:
            raise ValueError(
                f"holdout share must be strictly between 0 and 1, got {share}. At 0 there "
                "is no control group and at 1 no treatment group; either way there is no "
                "contrast, which is a design error rather than an edge case."
            )
        self.inner = inner
        self.share = share
        self.salt = salt
        self.arm = f"{inner.arm}-holdout"
        self.selected: set[str] = set()
        """Every mandate the inner arm ever wanted to ask, across the whole horizon.

        This is the experiment's population, and it is not knowable in advance -- it is
        whatever the policy chose. Both groups are drawn from it, which is what makes the
        contrast a statement about the system rather than about the book.
        """

    def allocate(self, book: list[MandateWeek], budget_inr: float, week: int) -> AllocationResponse:
        response = self.inner.allocate(book, budget_inr, week)
        decisions: list[Decision] = []
        for decision in response.decisions:
            if decision.kind is not DecisionKind.ASKED:
                decisions.append(decision)
                continue
            self.selected.add(decision.mandate_id)
            if assigned_to_control(decision.mandate_id, self.share, self.salt):
                decisions.append(
                    Decision(
                        mandate_id=decision.mandate_id,
                        week=decision.week,
                        kind=DecisionKind.NOT_ASKED,
                        value_inr=0.0,
                        reason=(
                            f"held out: `{self.inner.arm}` chose to ask via "
                            f"{decision.channel}, and this mandate is in the "
                            f"{self.share:.0%} control group. The ask was withheld to "
                            "measure what it would have been worth."
                        ),
                    )
                )
                continue
            decisions.append(decision)

        # Recomputed rather than carried over: the withheld asks were never sent, so the
        # inner arm's spend figure is no longer this arm's spend.
        spent = response.budget_spent_inr
        sent = {d.mandate_id for d in decisions if d.kind is DecisionKind.ASKED}
        withheld = [d for d in response.decisions if d.kind is DecisionKind.ASKED]
        for decision in withheld:
            if decision.mandate_id not in sent:
                spent = max(0.0, spent - self._cost_of(decision, book))
        return AllocationResponse(decisions=decisions, theta_inr=None, budget_spent_inr=spent)

    def _cost_of(self, decision: Decision, book: list[MandateWeek]) -> float:
        """What the withheld ask would have cost. Needs the channel table, which the
        wrapper does not hold, so it is read off the inner policy when it has one."""
        channels = getattr(self.inner, "channels", None)
        if isinstance(channels, dict) and decision.channel in channels:
            return float(channels[decision.channel].cost_inr)
        params = getattr(self.inner, "params", None)
        if params is not None:
            for channel in params.channels:
                if channel.name == decision.channel:
                    return float(channel.cost_inr)
        return 0.0


class Estimate(BaseModel):
    """One holdout contrast, with the population it was measured on."""

    model_config = {"frozen": True}

    arm: str
    share: float
    treated: int = Field(ge=0)
    control: int = Field(ge=0)
    treated_survival: float
    control_survival: float

    @property
    def effect(self) -> float:
        """`P(active | do(send)) - P(active | do(drop))`, in survival probability."""
        return self.treated_survival - self.control_survival

    @property
    def usable(self) -> bool:
        """Both arms of the experiment have somebody in them."""
        return self.treated > 0 and self.control > 0


def estimate(metrics: RunMetrics, arm: RandomDrop) -> Estimate:
    """The contrast, on the mandates the policy chose to act on.

    Mandates the policy never wanted to contact are excluded from both groups. They are
    not controls -- nothing was withheld from them -- and including them would dilute the
    estimate with rows that carry no information about the intervention.
    """
    treated: list[float] = []
    control: list[float] = []
    for mandate_id in sorted(arm.selected):
        alive = metrics.alive_by_mandate.get(mandate_id)
        if alive is None:
            continue
        if assigned_to_control(mandate_id, arm.share, arm.salt):
            control.append(alive)
        else:
            treated.append(alive)
    return Estimate(
        arm=arm.inner.arm,
        share=arm.share,
        treated=len(treated),
        control=len(control),
        treated_survival=sum(treated) / len(treated) if treated else 0.0,
        control_survival=sum(control) / len(control) if control else 0.0,
    )


def naive_contrast(metrics: RunMetrics) -> tuple[float, float, int, int]:
    """The wrong number, computed on purpose: contacted against not-contacted.

    This is what a notification dashboard reports, and on a working system it usually comes
    out **negative** -- because the allocator contacts the mandates most likely to die, so
    the contacted group is sicker before anybody sends anything. Selection, not causation.

    It is here to be printed next to the holdout estimate. The gap between the two is the
    clearest available demonstration of why the randomisation is not ceremony.
    """
    contacted = [
        alive
        for mandate_id, alive in metrics.alive_by_mandate.items()
        if metrics.asks_by_mandate.get(mandate_id, 0) > 0
    ]
    untouched = [
        alive
        for mandate_id, alive in metrics.alive_by_mandate.items()
        if metrics.asks_by_mandate.get(mandate_id, 0) == 0
    ]
    return (
        sum(contacted) / len(contacted) if contacted else 0.0,
        sum(untouched) / len(untouched) if untouched else 0.0,
        len(contacted),
        len(untouched),
    )


def spread(
    book: list[BookMandate],
    params: Params,
    make_policy: Callable[[], Policy],
    budget_inr: float,
    salts: list[str],
    share: float = 0.5,
) -> list[Estimate]:
    """Re-draw the assignment under each salt and re-estimate: a randomisation distribution.

    The harness carries expectations rather than samples, so given one assignment the
    outcome is fixed and there is no sampling error to report. The assignment is the only
    random object in the design, so its own distribution is the honest uncertainty here.

    `make_policy` is a factory rather than an instance because the wrapped arms accumulate
    state -- `RandomDrop.selected` grows over a run, and `OnlineServing` holds a calibrated
    price -- and reusing one across draws would leak the first draw into the rest.
    """
    found = []
    for salt in salts:
        arm = RandomDrop(make_policy(), share=share, salt=salt)
        metrics = run(book, arm, params, budget_inr)
        found.append(estimate(metrics, arm))
    return found


def format_holdout(
    estimates: list[Estimate],
    naive: tuple[float, float, int, int],
    salts: list[str],
) -> str:
    """The two contrasts side by side, and what the spread says about detectability."""
    contacted, untouched, n_contacted, n_untouched = naive
    naive_effect = contacted - untouched
    effects = [e.effect for e in estimates]
    mean = sum(effects) / len(effects)
    variance = (
        sum((e - mean) ** 2 for e in effects) / (len(effects) - 1) if len(effects) > 1 else 0.0
    )
    deviation = variance**0.5
    headline = estimates[0]

    lines = [
        "| contrast | treated | control | P(alive) treated | P(alive) control | effect |",
        "|---|---:|---:|---:|---:|---:|",
        f"| naive: contacted vs untouched | {n_contacted:,} | {n_untouched:,} "
        f"| {contacted:.4f} | {untouched:.4f} | **{naive_effect:+.4f}** |",
        f"| holdout: sent vs withheld | {headline.treated:,} | {headline.control:,} "
        f"| {headline.treated_survival:.4f} | {headline.control_survival:.4f} "
        f"| **{headline.effect:+.4f}** |",
        "",
    ]

    if naive_effect < 0 < headline.effect:
        lines += [
            f"**The naive contrast has the wrong sign, and it is not close.** It reports "
            f"the system destroying {abs(naive_effect):.1%} of retention; the randomised "
            f"contrast on the same run reports it adding {headline.effect:.1%}. Nothing "
            "about the allocator differs between those two numbers -- only which "
            "comparison was made.",
            "",
            "The mechanism is the whole reason the design exists: the allocator contacts "
            "the mandates most likely to die, so the contacted group is sicker *before* "
            "anything is sent. Reading that gap as an effect measures the selection rule, "
            "not the intervention. This is the number a notification dashboard shows.",
        ]
    else:
        lines.append(
            f"Naive contrast {naive_effect:+.4f}, randomised contrast "
            f"{headline.effect:+.4f} on the same run."
        )

    lines += [
        "",
        f"**The randomisation distribution, over {len(salts)} independent draws of the "
        "assignment.** The harness carries expectations rather than samples, so given one "
        "assignment the outcome is fixed and there is no sampling error to quote. The "
        "assignment is the only random object in the design, so re-drawing it is the "
        "honest uncertainty here.",
        "",
        "| draw | treated | control | effect |",
        "|---|---:|---:|---:|",
    ]
    for salt, found in zip(salts, estimates, strict=True):
        lines.append(f"| `{salt}` | {found.treated:,} | {found.control:,} | {found.effect:+.4f} |")
    lines += [
        f"| **mean** | | | **{mean:+.4f}** |",
        f"| **spread (sd)** | | | **{deviation:.4f}** |",
        "",
    ]

    ratio = abs(mean) / deviation if deviation else float("inf")
    if ratio < 2.0:
        # Standard error falls as 1/sqrt(n), so the population has to grow by the square
        # of the shortfall in the ratio.
        needed = int(round(headline.treated + headline.control) * (2.0 / ratio) ** 2)
        lines += [
            f"**And the experiment cannot detect its own effect.** The mean is "
            f"{mean:+.4f} against a spread of {deviation:.4f} -- a ratio of {ratio:.1f}, "
            "where roughly 2 is the least you would want before calling an effect "
            "distinguishable from zero. On this book the allocator selects only "
            f"{headline.treated + headline.control:,} mandates, so each arm of the "
            "experiment holds about "
            f"{(headline.treated + headline.control) // 2:,}.",
            "",
            f"That is a **result about the pilot, not about the allocator**. Standard "
            f"error falls with the square root of the population, so reaching a ratio of "
            f"2 needs roughly **{needed:,} selected mandates** -- around "
            f"{needed / max(headline.treated + headline.control, 1):.0f}x this book's "
            "selected population. A merchant pilot sized like this one would return a "
            "number indistinguishable from zero however well the system worked, and that "
            "is worth knowing before running it rather than after.",
        ]
    else:
        lines.append(
            f"The mean effect is {mean:+.4f} against a spread of {deviation:.4f}, a ratio "
            f"of {ratio:.1f} -- large enough to be distinguishable from zero on this book."
        )
    return "\n".join(lines)
