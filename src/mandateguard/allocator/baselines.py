"""T2.4-T2.6 -- the three arms that are not ours, built before the one that is.

The order is deliberate, and it is the same discipline T1.6 used one layer down: build
the opponents first, so the harness cannot quietly be tuned to flatter the policy it was
written alongside.

Each arm is a real thing someone ships, not a straw man:

* **P1 `ChronologicalCap`** is the industry default. Braze, MoEngage and CleverTap all
  ship first-come-first-served-until-the-cap as out-of-the-box behaviour, so it is the
  number a merchant actually recognises. Its failure mode is not that it picks badly --
  it is that it picks *the same people* every week.
* **P2 `RoundRobin`** spends the same budget and rotates fairly. **Never cut this arm.**
  ARMMAN's AAAI 2022 result was credible precisely because round-robin was in the
  comparison: without it, "we beat doing nothing" and "we beat spreading contacts evenly"
  are indistinguishable, and only the second is interesting.
* **P3 `GreedyEV`** takes the top-B by expected rupee value. This is the honest simple
  baseline, and it is what a good deal of software marketed as "AI-powered" actually is.
  If Phase 3's optimiser cannot beat a sort, that is worth finding out here.

One channel, on purpose
-----------------------
All three send through the **cheapest intrusive channel** and never choose. That is not
laziness -- it is the line between the baselines and P4. These arms are about *who gets
asked*; choosing a channel per mandate against a cost table is the multiple-choice
knapsack (T3.3), and handing a baseline a channel optimiser would be building our own arm
twice and calling one of them a baseline.

It is also what campaign tools do: a campaign picks a channel, then a segment.
"""

from __future__ import annotations

from mandateguard.allocator.base import Policy
from mandateguard.models import (
    AllocationResponse,
    Channel,
    Decision,
    DecisionKind,
    MandateWeek,
)
from mandateguard.policy.loader import Params
from mandateguard.value.price import AskPrice, Pricer


def bulk_channel(channels: list[Channel]) -> Channel:
    """The channel a campaign tool would pick: the cheapest one that consumes budget.

    Non-intrusive channels are excluded even though they are cheaper still, because they
    cost nothing (`docs/problem.md` §5.3) -- an arm sending only in-app notifications
    would have an infinite budget and the ladder would be comparing nothing. Ties break
    toward the higher efficacy prior, so the rule is total (ADR 0003).
    """
    intrusive = [c for c in channels if c.intrusive]
    if not intrusive:
        raise ValueError(
            "no intrusive channel is configured, so no arm can spend its budget. "
            "config/params.yaml needs at least one channel with intrusive: true."
        )
    return min(intrusive, key=lambda c: (c.cost_inr, -c.efficacy_prior, c.name))


def _respond(
    book: list[MandateWeek],
    chosen: dict[str, float],
    channel: Channel,
    week: int,
    refusal: str,
) -> AllocationResponse:
    """Turn a selection into a *total* set of decisions.

    Every mandate gets a record, including the ones not asked, with a reason. That is
    the refusal ledger's whole basis: a system that can only explain what it did cannot
    explain what it declined to do, and the declining is most of what it does.
    """
    decisions = []
    spent = 0.0
    for entry in book:
        if entry.mandate_id in chosen:
            spent += channel.cost_inr
            decisions.append(
                Decision(
                    mandate_id=entry.mandate_id,
                    week=week,
                    kind=DecisionKind.ASKED,
                    channel=channel.name,
                    value_inr=chosen[entry.mandate_id],
                    reason=f"selected this week, contacted via {channel.name}",
                )
            )
        else:
            decisions.append(
                Decision(
                    mandate_id=entry.mandate_id,
                    week=week,
                    kind=DecisionKind.NOT_ASKED,
                    value_inr=0.0,
                    reason=refusal,
                )
            )
    return AllocationResponse(decisions=decisions, theta_inr=None, budget_spent_inr=spent)


def _slots(budget_inr: float, channel: Channel) -> int:
    """How many asks this week's budget buys. A free channel would buy unbounded asks,
    which `bulk_channel` already refuses to hand over."""
    if channel.cost_inr <= 0:
        raise ValueError("bulk channel must cost something, or the budget cannot bind")
    return int(budget_inr // channel.cost_inr)


class ChronologicalCap(Policy):
    """P1 -- first-come, first-served, until the budget runs out.

    The order is the book's own order, which is `mandate_id` -- stable, arbitrary, and
    exactly as informative about risk as a queue is. It reads neither `hazard` nor
    `alive`, and both omissions are faithful: a queue does not know who is about to
    churn, and it happily spends contacts on customers who are already gone.

    Because the order does not change between weeks, the same mandates are asked every
    week. That is not a bug in the implementation -- it is the failure mode P2 exists to
    isolate, and it is what makes the backfire ladder bite.
    """

    arm = "P1"

    def __init__(self, params: Params) -> None:
        self.channel = bulk_channel(params.channels)

    def allocate(self, book: list[MandateWeek], budget_inr: float, week: int) -> AllocationResponse:
        slots = _slots(budget_inr, self.channel)
        order = sorted(book, key=lambda entry: entry.mandate_id)
        chosen = {entry.mandate_id: 0.0 for entry in order[:slots]}
        return _respond(book, chosen, self.channel, week, "budget went to earlier arrivals")


class RoundRobin(Policy):
    """P2 -- the same budget, rotated fairly. **Never cut this arm.**

    Least-contacted first, ties broken by `mandate_id` so the rotation is deterministic.
    It still reads no risk signal, so the only thing separating it from P1 is *fairness
    across weeks* -- which isolates exactly one variable. If our arm beats P1 but not P2,
    then all it discovered was "stop hammering the same people", and that is a finding
    worth being unable to hide from.
    """

    arm = "P2"

    def __init__(self, params: Params) -> None:
        self.channel = bulk_channel(params.channels)

    def allocate(self, book: list[MandateWeek], budget_inr: float, week: int) -> AllocationResponse:
        slots = _slots(budget_inr, self.channel)
        order = sorted(book, key=lambda entry: (entry.asks_so_far, entry.mandate_id))
        chosen = {entry.mandate_id: 0.0 for entry in order[:slots]}
        return _respond(book, chosen, self.channel, week, "not this week's turn in the rotation")


class GreedyEV(Policy):
    """P3 -- top-B by expected rupee value of asking, computed per mandate per week.

    The value of one ask, in the currency the whole project uses:

    ```
    prevented = alive * (1 - b) * (h - h_eff)      deaths this ask avoids
    value     = prevented * loss_on_lapse
              - alive * b * loss_on_revocation      revocations it causes
              - channel cost
    ```

    Note what this already gets right and what it still misses. It prices backfire, and
    because `backfire(n)` climbs with contact count it will naturally stop re-asking the
    same customer -- so it is a genuinely strong opponent, not a straw man. What it
    cannot do is plan: it maximises *this week* every week, so it has no way to say "ask
    later, when the coverage clock is closer". That is what the `(mandate, channel, week)`
    decision variable in Phase 3 is for.
    """

    arm = "P3"

    def __init__(self, params: Params) -> None:
        self.channel = bulk_channel(params.channels)
        self.pricer = Pricer(params)

    def value_of_asking(self, entry: MandateWeek) -> AskPrice:
        """Priced by the shared value layer (T3.2), through this arm's one channel.

        P3 and P4 ask `value/` the same question and get the same answer. That is
        deliberate: if they priced asks differently, "the optimiser beats the greedy
        sort" would be a claim about two value functions, and this ladder exists to
        isolate **allocation**. What P4 gets that P3 does not is the choice of channel
        and a budget solved across the whole book at once -- not a better price.
        """
        return self.pricer.price(entry, self.channel)

    def allocate(self, book: list[MandateWeek], budget_inr: float, week: int) -> AllocationResponse:
        slots = _slots(budget_inr, self.channel)
        quotes = {entry.mandate_id: self.value_of_asking(entry) for entry in book}
        # Only positive-value asks are bought. A greedy policy that spent its whole
        # budget because it had one would be a worse policy than the sort it is meant to
        # represent -- and the refusal reason has to be able to say "this was not worth
        # it", not merely "someone else was ahead of you".
        order = sorted(
            (entry for entry in book if quotes[entry.mandate_id].worth_asking),
            key=lambda entry: (-quotes[entry.mandate_id].net_inr, entry.mandate_id),
        )
        chosen = {entry.mandate_id: quotes[entry.mandate_id].net_inr for entry in order[:slots]}
        return _respond(book, chosen, self.channel, week, "expected value below the week's cut-off")
