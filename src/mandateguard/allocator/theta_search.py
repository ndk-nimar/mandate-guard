"""T3.4 -- Pinterest's theta, as an algorithm rather than as a name.

T3.3 gets theta by handing the LP relaxation to CBC and reading the dual off the budget
constraint. That is correct and it does not scale: it needs a solver process, it needs the
whole book in one model, and it cannot answer "should I contact *this* mandate" without
re-solving everything. Pinterest (KDD 2018) ran notification volume control over hundreds
of millions of users with no LP in the serving path at all, and the trick is one line of
Lagrangian duality:

```
maximise  sum (i,c)  profit[i,c] * x[i,c]   subject to  sum k[c] * x[i,c] <= B
                                                        sum over c of x[i,c] <= 1

L(theta) = max_x  sum (profit[i,c] - theta * k[c]) * x[i,c]  +  theta * B
```

Price the budget at `theta` rupees per rupee and the budget constraint *disappears from
the problem*. What is left separates completely: every mandate picks the channel with the
best `profit - theta * cost` and asks only if that is positive, with no reference to any
other mandate. The coupling that made this a knapsack now lives entirely in one scalar.

So the whole allocation reduces to finding the right scalar, and that is what this module
does. theta is not a statistic reported after the fact -- it is the decision variable.

Why the search terminates, and why bisection is legitimate
----------------------------------------------------------
`spend(theta)` is **non-increasing**, and provably so rather than empirically. Each
mandate's reduced value is `max_c (profit[i,c] - theta * k[c])`: an upper envelope of
straight lines whose slopes are `-k[c]`. As theta rises the envelope's argmax moves to
lines that are *flatter*, which is to say to *cheaper* channels, and eventually below zero
where the mandate drops out. A mandate can therefore only ever get cheaper as theta rises,
never dearer, so the total can only fall. A monotone function is exactly what bisection
needs, and this one comes with the proof attached rather than with a chart.

The bracket is analytic too. Above the largest theta at which any paid ask still beats
its mandate's free fallback, nothing paid is ever selected, so `spend = 0` there for any
book. The hill-climb doubles upward from the *smallest* such crossing -- the point where
the first candidate changes hands -- so the search always starts inside the interval where
the answer can live and always finishes inside a bracket that is known to exist. A
doubling loop with no proof of an upper bracket is an infinite loop waiting for a config
change. `_ratios` has the argument for why those crossings, and not `profit / cost`, are
the right endpoints.

Where this lands versus CBC
---------------------------
`L(theta)` is convex, and its minimiser over `theta >= 0` *is* the LP relaxation's dual
price. So this search and `mckp.shadow_price` are two algorithms for one number, and
`tests/test_theta_search.py` asserts that they agree. That is the strongest check
available on either of them: a hand-rolled bisection agreeing with a mature
branch-and-cut solver's dual is evidence about the number, not about the code.

The step problem, which is real and is not hidden
-------------------------------------------------
`spend(theta)` is a *step* function -- it moves only when some mandate changes channel or
drops out -- so a theta that lands exactly on the budget generally does not exist. Between
the last theta that overspends and the first that fits there is a jump, and the width of
that jump is this instance's integrality gap. On a large book with cheap channels the
steps are small and the fit is tight; on a small book with a `letter` at INR 25, one step
can be most of the budget.

`repair` is what closes it. After the bisection the leftover slack is spent greedily on
the best available *upgrade* -- the largest gain in profit per extra rupee -- which is the
standard completion of a Lagrangian relaxation, and it is why T3.4's "within 2% of budget"
gate is reachable at all. It is a separate, named, switchable step precisely so the two
claims stay separable: the **unrepaired** theta is what should match CBC's dual, and the
**repaired** allocation is what should approach CBC's integer answer. Netting them into
one number would make a disagreement in either impossible to locate.

What "global budget" means here
-------------------------------
This module takes a candidate set and a budget and does not care where they came from.
Hand it one week's pairs and that week's budget and theta is a weekly price; hand it the
whole horizon's pairs and `weeks * budget` and it is the horizon-wide price T3.4 calls
global. What it cannot do either way is *plan* -- moving an ask from this week into a
later one changes that mandate's own state, and a static candidate set cannot express
that. That is the `(mandate, channel, week)` decision variable, and it is T3.8's job.
"""

from __future__ import annotations

import math

from pydantic import BaseModel, Field

from mandateguard.allocator import candidates as candidate_set
from mandateguard.allocator.candidates import THETA_DECIMALS, Candidate
from mandateguard.models import MandateWeek
from mandateguard.policy.loader import Params
from mandateguard.value.price import Pricer

MAX_ITERATIONS = 64
"""Bisection steps before the search stops and says how wide its bracket still is.

64 halvings shrink any bracket below what a float can represent, so reaching this cap
means the loop is not converging rather than that it needs longer. `converged` is
reported rather than raised on: a wide bracket still yields a usable price, and the
caller is better placed than this module to decide whether to publish it."""

BUDGET_TOLERANCE = 0.02
"""T3.4's stated gate: total asks land within 2% of budget."""

EPSILON = 1e-12
"""Guard on the `profit / cost` ratios. Costs are read from a hand-edited YAML file, and
a channel priced at 0.0 that is nonetheless marked intrusive would divide by zero here --
a config mistake should not surface as a ZeroDivisionError four modules away."""


class ThetaSolution(BaseModel):
    """A converged shadow price, the allocation it implies, and the evidence for both.

    Deliberately not a bare float. Whether the search converged, how tightly the spend
    fits the budget, and how much of the fit the repair step did are all things
    `docs/eval.md` has to be able to state, and a bare float forces every one of them to
    be recomputed by the caller -- which is how two files end up disagreeing about one
    number (`docs/seekha.md` #45).
    """

    model_config = {"frozen": True}

    theta_inr: float = Field(ge=0)
    binding: bool
    chosen: dict[str, Candidate]
    budget_inr: float
    iterations: int = Field(ge=0)
    bracket_inr: float = Field(ge=0, description="width of the final bisection bracket")
    converged: bool
    upgrades: int = Field(ge=0, description="asks the repair step improved with the slack")

    @property
    def spend_inr(self) -> float:
        return sum(c.cost_inr for c in self.chosen.values())

    @property
    def value_inr(self) -> float:
        """Net rupees the selection creates. The objective, not the Lagrangian."""
        return sum(c.profit_inr for c in self.chosen.values())

    @property
    def asks(self) -> int:
        return len(self.chosen)

    @property
    def paid_asks(self) -> int:
        """Asks that actually consumed budget.

        The free channel makes this the honest count for "did the asks match the budget":
        an in-app nudge is a real ask that costs nothing, so counting it against a rupee
        budget would flatter the fit.
        """
        return sum(1 for c in self.chosen.values() if c.cost_inr > 0)

    @property
    def utilisation(self) -> float:
        """Share of the budget actually spent. 1.0 is a perfect fit."""
        return self.spend_inr / self.budget_inr if self.budget_inr > 0 else 0.0

    @property
    def gap_fraction(self) -> float:
        """How far the spend sits from the budget, as a fraction of it.

        Zero when the budget does not bind: an unspent budget is not a miss there, it is
        the answer. Reporting the shortfall anyway would make a correct slack result look
        like a convergence failure, which is exactly the misreading T3.4's "within 2%"
        gate invites.
        """
        if not self.binding or self.budget_inr <= 0:
            return 0.0
        return abs(self.budget_inr - self.spend_inr) / self.budget_inr

    def within(self, tolerance: float = BUDGET_TOLERANCE) -> bool:
        return self.gap_fraction <= tolerance

    def summary(self) -> str:
        """One line for `docs/eval.md`. Numbers get printed, never retyped."""
        if not self.binding:
            return (
                f"theta = INR 0.000000 -- the budget does not bind. {self.asks:,} asks "
                f"({self.paid_asks:,} paid) spend INR {self.spend_inr:,.2f} of "
                f"INR {self.budget_inr:,.2f}; the next rupee buys nothing."
            )
        return (
            f"theta = INR {self.theta_inr:.6f} after {self.iterations} steps "
            f"(bracket INR {self.bracket_inr:.2e}). {self.asks:,} asks "
            f"({self.paid_asks:,} paid) spend INR {self.spend_inr:,.2f} of "
            f"INR {self.budget_inr:,.2f} -- {self.utilisation:.2%} of budget, "
            f"{self.gap_fraction:.2%} off. {self.upgrades:,} repair upgrades."
        )


def select(candidates: list[Candidate], theta: float) -> dict[str, Candidate]:
    """Each mandate's best ask once budget is priced at `theta`, or none if none pays.

    This is the entire Lagrangian sub-problem, and there is no budget in the loop -- that
    is the point. Every mandate is decided in isolation, which is what makes T3.5's online
    serving rule possible and what let Pinterest run this over hundreds of millions of
    users with no solver in the request path.

    Ties break toward the **cheaper** channel, then alphabetically. Both halves earn their
    place. Cheaper-first is what keeps `spend` non-increasing across a crossing point: at
    the theta where two channels are worth exactly the same, taking the dearer one would
    put a step *up* into a function the bisection assumes only ever steps down.
    Alphabetical is ADR 0003 -- a selection that reorders itself between runs breaks the
    byte-identical gate whether or not it costs the same.
    """
    best: dict[str, tuple[float, float, str]] = {}
    chosen: dict[str, Candidate] = {}
    for candidate in candidates:
        reduced = candidate.reduced_inr(theta)
        if reduced <= 0:
            continue
        key = (reduced, -candidate.cost_inr, candidate.channel)
        current = best.get(candidate.mandate_id)
        if current is None or key > current:
            best[candidate.mandate_id] = key
            chosen[candidate.mandate_id] = candidate
    return chosen


def spend_at(candidates: list[Candidate], theta: float) -> float:
    """What the Lagrangian selection costs at this price. The function being inverted."""
    return sum(c.cost_inr for c in select(candidates, theta).values())


def _round_up(value: float, decimals: int) -> float:
    """Round away from zero, so a published price never buys more than it was checked for.

    `round()` would go to nearest, which lands below the true crossing about half the
    time -- and below the crossing, candidates the bisection had priced out come back in
    and the spend can exceed the budget. Rounding up only ever removes candidates.
    """
    scale = 10.0**decimals
    return math.ceil(value * scale) / scale


def _ratios(candidates: list[Candidate]) -> list[float]:
    """Every theta at which a paid ask stops being worth its price. The search interval.

    The obvious quantity is `profit / cost` -- the theta at which a candidate's reduced
    value hits zero -- and it is the wrong one whenever a **free** channel is configured.
    `in_app` costs nothing, so its reduced value is `profit` at every theta: it never
    falls. A paid channel therefore does not have to beat zero, it has to beat the free
    fallback, and it stops doing that at

        (profit[i,c] - best free profit for i) / k[c]

    which is strictly smaller. Bracketing on `profit / cost` puts the whole hill-climb
    *above* the region where anything actually changes hands, so the climb terminates
    immediately and hands the bisection a bracket several times wider than it needs. The
    answer still converges -- bisection on a valid bracket does not care how loose it is
    -- but the climb T3.4 asks for is then decorative, and a ceiling that is not tight
    cannot be used to reason about how many steps the search takes.

    Non-positive ratios are dropped: a paid channel already worth less than the free one
    at `theta = 0` is never selected at any price, so it is not a crossing point.
    """
    free_profit: dict[str, float] = {}
    for candidate in candidates:
        if candidate.cost_inr <= 0:
            free_profit[candidate.mandate_id] = max(
                free_profit.get(candidate.mandate_id, 0.0), candidate.profit_inr
            )
    crossings = [
        (candidate.profit_inr - free_profit.get(candidate.mandate_id, 0.0)) / candidate.cost_inr
        for candidate in candidates
        if candidate.cost_inr > 0
    ]
    return sorted(ratio for ratio in crossings if ratio > EPSILON)


def upgrades(
    candidates: list[Candidate], chosen: dict[str, Candidate]
) -> list[tuple[float, float, str, Candidate]]:
    """Every move that would raise a mandate's profit by spending more on it.

    A move is `(profit per extra rupee, extra profit, mandate_id, candidate)`. Both a
    mandate climbing the channel ladder and a mandate being contacted at all count -- an
    unselected mandate is just one whose current profit and cost are zero.

    Sorted by value per rupee, then by absolute gain, then by name, so the ordering is
    total (ADR 0003). Ratio alone leaves ties, and a tie leaves the answer up to whatever
    order the dictionary happened to be built in.
    """
    by_mandate: dict[str, list[Candidate]] = {}
    for candidate in candidates:
        by_mandate.setdefault(candidate.mandate_id, []).append(candidate)

    moves: list[tuple[float, float, str, Candidate]] = []
    for mandate_id, options in by_mandate.items():
        current = chosen.get(mandate_id)
        base_profit = current.profit_inr if current else 0.0
        base_cost = current.cost_inr if current else 0.0
        for option in options:
            extra_cost = option.cost_inr - base_cost
            extra_profit = option.profit_inr - base_profit
            if extra_cost <= 0 or extra_profit <= 0:
                continue
            moves.append((extra_profit / extra_cost, extra_profit, mandate_id, option))
    moves.sort(key=lambda move: (-move[0], -move[1], move[2], move[3].channel))
    return moves


def affordable_upgrades(
    candidates: list[Candidate], chosen: dict[str, Candidate], budget_inr: float
) -> list[tuple[float, float, str, Candidate]]:
    """The moves that would still both pay and *fit* in what is left of the budget.

    This is how a shortfall against T3.4's "+-2% of budget" gate gets diagnosed instead of
    excused. An allocation that leaves money unspent is either leaving value on the table
    -- in which case this list is non-empty and the repair has a bug -- or it has run out
    of things worth buying at that price, in which case the list is empty and no allocator
    could have done better. Those are opposite conclusions from the same visible symptom,
    and only this distinguishes them.
    """
    spend = sum(c.cost_inr for c in chosen.values())
    slack = budget_inr - spend
    return [
        move
        for move in upgrades(candidates, chosen)
        if move[3].cost_inr - (chosen[move[2]].cost_inr if move[2] in chosen else 0.0)
        <= slack + EPSILON
    ]


def _repair(
    candidates: list[Candidate], chosen: dict[str, Candidate], budget_inr: float
) -> tuple[dict[str, Candidate], int]:
    """Spend the leftover slack on the best upgrades that still fit.

    The bisection stops at a step, and the step is generally not flush with the budget.
    Whatever is left over is real money the allocation declined to use, so this walks the
    available upgrades in descending order of extra profit per extra rupee, taking each
    that fits. That ordering is the greedy knapsack rule, and it is the standard
    completion of a Lagrangian relaxation for the same reason it works there: with items
    far smaller than the budget, greedy lands within one item of the LP bound.

    One upgrade per mandate, one pass. A mandate that could profitably climb two rungs
    climbs one, which understates the repair rather than overstating it -- and the honest
    direction to be wrong in is the one that makes our own arm look worse.
    """
    spend = sum(c.cost_inr for c in chosen.values())
    upgraded: set[str] = set()
    for _, _, mandate_id, option in upgrades(candidates, chosen):
        if mandate_id in upgraded:
            continue
        current = chosen.get(mandate_id)
        extra_cost = option.cost_inr - (current.cost_inr if current else 0.0)
        if spend + extra_cost > budget_inr + EPSILON:
            continue
        chosen[mandate_id] = option
        spend += extra_cost
        upgraded.add(mandate_id)
    return chosen, len(upgraded)


class ThetaSearch:
    """Hill-climb to a bracket, bisect inside it, then spend what the step left behind."""

    def __init__(
        self,
        max_iterations: int = MAX_ITERATIONS,
        tolerance: float = BUDGET_TOLERANCE,
        repair: bool = True,
    ) -> None:
        self.max_iterations = max_iterations
        self.tolerance = tolerance
        self.repair = repair

    def search(self, candidates: list[Candidate], budget_inr: float) -> ThetaSolution:
        """The converged price, and the allocation it buys."""
        free = select(candidates, 0.0)
        if not candidates or sum(c.cost_inr for c in free.values()) <= budget_inr + EPSILON:
            # theta = 0 is not a failure to find a price -- it *is* the price. A slack
            # constraint is worth nothing at the margin because the next rupee buys
            # nothing, which is exactly the situation `docs/results.md` §2 reports.
            return ThetaSolution(
                theta_inr=0.0,
                binding=False,
                chosen=free,
                budget_inr=budget_inr,
                iterations=0,
                bracket_inr=0.0,
                converged=True,
                upgrades=0,
            )

        low, high, climbs = self._bracket(candidates, budget_inr)
        bisections = 0
        while bisections < self.max_iterations and high - low > EPSILON:
            middle = 0.5 * (low + high)
            bisections += 1
            if spend_at(candidates, middle) > budget_inr + EPSILON:
                low = middle
            else:
                high = middle

        # Round the price FIRST, then select at the price that will actually be published.
        # The other order is a trap: `chosen` would be the selection at the full-precision
        # bracket while `theta_inr` carried a rounded number, so anyone re-deriving the
        # allocation from the published price -- which is exactly what T3.5's online rule
        # does -- would get a different answer from the one reported here. Two files
        # disagreeing about one piece of arithmetic is the recurring bug in this project
        # (`docs/seekha.md` #45), and publishing a price that does not reproduce its own
        # selection is that bug in its purest form.
        #
        # Rounded *up*, never to nearest. A higher price can only drop candidates, so the
        # selection shrinks and the budget stays respected. Rounding down would admit
        # candidates the bisection had excluded and could push the spend over the cap.
        theta = _round_up(high, THETA_DECIMALS)
        chosen = select(candidates, theta)
        upgrades = 0
        if self.repair:
            chosen, upgrades = _repair(candidates, chosen, budget_inr)

        solution = ThetaSolution(
            theta_inr=theta,
            binding=True,
            chosen=chosen,
            budget_inr=budget_inr,
            iterations=climbs + bisections,
            bracket_inr=high - low,
            converged=bisections < self.max_iterations,
            upgrades=upgrades,
        )
        if solution.spend_inr > budget_inr + EPSILON:  # pragma: no cover - guarded above
            raise RuntimeError(
                f"theta search returned a selection spending INR {solution.spend_inr:.2f} "
                f"against a budget of INR {budget_inr:.2f}. Bisection keeps the feasible "
                "side of the bracket and the repair only takes upgrades that fit, so this "
                "cannot happen unless one of those two is wrong."
            )
        return solution

    def _bracket(self, candidates: list[Candidate], budget_inr: float) -> tuple[float, float, int]:
        """The hill-climb: double upward until the spend fits, and count the steps.

        Starts at the smallest crossing, which is the first theta at which anything at
        all changes hands, and stops at the largest, above which nothing paid survives.
        Both ends come out of the candidate set, so the climb needs no magic constant and
        no unit -- rupees or paise, the same number of doublings.
        """
        ratios = _ratios(candidates)
        if not ratios:  # pragma: no cover - the slack branch takes this case first
            raise RuntimeError(
                "the budget binds but no paid ask ever beats its free fallback, so there "
                "is no theta at which the spend changes. `search` treats a spend that "
                "already fits as slack, so reaching here means that check and this one "
                "disagree about the same selection."
            )
        ceiling = ratios[-1]
        theta = max(ratios[0], EPSILON)
        low = 0.0
        climbs = 0
        while theta < ceiling and spend_at(candidates, theta) > budget_inr + EPSILON:
            low = theta
            theta *= 2.0
            climbs += 1
        return low, min(theta, ceiling), climbs


def from_book(
    params: Params,
    book: list[MandateWeek],
    budget_inr: float,
    pricer: Pricer | None = None,
    search: ThetaSearch | None = None,
) -> ThetaSolution:
    """Price a week of the live book directly, without assembling candidates by hand.

    Uses `allocator/candidates.py`, which is the same set `MCKPPolicy` solves over -- so a
    disagreement between the two is about the algorithms and never about the input.
    """
    priced = pricer or Pricer(params)
    pairs = candidate_set.build(priced, params, book, budget_inr)
    return (search or ThetaSearch()).search(pairs, budget_inr)
