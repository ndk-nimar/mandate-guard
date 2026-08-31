"""T3.3 -- P4, the multiple-choice knapsack. The first arm that is ours.

Everything below P4 answers one question: *who* gets asked. P4 answers two at once --
who, and *through which channel* -- under a shared budget, and that is what makes it a
knapsack rather than a sort.

```
maximise    sum over (i, c) of  profit[i,c] * x[i,c]
subject to  sum over c of x[i,c] <= 1        for every mandate i   (multiple CHOICE)
            sum over (i, c) of k[c] * x[i,c] <= B                  (the budget)
            x[i,c] in {0, 1}
```

Two things about this formulation are load-bearing, and `docs/problem.md` §5.2 argues
both. Without distinct per-channel costs the "knapsack" collapses into a greedy sort by
value, because every item weighs the same. And without the at-most-one constraint a
mandate could be contacted four ways in one week, which is not a plan, it is a mistake.

`profit` is not this module's invention
---------------------------------------
Every coefficient comes from `value.price.Pricer`, the same one P3 uses. If P4 optimised
a different objective from the one the harness scores, "the optimiser beats the greedy
sort" would be a claim about two value functions -- and `docs/seekha.md` #45 records what
happened the last time two files disagreed about the same arithmetic.

The coefficient is the **net** price, with the channel cost already subtracted, and the
constraint carries that same cost as the knapsack weight. That looks like double-counting
and is not: `docs/problem.md` §5.1 makes `k[c]` a genuine part of what an ask costs, so it
belongs in the value; and the budget is a *capacity cap* rather than a second bill, so it
belongs in the constraint. Money spent and capacity consumed are two mechanisms, not one
charged twice.

The first draft did put the gross profit in the objective, and the harness caught it: P4
bought 258 asks and returned a **negative** net value, because a pair can be gross-positive
and net-negative and a gross objective cannot tell the difference.

theta
-----
The shipping allocator solves the **integer** problem; only the **relaxation** supplies
the dual (ADR 0002). Both are solved, which costs a second solve and buys the single most
business-legible output this project has: *every extra rupee of ask budget returns theta
rupees of value, net of that rupee.* A dual from the integer problem does not exist, and a
relaxation-only allocation would hand out fractional asks.

theta rises as the budget tightens, which is the behaviour that makes it a price rather
than a statistic. It is not *strictly* monotone across every pair of budgets, and that is
LP rather than a bug: the value function of a knapsack is concave and piecewise linear, so
at a kink the subdifferential is an interval and CBC may report either one-sided
derivative. `tests/test_mckp.py` therefore asserts the economic property the spike
established -- relaxing the budget by a rupee raises the objective by about theta -- rather
than a monotone ordering the solver never promised.

A free channel changes what a refusal means
-------------------------------------------
`in_app` costs nothing, so it never consumes budget (`docs/problem.md` §5.3). The
consequence is sharper than it first looks: **with a zero-cost channel configured, no
mandate is ever refused for lack of budget.** Anything worth contacting at all can be
contacted for free, so every refusal is "not worth asking", never "we ran out of money".
The budget rations *which channel*, not *whether*. That is §5.1's thesis falling out of the
solver rather than being asserted at it -- and it is why the arm still asks only about a
hundred times out of a possible sixteen thousand: what stops it is fatigue and backfire,
not the budget.
"""

from __future__ import annotations

import pulp

from mandateguard.allocator import candidates as candidate_set
from mandateguard.allocator.base import Policy
from mandateguard.allocator.candidates import THETA_DECIMALS, Candidate
from mandateguard.models import AllocationResponse, Decision, DecisionKind, MandateWeek
from mandateguard.policy.loader import Params
from mandateguard.value.price import Pricer


class MCKPPolicy(Policy):
    """P4 -- solve the whole week's allocation at once, and price the budget."""

    arm = "P4"

    def __init__(self, params: Params, with_theta: bool = True, msg: bool = False) -> None:
        self.params = params
        self.pricer = Pricer(params)
        self.with_theta = with_theta
        self.msg = msg
        """`with_theta=False` skips the LP relaxation and halves the solve time.

        The sweeps use it. They run this arm dozens of times to answer questions about
        *parameters*, and theta is a per-run headline nobody reads off a heatmap cell --
        paying for 400 duals to print none of them is a straightforward waste. The ladder
        keeps it on, because that is where theta is published."""

    def candidates(self, book: list[MandateWeek], budget_inr: float) -> list[Candidate]:
        """The pairs this week's solve ranges over. Built by `allocator/candidates.py`,
        which T3.4's Lagrangian search reads from too -- so "the search reproduces the
        LP's dual" is a claim about two algorithms rather than about two candidate sets.
        """
        return candidate_set.build(self.pricer, self.params, book, budget_inr)

    def _build(self, candidates: list[Candidate], budget_inr: float, relaxed: bool):
        category = "Continuous" if relaxed else "Binary"
        problem = pulp.LpProblem("mandateguard_mckp", pulp.LpMaximize)
        variables = {
            index: pulp.LpVariable(f"x_{index}", lowBound=0, upBound=1, cat=category)
            for index in range(len(candidates))
        }
        problem += pulp.lpSum(
            candidates[index].profit_inr * variables[index] for index in variables
        )

        by_mandate: dict[str, list[int]] = {}
        for index, candidate in enumerate(candidates):
            by_mandate.setdefault(candidate.mandate_id, []).append(index)
        # At most one channel per mandate. This is the "multiple choice" in the name, and
        # without it a mandate could be emailed, texted and telephoned in the same week.
        for mandate_id, indices in by_mandate.items():
            problem += pulp.lpSum(variables[i] for i in indices) <= 1, f"one_channel_{mandate_id}"

        problem += (
            pulp.lpSum(candidates[i].cost_inr * variables[i] for i in variables) <= budget_inr,
            "budget",
        )
        return problem, variables

    def shadow_price(self, candidates: list[Candidate], budget_inr: float) -> float | None:
        """theta: rupees of value per extra rupee of ask budget, from the LP relaxation.

        `None` when the budget does not bind -- and that is a real answer, not a failure.
        A slack budget prices at zero because the next rupee buys nothing, which is
        precisely the situation `docs/results.md` §2 is in.
        """
        if not candidates:
            return None
        problem, _ = self._build(candidates, budget_inr, relaxed=True)
        status = problem.solve(pulp.PULP_CBC_CMD(msg=self.msg))
        if pulp.LpStatus[status] != "Optimal":
            return None
        dual = problem.constraints["budget"].pi
        if dual is None:
            return None
        # Clamped at zero. The dual of a `<=` constraint in a maximisation cannot be
        # negative; CBC returns a small negative one when the budget is slack, and
        # printing "-0.00" as a price would be a solver artefact wearing a finding's
        # clothes. Zero is the true answer there: the next rupee buys nothing.
        return round(max(0.0, float(dual)), THETA_DECIMALS)

    def unconstrained_optimum(
        self, candidates: list[Candidate], budget_inr: float
    ) -> dict[str, Candidate] | None:
        """The answer when the budget does not bind -- or `None` when it does.

        If every mandate takes its own best channel and the total still fits the budget,
        that selection **is** the optimum. The at-most-one constraint is per mandate, so
        with the shared constraint inactive the problem separates into independent
        one-mandate choices, and an exact solver cannot beat picking the largest
        coefficient in each. No approximation, no heuristic -- the LP just has nothing
        left to do.

        This is not a micro-optimisation. CBC runs as a separate process, and on this book
        the budget is slack in most weeks of most sweep cells: the sweeps were spawning
        roughly 660 solves and taking eleven minutes, nearly all of it process startup for
        problems whose answer was already known. GATE 2 regenerates `results.md` in CI on
        every push, so that cost is paid over and over by people who did not choose it.

        theta is 0 in this branch, and provably so rather than by convention: a slack
        constraint prices at zero because the next rupee buys nothing.
        """
        best: dict[str, Candidate] = {}
        for candidate in candidates:
            current = best.get(candidate.mandate_id)
            if current is None or (candidate.profit_inr, -candidate.cost_inr) > (
                current.profit_inr,
                -current.cost_inr,
            ):
                best[candidate.mandate_id] = candidate
        if sum(c.cost_inr for c in best.values()) <= budget_inr + 1e-9:
            return best
        return None

    def allocate(self, book: list[MandateWeek], budget_inr: float, week: int) -> AllocationResponse:
        candidates = self.candidates(book, budget_inr)
        chosen: dict[str, Candidate] = {}
        theta: float | None = None

        slack = self.unconstrained_optimum(candidates, budget_inr) if candidates else None
        if slack is not None:
            chosen = slack
            theta = 0.0 if self.with_theta else None
        elif candidates:
            problem, variables = self._build(candidates, budget_inr, relaxed=False)
            status = problem.solve(pulp.PULP_CBC_CMD(msg=self.msg))
            if pulp.LpStatus[status] != "Optimal":
                raise RuntimeError(
                    f"P4: CBC returned {pulp.LpStatus[status]} on {len(candidates)} "
                    f"candidates at a budget of {budget_inr:.2f}. The allocator has no "
                    "safe fallback here -- an infeasible or unbounded knapsack means the "
                    "model is wrong, not that the answer is 'ask nobody'."
                )
            for index, variable in variables.items():
                if variable.value() is not None and variable.value() > 0.5:
                    chosen[candidates[index].mandate_id] = candidates[index]
            theta = self.shadow_price(candidates, budget_inr) if self.with_theta else None

        decisions = []
        spent = 0.0
        for entry in book:
            candidate = chosen.get(entry.mandate_id)
            if candidate is None:
                decisions.append(
                    Decision(
                        mandate_id=entry.mandate_id,
                        week=week,
                        kind=DecisionKind.NOT_ASKED,
                        value_inr=0.0,
                        reason=self._refusal(entry, candidates, budget_inr),
                    )
                )
                continue
            spent += candidate.cost_inr
            decisions.append(
                Decision(
                    mandate_id=entry.mandate_id,
                    week=week,
                    kind=DecisionKind.ASKED,
                    channel=candidate.channel,
                    value_inr=candidate.price.net_inr,
                    reason=candidate.price.reason(),
                )
            )
        return AllocationResponse(decisions=decisions, theta_inr=theta, budget_spent_inr=spent)

    def _refusal(self, entry: MandateWeek, candidates: list[Candidate], budget_inr: float) -> str:
        """Why this mandate was not asked, in rupees.

        Two different refusals, and conflating them would make the ledger useless. A
        mandate with no positive-value channel was never worth asking; one that had a
        candidate and still lost was outbid by the budget. The second is the interesting
        one, and it is the sentence theta exists to make actionable.
        """
        best = self.pricer.best_channel(entry, budget_inr)
        if best is None:
            return self.pricer.price(
                entry, min(self.params.channels, key=lambda c: c.cost_inr)
            ).reason()
        return (
            f"not asked: the best available ask ({best.channel}, worth "
            f"INR {best.net_inr:,.2f}) lost the budget to higher-value mandates this week. "
            f"{len(candidates):,} candidate asks competed for INR {budget_inr:,.2f}."
        )
