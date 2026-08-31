"""T2.1 -- the evaluation harness: roll the world forward one week at a time.

Every arm in the ladder is measured by this loop and nothing else. For each of the twelve
weeks it hands the policy the live book and that week's budget, takes the decisions back,
enforces the budget, and advances every mandate's fate.

Expectations, not coin flips
----------------------------
Each mandate carries `alive[i] in [0,1]` -- the probability it survived to this week --
rather than a sampled alive/dead flag, and every outcome is scaled by it. Monte Carlo was
the alternative and was rejected for two reasons:

* **ADR 0003.** A sampled run is only reproducible if an RNG's state is threaded through
  every caller and never touched again. Expectations are reproducible by construction.
* **T2.7 needs a smooth curve.** The budget sweep has to show an inverted U with a visible
  asymmetry. Sampling noise on a 1,354-mandate book would drown a 16% effect unless the
  whole sweep were run many times over.

The cost is real and is stated in `docs/eval.md`: expectations report the mean outcome and
say nothing about its variance, so this harness cannot answer "how often does this policy
do worse than doing nothing".

What an ask does
----------------
Given the hazard `h` the mandate would face untouched, an ask through channel `c` when the
customer has already had `n - 1` asks:

```
b       = intervention.backfire(n)              # this ask irritates them into revoking
h_eff   = h * (1 - uplift_scale * efficacy[c])  # it saves a share of the deaths
P(dies this week) = b + (1 - b) * h_eff
```

Uplift multiplies the hazard rather than adding to a survival probability. Reading
`efficacy_prior` as an absolute conversion rate would let an ask "save" a mandate that was
never going to die this week, which is how a simulator ends up claiming a 40% lift --
against Adyen's ~6%, the most trustworthy public number in payments.

A death caused by an ask is **always** a revocation: that is what backfire means, and it
is priced at `loss_on_revocation`, which exceeds `loss_on_lapse` by construction. A death
that happens anyway splits by the measured natural mix (`mapping.md` §5.6).
"""

from __future__ import annotations

import duckdb
from pydantic import BaseModel, model_validator

from mandateguard.allocator.base import Policy
from mandateguard.models import Channel, DecisionKind, MandateWeek
from mandateguard.policy.loader import Params
from mandateguard.value.price import Pricer


class BookMandate(BaseModel):
    """One live mandate, with its whole projected hazard path.

    Deliberately plain data: the harness never touches DuckDB, so a three-mandate world
    can be written by hand in a test and run through exactly the code the full book runs
    through.
    """

    model_config = {"frozen": True}

    mandate_id: str
    hazards: list[float]
    ltv_remaining_inr: float
    reachability_value_inr: float
    recovery_after_lapse: float
    recovery_after_revocation: float

    @model_validator(mode="after")
    def _lapse_recovers_better_than_revocation(self) -> BookMandate:
        """`q > r`, checked at the earliest point the harness owns the data.

        `MandateWeek` checks it too, but that is one object per mandate per week: a book
        loaded with a bad pair would fail on the first week of the first arm, deep inside
        a run, rather than when the book was read.
        """
        if self.recovery_after_lapse <= self.recovery_after_revocation:
            raise ValueError(
                f"{self.mandate_id}: recovery_after_lapse (q={self.recovery_after_lapse}) "
                f"must exceed recovery_after_revocation "
                f"(r={self.recovery_after_revocation}); see docs/problem.md 6.2"
            )
        return self


class RunMetrics(BaseModel):
    """T2.2 -- the six numbers, plus what is needed to read them.

    Every one is an expectation over the survival weights, so `mandates_retained` is a
    fractional count and that is not a rounding error.
    """

    arm: str
    weeks: int
    mandates: int

    # The six.
    mandates_retained: float
    revocations_caused: float
    arr_retained_inr: float
    asks_spent: int
    net_value_inr: float
    theta_inr: float | None

    # What makes the six readable.
    lapses: float
    revocations_natural: float
    budget_spent_inr: float
    channel_cost_inr: float

    asks_by_mandate: dict[str, int] = {}
    """How many asks each mandate received over the whole horizon.

    T3.7 needs this and nothing else does: Pinterest's inverted-U is a claim about how
    contacts are *distributed across a population*, and the aggregate counts above cannot
    express a distribution. Populated always rather than behind a flag, because a second
    code path is a second thing that can drift (`docs/seekha.md` #45) and 1,354 integers
    is not a cost worth branching over.
    """

    @property
    def retention_rate(self) -> float:
        return self.mandates_retained / self.mandates if self.mandates else 0.0

    @property
    def profit_inr(self) -> float:
        """ARR retained less what was spent to retain it.

        The business-legible curve, and the one T2.7 sweeps. Backfire damage is already
        inside `arr_retained_inr` -- a revoked mandate simply is not in it -- so this is
        not double-counting: it is revenue kept minus money spent.

        `net_value_inr` answers a different question (what did the asks themselves
        create) and can be negative while profit is merely lower than the floor's. Both
        are reported because a reader who sees only one will misread it.
        """
        return self.arr_retained_inr - self.channel_cost_inr

    @property
    def inr_per_ask(self) -> float:
        """Net rupees created per ask. Negative means the arm is destroying value.

        Self-contained rather than measured against P0: an arm that has to be handed
        another arm's result before it can be scored cannot be run on its own, and the
        harness is used one arm at a time.
        """
        return self.net_value_inr / self.asks_spent if self.asks_spent else 0.0

    @property
    def line(self) -> str:
        theta = f"{self.theta_inr:,.2f}" if self.theta_inr is not None else "--"
        return (
            f"| {self.arm} | {self.mandates_retained:,.1f} | {self.retention_rate:.3%} "
            f"| {self.revocations_caused:,.1f} | {self.arr_retained_inr:,.0f} "
            f"| {self.asks_spent:,} | {self.inr_per_ask:,.2f} | {self.net_value_inr:,.0f} "
            f"| {theta} |"
        )


class BudgetExceeded(RuntimeError):
    """A policy spent more than it was given.

    Raised rather than clipped. A harness that silently trimmed an over-spending policy
    would report a budget-respecting result for a policy that does not respect budgets,
    and the whole ladder is a comparison at equal budget.
    """


class _State:
    """Mutable per-mandate simulation state. Deliberately not a pydantic model: this is
    rewritten `mandates x weeks` times and validation on every step is pure cost."""

    __slots__ = ("alive", "asks", "last_ask_week")

    def __init__(self) -> None:
        self.alive = 1.0
        self.asks = 0
        self.last_ask_week: int | None = None


def run(
    book: list[BookMandate],
    policy: Policy,
    params: Params,
    budget_inr_per_week: float | None = None,
) -> RunMetrics:
    """Run one policy over the horizon and return its metrics."""
    weeks = params.horizon.weeks
    budget = (
        params.horizon.budget_inr_per_week if budget_inr_per_week is None else budget_inr_per_week
    )
    channels = {c.name: c for c in params.channels}
    intervention = params.intervention
    pricer = Pricer(params)

    state = {m.mandate_id: _State() for m in book}

    asks_spent = 0
    channel_cost = 0.0
    budget_spent = 0.0
    revocations_caused = 0.0
    revocations_natural = 0.0
    lapses = 0.0
    net_value = 0.0
    theta: float | None = None

    for week in range(weeks):
        view = [
            MandateWeek(
                mandate_id=m.mandate_id,
                week=week,
                hazard=m.hazards[week],
                alive=state[m.mandate_id].alive,
                ltv_remaining_inr=m.ltv_remaining_inr,
                reachability_value_inr=m.reachability_value_inr,
                recovery_after_lapse=m.recovery_after_lapse,
                recovery_after_revocation=m.recovery_after_revocation,
                asks_so_far=state[m.mandate_id].asks,
                weeks_since_last_ask=(
                    None
                    if state[m.mandate_id].last_ask_week is None
                    else week - state[m.mandate_id].last_ask_week  # type: ignore[operator]
                ),
            )
            for m in book
        ]

        response = policy.allocate(view, budget, week)
        if len(response.decisions) != len(view):
            raise ValueError(
                f"{policy.arm} returned {len(response.decisions)} decisions for "
                f"{len(view)} mandates. The contract is total: a not-asked mandate is a "
                "record with a reason, not an omission -- that is the refusal ledger."
            )
        if response.theta_inr is not None:
            theta = response.theta_inr

        asked: dict[str, Channel] = {}
        week_cost = 0.0
        for decision in response.decisions:
            if decision.kind is not DecisionKind.ASKED:
                continue
            assert decision.channel is not None
            channel = channels.get(decision.channel)
            if channel is None:
                raise ValueError(f"{policy.arm} asked through unknown channel {decision.channel!r}")
            asked[decision.mandate_id] = channel
            week_cost += channel.cost_inr

        if week_cost > budget + 1e-9:
            raise BudgetExceeded(
                f"{policy.arm} spent {week_cost:.2f} of a {budget:.2f} budget in week "
                f"{week}. The ladder compares arms at equal budget, so an over-spending "
                "arm is not a better policy, it is a different experiment."
            )

        asks_spent += len(asked)
        channel_cost += week_cost
        budget_spent += week_cost

        for entry in view:
            current = state[entry.mandate_id]
            if current.alive <= 0.0:
                continue

            channel = asked.get(entry.mandate_id)
            if channel is None:
                backfire = 0.0
                effective = entry.hazard
            else:
                current.asks += 1
                current.last_ask_week = week
                # The world and the policies share one definition of what an ask does.
                # They did not, briefly, and it showed: the pricer softened backfire by
                # channel (Chrome, `value/channel_priors.py`) while this loop charged the
                # unsoftened rate, so P3 bought asks its own arithmetic called profitable
                # and the harness scored them at a loss. A policy is allowed to be wrong
                # about the world; it is not allowed to be wrong because two files
                # disagree about physics.
                backfire = pricer.backfire(entry, channel)
                effective = pricer.effective_hazard(entry, channel)
                net_value += pricer.price(entry, channel).net_inr

            caused = current.alive * backfire
            natural = current.alive * (1.0 - backfire) * effective

            revocations_caused += caused
            revocations_natural += natural * intervention.natural_revocation_share
            lapses += natural * (1.0 - intervention.natural_revocation_share)

            current.alive *= 1.0 - (backfire + (1.0 - backfire) * effective)

    retained = sum(state[m.mandate_id].alive for m in book)
    arr = sum(state[m.mandate_id].alive * m.ltv_remaining_inr for m in book)
    return RunMetrics(
        arm=policy.arm,
        weeks=weeks,
        mandates=len(book),
        mandates_retained=retained,
        revocations_caused=revocations_caused,
        arr_retained_inr=arr,
        asks_spent=asks_spent,
        net_value_inr=net_value,
        theta_inr=theta,
        lapses=lapses,
        revocations_natural=revocations_natural,
        budget_spent_inr=budget_spent,
        channel_cost_inr=channel_cost,
        # Sorted, because this ends up in a committed PNG and ADR 0003 asks for bytes.
        asks_by_mandate={
            m.mandate_id: state[m.mandate_id].asks for m in sorted(book, key=lambda e: e.mandate_id)
        },
    )


def load_book(con: duckdb.DuckDBPyConnection) -> list[BookMandate]:
    """Read the `forecast` table (see `eval/forecast.py`) into the harness's own shape."""
    rows = con.execute(
        """
        SELECT mandate_id,
               list(hazard ORDER BY week)      AS hazards,
               any_value(ltv_remaining_inr)    AS ltv,
               any_value(reachability_value_inr) AS reach,
               any_value(recovery_after_lapse) AS q,
               any_value(recovery_after_revocation) AS r
        FROM forecast GROUP BY mandate_id ORDER BY mandate_id
        """
    ).fetchall()
    return [
        BookMandate(
            mandate_id=str(mandate_id),
            hazards=[float(h) for h in hazards],
            ltv_remaining_inr=float(ltv),
            reachability_value_inr=float(reach),
            recovery_after_lapse=float(q),
            recovery_after_revocation=float(r),
        )
        for mandate_id, hazards, ltv, reach, q, r in rows
    ]


def format_metrics(results: list[RunMetrics]) -> str:
    """Markdown, because these numbers are due in results.md, not on a terminal."""
    if not results:
        return "_no arms run_"
    head = results[0]
    lines = [
        f"**{head.mandates:,} live mandates**, {head.weeks}-week horizon.",
        "",
        "| arm | retained | rate | revocations caused | ARR retained (INR) "
        "| asks | INR/ask | net value (INR) | theta |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    lines += [r.line for r in results]
    return "\n".join(lines)
