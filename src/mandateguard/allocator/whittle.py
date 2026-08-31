"""T3.8 -- P5, the Whittle index. The first arm that can say "ask later".

Every arm up to here decides one week at a time. P4 solves the whole book at once, which
is a real advance over a sort, but its horizon is still seven days: it maximises this
week's value every week and has no way to express *this mandate is worth asking, but not
yet*. That sentence is the whole reason a multi-period formulation exists.

It is worth being precise about why it matters on this book, because the aggregate hides
it. `docs/results.md` §1 shows the median hazard barely moving across the horizon --
0.00164 in week 0, 0.00140 in week 11 -- which reads like a book with no timing structure
at all. That median is taken *across mandates*, and it conceals what happens *within* one:
the median mandate's hazard varies by a factor of **60** between its quietest and its
riskiest week, and the peak week is spread across all twelve. The hazard model keys on
days-to-coverage-end, so risk spikes as each renewal approaches and falls again after it.
Asking in the week before the spike is worth more than asking four weeks early, and no
single-week optimiser can know that.

The restless bandit
-------------------
Each mandate is its own Markov decision process; the budget is the only thing coupling
them. Whittle's relaxation prices that coupling with a subsidy `lambda` per rupee of ask
budget, exactly as T3.4 prices it with theta -- and once it is priced, the mandates
separate and each can be solved on its own.

State is `(week, asks so far, weeks since last ask)`. All three are needed and none is
decoration: the week fixes the hazard, the ask count drives backfire (`b(n)` climbs with
contact count), and the recency drives fatigue (`gamma * 0.5^(d/h)`, still worth 0.50 of a
rupee twelve weeks out). Dropping recency would make the model disagree with the pricer
about what an ask costs, which is `docs/seekha.md` #45 all over again.

```
V_T(n, d) = 0
V_t(n, d) = max(  (1 - h_t) * V_{t+1}(n, d+1)                            <- wait
                , max_c [ net(t,n,d,c) - lambda*k[c]
                          + (1 - p_die(t,n,c)) * V_{t+1}(n+1, 0) ]       <- ask via c
               )
```

`net(...)` is `value.price.Pricer`'s four-term price at `alive = 1`, unchanged. P3, P4, P5
and the harness all price an ask the same way; what separates them is only what they are
allowed to optimise over.

The index, and why it is not just "solve it"
--------------------------------------------
The **Whittle index** of a state is the subsidy at which acting and waiting are exactly
indifferent there. A high index means "act even when budget is expensive" -- it is an
urgency score, comparable across mandates, computed once and then read.

That is the property worth having. Re-solving the whole book's MDP every week under a
budget would be P4 with more arithmetic; an index can be computed offline and *ranked* at
serving time, which is the same shape T3.5's online rule has and the reason ARMMAN
(AAAI 2022) could deploy this over tens of thousands of mothers.

The free channel breaks the index, exactly as it broke T3.4's bracket
---------------------------------------------------------------------
`in_app` costs nothing, so `lambda * k = 0` for it at every subsidy: a free ask worth
making is worth making at any price of budget, and the indifference point runs off to
infinity. `docs/seekha.md` #54 records this happening once already, in `theta_search`'s
bracket, and the worklog predicted it would recur here.

So the index is defined over **budget-consuming channels only**, against a baseline of
"wait, or send something free". That is not a workaround -- it is the same conclusion
`mckp.py` reached from the other direction: with a zero-cost channel configured, the
budget rations *which channel*, never *whether*. There is nothing for an index to ration
about a free ask.

What this arm is not
--------------------
The index is a **heuristic**, not an optimum. Whittle's relaxation is only exactly optimal
in the infinite-population limit and requires indexability, which is not verified here and
is genuinely hard to verify. `docs/eval.md` §8 reports what it does against P4 rather than
claiming what it must do, and if it loses, that is the result.
"""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel

from mandateguard.allocator.base import Policy
from mandateguard.models import AllocationResponse, Decision, DecisionKind, MandateWeek
from mandateguard.policy.loader import Params
from mandateguard.value.channel_priors import build_ladder
from mandateguard.value.fatigue import fatigue_inr
from mandateguard.value.ltv import loss_on_lapse_inr, loss_on_revocation_inr

BISECTION_STEPS = 12
"""Halvings used to locate each index, once the bracket is around it.

The index is consumed as a **ranking**, not as a rupee figure, so precision past the point
where the order stops changing buys nothing. What this number must *not* be is small
enough for the allocation to depend on it -- see `WhittleSolver.index`, where a loose
bracket made the ask count move from 31 to 109 as the halvings went from 8 to 24. With a
per-mandate bracket the answer settles far sooner -- identically from six halvings onward at both a
tight and a saturating budget -- so twelve is double the depth at which it stops moving.
`tests/test_whittle.py` pins that it no longer moves."""

EXPANSIONS = 12
"""Doublings allowed while growing a mandate's bracket until acting stops paying.

Twelve doublings is 4,096x the seed, and the seed is already the scale the answer lives
on. Bounded rather than `while True`: an unbounded expansion loop is an infinite loop
waiting for a config change, which is the same reasoning `theta_search._bracket` uses."""


class Horizon(BaseModel):
    """The book as arrays, in the shape the backward induction wants.

    Built once. The MDP is identical in structure for every mandate -- only the hazard
    path and the two loss figures differ -- so the whole book is solved in one set of
    array operations rather than in a Python loop over 1,354 separate dynamic programs.
    """

    model_config = {"frozen": True, "arbitrary_types_allowed": True}

    mandate_ids: list[str]
    hazards: np.ndarray
    """(mandates, weeks) -- the projected hazard path."""
    lapse_loss_inr: np.ndarray
    revocation_loss_inr: np.ndarray

    @property
    def weeks(self) -> int:
        return int(self.hazards.shape[1])

    @property
    def size(self) -> int:
        return len(self.mandate_ids)


def horizon_from(book: list[MandateWeek], params: Params, weeks: int) -> Horizon:
    """Build the arrays the backward induction wants, from what the policy was given.

    `MandateWeek.hazard_path` carries the rest of the horizon when the caller has a
    forecast, and **every** arm is offered it -- see the field's own docstring. Without it
    there is nothing to plan the *timing* against: a mandate whose risk spikes in week 9 is
    indistinguishable from one whose risk is flat, and the only thing left for a
    multi-period model to reason about is the accumulation of backfire and fatigue.

    When the path is missing this falls back to projecting the current hazard flat. That is
    a real degradation and not a defensive default -- the API layer (T5.3) is handed one
    mandate in one week by a caller who may hold no forecast at all, and an arm has to
    answer rather than fail. `docs/eval.md` §8 measures what the fallback costs, by running
    the arm both ways.
    """
    ids = [entry.mandate_id for entry in book]
    rows = []
    for entry in book:
        path = entry.hazard_path or [entry.hazard]
        # Pad by repeating the last known hazard rather than by dropping to zero: an
        # unknown future is better modelled as "like the end of what we know" than as
        # "nothing ever dies again", which would make waiting look free.
        padded = list(path[:weeks])
        padded += [padded[-1]] * (weeks - len(padded))
        rows.append(padded)
    hazards = np.array(rows, dtype=float)
    return Horizon(
        mandate_ids=ids,
        hazards=hazards,
        lapse_loss_inr=np.array(
            [loss_on_lapse_inr(e.ltv_remaining_inr, e.recovery_after_lapse) for e in book],
            dtype=float,
        ),
        revocation_loss_inr=np.array(
            [
                loss_on_revocation_inr(
                    e.ltv_remaining_inr,
                    e.recovery_after_revocation,
                    e.reachability_value_inr,
                    params.value.alpha_reachability,
                )
                for e in book
            ],
            dtype=float,
        ),
    )


class WhittleSolver:
    """Backward induction over `(week, asks, recency)`, vectorised across the book."""

    def __init__(self, params: Params) -> None:
        self.params = params
        self.weeks = params.horizon.weeks
        ladder = build_ladder(params.channels, params.value.backfire_avoided_per_softer_step)

        channels = sorted(params.channels, key=lambda c: c.name)
        self.channel_names = [c.name for c in channels]
        self.cost = np.array([c.cost_inr for c in channels], dtype=float)
        self.paid = self.cost > 0
        # `(1 - uplift * efficacy)`, floored at zero for the same reason `Pricer` floors
        # it: a swept uplift above `1 / efficacy` would otherwise make hazard negative and
        # the simulation would start creating mandates.
        self.retained = np.array(
            [max(0.0, 1.0 - params.intervention.uplift_scale * c.efficacy_prior) for c in channels],
            dtype=float,
        )
        self.softness = np.array([ladder.backfire_multiplier(c) for c in channels], dtype=float)

        # b(n + 1): the backfire of the *next* ask when n have already been sent.
        self.backfire = np.array(
            [params.intervention.backfire(n + 1) for n in range(self.weeks + 1)], dtype=float
        )
        # fatigue at d weeks since the last ask; the final slot is "never asked", which
        # costs nothing and is a case `fatigue_inr` handles rather than approximates.
        self.fatigue = np.array(
            [
                fatigue_inr(d, params.value.gamma_fatigue, params.value.fatigue_half_life_days)
                for d in range(self.weeks + 1)
            ]
            + [0.0],
            dtype=float,
        )
        self.never = self.weeks + 1
        """Index of the never-asked recency slot. One past the last real one."""

    def tables(self, horizon: Horizon) -> list[tuple[np.ndarray, np.ndarray]]:
        """`(net, p_die)` for every week, computed once and passed down.

        Neither the net value of an ask nor its chance of killing the mandate depends on
        `lambda` -- the subsidy is subtracted afterwards. Rebuilding them inside every
        bisection step was the whole cost of this arm; hoisting them out took a ladder run
        from about 150 seconds to under 50.

        **Threaded as an argument rather than cached on the solver, and that is the second
        version.** The first memoised on `(id(horizon), week)`, reasoning that `Horizon` is
        frozen and rebuilt per call. `id()` is only unique among *live* objects: once a
        horizon was collected, CPython handed its address to the next one and the solver
        served the previous book's arrays for the new one. Nothing raised. It surfaced as
        an index that did not respond to the current week's hazard *at all* -- identical to
        six decimals at hazards from 0.10 to 0.30 -- which was very nearly written up as a
        finding about the value function.

        The lesson is narrow and worth keeping: `id()` is not an identity for a cache key,
        it is an address, and an address outlives nothing.
        """
        return [self._immediate(horizon, week) for week in range(self.weeks)]

    def _immediate(self, horizon: Horizon, week: int) -> tuple[np.ndarray, np.ndarray]:
        """Net rupee value of each ask, and the chance it kills the mandate.

        Returns `(net, p_die)` shaped `(mandates, channels, asks, recency)` and
        `(mandates, channels, asks)`. Deliberately the same four terms `value/price.py`
        computes, at `alive = 1`: this module must not become a second opinion on what an
        ask is worth.
        """
        hazard = horizon.hazards[:, week][:, None]  # (M, 1)
        effective = hazard * self.retained[None, :]  # (M, C)
        backfire = self.backfire[None, None, :] * self.softness[None, :, None]  # (1, C, N)

        prevented = (1.0 - backfire) * (hazard - effective)[:, :, None]  # (M, C, N)
        gain = self.params.value.mu_good_outcome * prevented * horizon.lapse_loss_inr[:, None, None]
        harm = (
            self.params.value.nu_complaint * backfire * horizon.revocation_loss_inr[:, None, None]
        )
        net = (gain - harm - self.cost[None, :, None])[:, :, :, None] - self.fatigue[
            None, None, None, :
        ]
        p_die = backfire + (1.0 - backfire) * effective[:, :, None]
        return net, p_die

    def solve(
        self,
        horizon: Horizon,
        lam: np.ndarray,
        tables: list[tuple[np.ndarray, np.ndarray]],
    ) -> list[np.ndarray]:
        """Value functions for every week, at a per-mandate subsidy.

        `lam` is an array rather than a scalar so that every mandate can be bisected on
        its own index in one pass. The mandates never interact once the budget is priced
        -- that is what Whittle's relaxation buys -- so solving them at different subsidies
        simultaneously is not an approximation, it is just bookkeeping.
        """
        asks = self.weeks + 1
        recency = self.weeks + 2
        shape = (horizon.size, asks, recency)
        values: list[np.ndarray] = [np.zeros(shape) for _ in range(self.weeks + 1)]

        # Where recency goes if nobody is contacted: one week staler, and "never" stays
        # "never". Precomputed because it is the same map every week.
        older = np.minimum(np.arange(recency) + 1, self.weeks)
        older[self.never] = self.never
        # Where the ask count goes after an ask, capped so the array stays finite.
        more = np.minimum(np.arange(asks) + 1, self.weeks)

        for week in range(self.weeks - 1, -1, -1):
            future = values[week + 1]
            survive = (1.0 - horizon.hazards[:, week])[:, None, None]
            best = survive * future[:, :, older]

            net, p_die = tables[week]
            after = future[:, more, 0][:, None, :, None]  # (M, 1, N, 1) -- once asked
            # Every channel at once. The Python-level loop this replaces built eight
            # full-sized temporaries per week per bisection step, and there are 24 steps
            # in every one of twelve weeks -- the loop, not the arithmetic, was the cost.
            candidates = (
                net
                - lam[:, None, None, None] * self.cost[None, :, None, None]
                + (1.0 - p_die)[:, :, :, None] * after
            )
            values[week] = np.maximum(best, candidates.max(axis=1))
        return values

    def _gap(
        self,
        horizon: Horizon,
        lam: np.ndarray,
        week: int,
        asks: np.ndarray,
        recency: np.ndarray,
        tables: list[tuple[np.ndarray, np.ndarray]],
    ) -> np.ndarray:
        """`best paid ask` minus `wait or send something free`, at this subsidy.

        Positive means the mandate is worth spending on at this price of budget. The
        function decreases in `lam` -- a dearer budget can only make paying less
        attractive -- which is what makes the bisection below valid.
        """
        values = self.solve(horizon, lam, tables)
        future = values[week + 1] if week + 1 <= self.weeks else np.zeros_like(values[0])
        rows = np.arange(horizon.size)

        older = np.minimum(recency + 1, self.weeks)
        older = np.where(recency == self.never, self.never, older)
        passive = (1.0 - horizon.hazards[:, week]) * future[rows, asks, older]

        net, p_die = tables[week]
        more = np.minimum(asks + 1, self.weeks)
        after = future[rows, more, 0]

        paid_best = np.full(horizon.size, -np.inf)
        free_best = passive
        for channel in range(len(self.channel_names)):
            value = net[rows, channel, asks, recency] + (1.0 - p_die[rows, channel, asks]) * after
            if self.paid[channel]:
                paid_best = np.maximum(paid_best, value - lam * self.cost[channel])
            else:
                free_best = np.maximum(free_best, value)
        return paid_best - free_best

    def index(
        self, horizon: Horizon, week: int, asks: np.ndarray, recency: np.ndarray
    ) -> np.ndarray:
        """The Whittle index of each mandate's current state: its urgency, in rupees.

        **Per-mandate bracket, seeded from the answer rather than from a bound.** The first
        version used one global ceiling -- the largest immediate net value times the horizon
        over the cheapest paid channel -- which is a valid bound and a terrible bracket: on
        this book it came out near 1,300 while a typical index is under a rupee, so most of
        the halvings were spent travelling rather than resolving. It showed. At 8 steps the
        arm made 31 asks, at 16 it made 108, at 24 it made 109; the *result* was still
        moving with the solver's stopping rule, which means it was not a result yet.

        That is `docs/seekha.md` #54 a second time, and in a nastier form -- there the loose
        bracket cost only tidiness, here it silently changed the allocation. A bracket wide
        enough to be obviously safe is not therefore harmless.

        So: solve once at `lambda = 0`, which gives each mandate the value of acting when
        budget is free. A mandate whose gap is already negative there will never act at any
        price, and its index is zero without any search. For the rest, `gap / cheapest paid
        channel` is the scale the answer lives on, and the bracket is grown from there by
        doubling until acting stops paying -- so the width is set by the mandate rather
        than by the book's worst case.
        """
        tables = self.tables(horizon)
        cheapest = float(self.cost[self.paid].min())
        zero = np.zeros(horizon.size)
        opening = self._gap(horizon, zero, week, asks, recency, tables)

        # Only mandates worth acting on when budget is free can have a positive index.
        active = opening > 0
        if not active.any():
            return zero

        high = np.maximum(opening, 0.0) / cheapest + 1e-9
        for _ in range(EXPANSIONS):
            beyond = active & (self._gap(horizon, high, week, asks, recency, tables) > 0)
            if not beyond.any():
                break
            high = np.where(beyond, high * 2.0, high)

        low = zero.copy()
        for _ in range(BISECTION_STEPS):
            middle = 0.5 * (low + high)
            positive = self._gap(horizon, middle, week, asks, recency, tables) > 0
            low = np.where(positive, middle, low)
            high = np.where(positive, high, middle)
        return np.where(active, low, 0.0)


class WhittleIndex(Policy):
    """P5 -- rank by urgency over the whole horizon, then fill the budget.

    The index already prices *waiting*, so the selection rule on top of it is deliberately
    plain: sort by index, take the ones worth acting on while the money lasts. Anything
    cleverer here would be a second allocator competing with the one inside the MDP.
    """

    arm = "P5"

    def __init__(self, params: Params) -> None:
        self.params = params
        self.solver = WhittleSolver(params)
        self.channels = {c.name: c for c in params.channels}

    def allocate(self, book: list[MandateWeek], budget_inr: float, week: int) -> AllocationResponse:
        if not book:
            return AllocationResponse(decisions=[], theta_inr=None, budget_spent_inr=0.0)

        ordered = sorted(book, key=lambda e: e.mandate_id)
        horizon = horizon_from(ordered, self.params, self.solver.weeks)
        asks = np.array([min(e.asks_so_far, self.solver.weeks) for e in ordered])
        recency = np.array(
            [
                self.solver.never
                if e.weeks_since_last_ask is None
                else min(e.weeks_since_last_ask, self.solver.weeks)
                for e in ordered
            ]
        )
        # The MDP plans over the whole horizon, but a policy is only ever asked about the
        # week it is in; the index is evaluated at week 0 of that plan.
        scores = self.solver.index(horizon, 0, asks, recency)

        paid = [name for name, c in self.channels.items() if c.cost_inr > 0]
        cheapest = min(self.channels[name].cost_inr for name in paid)

        chosen: dict[str, str] = {}
        spent = 0.0
        # Descending urgency, ties broken by id so the week is reproducible (ADR 0003).
        for position in sorted(
            range(len(ordered)), key=lambda i: (-scores[i], ordered[i].mandate_id)
        ):
            if scores[position] <= 0:
                break
            if spent + cheapest > budget_inr + 1e-9:
                break
            entry = ordered[position]
            # The index says *whether* this mandate is urgent; the pricer says which
            # channel is worth using, under what is left of the budget. Two questions,
            # and the index only answers the first.
            affordable = budget_inr - spent
            best = max(
                (
                    (self._value(entry, name), name)
                    for name in paid
                    if self.channels[name].cost_inr <= affordable
                ),
                default=None,
            )
            if best is None or best[0] <= 0:
                continue
            chosen[entry.mandate_id] = best[1]
            spent += self.channels[best[1]].cost_inr

        decisions = []
        for entry in book:
            channel = chosen.get(entry.mandate_id)
            position = horizon.mandate_ids.index(entry.mandate_id)
            if channel is None:
                decisions.append(
                    Decision(
                        mandate_id=entry.mandate_id,
                        week=week,
                        kind=DecisionKind.NOT_ASKED,
                        value_inr=0.0,
                        reason=(
                            f"not asked: Whittle index INR {scores[position]:,.4f} -- the "
                            "value of asking now, over waiting, did not clear the week's "
                            "cut-off."
                        ),
                    )
                )
                continue
            decisions.append(
                Decision(
                    mandate_id=entry.mandate_id,
                    week=week,
                    kind=DecisionKind.ASKED,
                    channel=channel,
                    value_inr=self._value(entry, channel),
                    reason=(
                        f"asked via {channel}: Whittle index INR {scores[position]:,.4f} "
                        "-- asking now beats waiting by more than any other mandate in "
                        "this week's budget."
                    ),
                )
            )
        return AllocationResponse(decisions=decisions, theta_inr=None, budget_spent_inr=spent)

    def _value(self, entry: MandateWeek, channel: str) -> float:
        """This week's rupee value of the ask, from the shared pricer.

        Reported on the decision rather than the index, because the ledger and the harness
        both speak in this week's rupees and the index is a horizon-wide urgency score.
        Putting the index in `value_inr` would silently change what the harness sums.
        """
        from mandateguard.value.price import Pricer

        if not hasattr(self, "_pricer"):
            self._pricer = Pricer(self.params)
        return self._pricer.price(entry, self.channels[channel]).net_inr
