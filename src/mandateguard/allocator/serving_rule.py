"""T3.5 -- LinkedIn's online rule: decide one mandate, without looking at the others.

P4 answers "who gets asked this week" by putting the whole book in front of a solver. That
is the right way to get the answer and the wrong shape for production. A live system is
handed **one** mandate -- a webhook fires, a customer opens the app -- and has to answer
inside a request, with no idea what the rest of the book is going to look like by Friday.

LinkedIn (KDD 2016) shipped the resolution, and T3.4 already built its engine. Once the
budget is priced at `theta`, the knapsack's coupling is gone and the decision is a
per-item threshold test:

```
ask through c  iff   mu * P(re-consent) * L_lapse
                   - nu * P(revoke) * L_revocation
                   - fatigue
                   - k[c]
                   - theta * k[c]     > 0
```

The first four terms are `value.price.Pricer` -- the same four-term price P3 and P4 use,
unchanged. The fifth is the only new thing, and it is one multiplication. That is the
whole rule.

theta is served, not solved
---------------------------
The price is **calibrated offline and then held**, which is what makes this online at all.
Recomputing the dual per request would just be P4 with extra steps. So a production
deployment recalculates theta on a schedule -- nightly, from yesterday's book -- and every
request in between reads a constant.

Which buys the shape and imports the risk: **theta goes stale.** The book moves under a
price that does not, and the two drift. `recalibrate_every` exposes exactly that axis, and
`docs/eval.md` §5 measures it rather than assuming it away. Holding theta fixed for the
whole horizon is the harshest honest setting and it is the default here, because the
flattering setting -- recalibrating every week -- is nearly P4 and would make the
comparison a formality.

The budget guarantee this rule does not have
--------------------------------------------
P4 cannot overspend: the budget is a constraint in its model. This rule has no such thing.
It decides each mandate on its own, so nothing stops a book that is richer than the
calibration book from spending past the cap -- and the harness raises `BudgetExceeded`
rather than clipping, correctly, because an over-spending arm is a different experiment
rather than a slightly worse one.

So the serving path carries a **spend meter**, which is what real systems carry. When the
chosen channel no longer fits, the rule falls back to the best channel that does -- often
the free one -- and records that it was capped. The cap is not decoration and its rate is
not an implementation detail: it is the measurement of how wrong the served price was, and
it is reported alongside the result rather than hidden inside it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from mandateguard.allocator import candidates as candidate_set
from mandateguard.allocator.base import Policy
from mandateguard.allocator.candidates import Candidate
from mandateguard.allocator.theta_search import ThetaSearch
from mandateguard.models import (
    AllocationResponse,
    Channel,
    Decision,
    DecisionKind,
    MandateWeek,
)
from mandateguard.policy.loader import Params
from mandateguard.value.price import AskPrice, Pricer


class Verdict(BaseModel):
    """One mandate's answer, and the arithmetic that produced it.

    Returned decomposed rather than as a bool for the same reason `AskPrice` is: the
    refusal ledger (T5.1) and `/explain` (T5.5) have to be able to say *why*, and "the
    budget's own price exceeded what this ask was worth" is an explanation that a bare
    `False` cannot give.
    """

    model_config = {"frozen": True}

    mandate_id: str
    channel: str | None
    price: AskPrice | None
    reduced_inr: float
    theta_inr: float
    capped: bool = Field(
        default=False,
        description="the rule wanted a dearer channel but the spend meter refused it",
    )

    @property
    def ask(self) -> bool:
        return self.channel is not None

    def reason(self) -> str:
        if self.price is None:
            return (
                "not asked: no channel clears the threshold once the budget is priced at "
                f"INR {self.theta_inr:.4f} per rupee. The best available ask is worth "
                f"INR {self.reduced_inr:,.2f} net of that price."
            )
        if self.capped:
            return (
                f"{self.price.reason()} (capped: the week's spend meter had run down, so "
                "this is the best channel that still fitted, not the best channel)"
            )
        return self.price.reason()


class ServingRule:
    """The per-item test. Sees one mandate and a spend allowance -- never the book.

    That restriction is the entire point and it is enforced by the signature rather than
    by discipline: `decide` takes a `MandateWeek`, not a list of them. A rule that could
    see the book could quietly start ranking, and "the online rule reproduces the batch
    solver" would stop being a claim about online serving.
    """

    def __init__(self, params: Params, theta_inr: float, pricer: Pricer | None = None) -> None:
        self.params = params
        self.theta_inr = theta_inr
        self.pricer = pricer or Pricer(params)

    def _best(self, entry: MandateWeek, affordable_inr: float) -> Candidate | None:
        """The highest reduced value among the channels this much money can reach.

        Ties break toward the cheaper channel, then alphabetically -- the same total order
        `theta_search.select` uses. That is not tidiness: the two have to agree at the same
        theta, or the batch-versus-online comparison in `docs/eval.md` §5 is measuring a
        tie-break rather than an algorithm.
        """
        best: Candidate | None = None
        best_key = (0.0, 0.0, "")
        for candidate in candidate_set.build(self.pricer, self.params, [entry], affordable_inr):
            reduced = candidate.reduced_inr(self.theta_inr)
            if reduced <= 0:
                continue
            key = (reduced, -candidate.cost_inr, candidate.channel)
            if best is None or key > best_key:
                best, best_key = candidate, key
        return best

    def decide(self, entry: MandateWeek, remaining_inr: float = float("inf")) -> Verdict:
        """Ask or not, and through what, for this one mandate.

        `remaining_inr` is the spend meter, not a budget: the rule is not optimising
        against it, it is only refusing to write a cheque that would bounce. Left at
        infinity, this is the pure LinkedIn rule with no cap at all -- the right default
        for a caller with its own rate limiting, and the wrong one inside a harness that
        raises on overspend.

        Both answers are computed: what the rule would pick with unlimited money, and what
        the meter actually permits. The second is the decision; the difference is the
        `capped` flag. Computing only the second would lose the diagnosis -- a rule quietly
        sending in-app notes because the money ran out looks identical, from the outside,
        to a rule that judged in-app the right channel.
        """
        wanted = self._best(entry, float("inf"))
        allowed = self._best(entry, remaining_inr)
        capped = wanted is not None and (allowed is None or allowed.channel != wanted.channel)

        if allowed is None:
            return Verdict(
                mandate_id=entry.mandate_id,
                channel=None,
                price=None,
                reduced_inr=wanted.reduced_inr(self.theta_inr) if wanted else 0.0,
                theta_inr=self.theta_inr,
                capped=capped,
            )
        return Verdict(
            mandate_id=entry.mandate_id,
            channel=allowed.channel,
            price=allowed.price,
            reduced_inr=allowed.reduced_inr(self.theta_inr),
            theta_inr=self.theta_inr,
            capped=capped,
        )


class OnlineServing(Policy):
    """P4o -- P4's price, served one mandate at a time. A variant of P4, not a new rung.

    The ladder's rungs (P0..P5) each change *what the allocator knows*. This one changes
    nothing about that: it is P4's own value function and P4's own price, applied without
    the solver. So it belongs beside P4 in the results rather than below it, and T3.5's
    point is precisely that the gap between them should be small.

    theta is calibrated once, on the first week this policy is asked to allocate, and then
    held. See the module docstring for why that is the honest default rather than the
    flattering one.
    """

    arm = "P4o"

    def __init__(
        self,
        params: Params,
        theta_inr: float | None = None,
        recalibrate_every: int | None = None,
    ) -> None:
        self.params = params
        self.pricer = Pricer(params)
        self.theta_inr = theta_inr
        self.recalibrate_every = recalibrate_every
        self.capped_decisions = 0
        """How often the spend meter overrode the rule, across the whole run.

        Kept on the policy rather than returned per week because it is a property of the
        *served price* and not of any one week: a theta calibrated too low caps late in
        every week, and one week's count would not show that.
        """

    def _theta_for(self, book: list[MandateWeek], budget_inr: float, week: int) -> float:
        """Calibrate if this is a calibration week, otherwise serve what is held.

        Calibration is the one moment this arm looks at the whole book, and it is the
        moment a production system would too -- offline, on a schedule, not in a request.
        """
        stale = self.theta_inr is None or (
            self.recalibrate_every is not None
            and self.recalibrate_every > 0
            and week % self.recalibrate_every == 0
        )
        if stale:
            pairs = candidate_set.build(self.pricer, self.params, book, budget_inr)
            self.theta_inr = ThetaSearch(repair=False).search(pairs, budget_inr).theta_inr
        assert self.theta_inr is not None
        return self.theta_inr

    def allocate(self, book: list[MandateWeek], budget_inr: float, week: int) -> AllocationResponse:
        theta = self._theta_for(book, budget_inr, week)
        rule = ServingRule(self.params, theta, self.pricer)
        costs: dict[str, Channel] = {c.name: c for c in self.params.channels}

        decisions: list[Decision] = []
        spent = 0.0
        # Book order, one at a time, with only a running total carried between them. This
        # loop is the claim: nothing here reads ahead, so a mandate's decision cannot
        # depend on a mandate that has not arrived yet.
        for entry in sorted(book, key=lambda e: e.mandate_id):
            verdict = rule.decide(entry, budget_inr - spent)
            if verdict.capped:
                self.capped_decisions += 1
            if verdict.channel is None:
                decisions.append(
                    Decision(
                        mandate_id=entry.mandate_id,
                        week=week,
                        kind=DecisionKind.NOT_ASKED,
                        value_inr=0.0,
                        reason=verdict.reason(),
                    )
                )
                continue
            spent += costs[verdict.channel].cost_inr
            assert verdict.price is not None
            decisions.append(
                Decision(
                    mandate_id=entry.mandate_id,
                    week=week,
                    kind=DecisionKind.ASKED,
                    channel=verdict.channel,
                    value_inr=verdict.price.net_inr,
                    reason=verdict.reason(),
                )
            )
        return AllocationResponse(decisions=decisions, theta_inr=theta, budget_spent_inr=spent)
