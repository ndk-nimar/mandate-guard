"""The `(mandate, channel)` pairs both Phase 3 solvers argue over.

T3.3 built this inline. T3.4 needs the identical set, and `docs/seekha.md` #45 records
what happened the last time two files computed the same arithmetic separately: the pricer
softened backfire by channel while the harness charged the unsoftened rate, so an arm
bought asks its own maths called profitable and the harness scored them at a loss. The
lesson generalises past that one bug -- if the knapsack and the Lagrangian search built
their candidate sets independently, "the search reproduces the LP's dual" would be a
claim about two sets, not about two algorithms.

So the pairs live here, once, and both import them.
"""

from __future__ import annotations

from pydantic import BaseModel

from mandateguard.models import MandateWeek
from mandateguard.policy.loader import Params
from mandateguard.value.price import AskPrice, Pricer

THETA_DECIMALS = 6
"""Decimal places kept on the published shadow price, whoever computed it.

CBC stops at a tolerance rather than at an exact optimum, and T3.4's bisection stops at a
bracket width -- so in both cases the last digits of theta are a property of the stopping
rule rather than of the book. The same reasoning rounds the hazard model's coefficients
(`risk/hazard.py`), and the same gate is behind it: GATE 2 asks for a byte-identical
`results.md`, and theta is printed in it."""


class Candidate(BaseModel):
    """One `(mandate, channel)` pair that is worth putting in front of a solver."""

    model_config = {"frozen": True}

    mandate_id: str
    channel: str
    profit_inr: float
    cost_inr: float
    price: AskPrice

    def reduced_inr(self, theta: float) -> float:
        """`profit - theta * cost`: what this ask is worth once budget is priced at theta.

        This single line is the Lagrangian relaxation of the budget constraint, and it is
        what lets `theta_search.py` decide every mandate independently and
        `serving_rule.py` (T3.5) decide them one at a time. At `theta = 0` it is the raw
        profit -- which is exactly the situation a slack budget describes.
        """
        return self.profit_inr - theta * self.cost_inr


def build(
    pricer: Pricer, params: Params, book: list[MandateWeek], budget_inr: float
) -> list[Candidate]:
    """Every affordable pair worth asking, in a fixed order.

    Pairs that lose money are dropped before any solver sees them. That is not an
    optimisation shortcut -- a maximiser would never select a negative coefficient
    anyway -- but it shrinks the problem by more than an order of magnitude on this book,
    because most mandates in a live book are nowhere near their coverage end.

    The order is `(mandate_id, channel)`, fixed, because CBC is free to choose any one of
    several equally-good optima and a model whose variables arrive in a different order
    each run is free to hand back a different one (ADR 0003). The search does not need
    the order for correctness, but it reports a selection, and a selection that reorders
    itself between runs would break the same gate.
    """
    found = []
    for entry in sorted(book, key=lambda e: e.mandate_id):
        for channel in sorted(params.channels, key=lambda c: c.name):
            if channel.cost_inr > budget_inr:
                continue
            price = pricer.price(entry, channel)
            if not price.worth_asking:
                continue
            found.append(
                Candidate(
                    mandate_id=entry.mandate_id,
                    channel=channel.name,
                    profit_inr=price.net_inr,
                    cost_inr=channel.cost_inr,
                    price=price,
                )
            )
    return found
