"""Losing the channel is its own loss. **Twitter/X, 2022.**

Twitter's result: modelling "will this user still be reachable" as a *separate term* from
"will this notification work" changed which notifications were worth sending. The two are
not the same quantity and folding them together loses the distinction that matters.

Here it is the most differentiating modelling choice in the project. On UPI AutoPay a
revocation does not merely end one subscription -- it removes the merchant's standing
authorisation rail to that customer. Every future mandate with them now starts from a cold
re-authorisation instead of a warm one. That option value is `R`, and `alpha` is its
weight in the objective.

Why it is a separate module rather than three characters inside `ltv.py`: because `R` is an
**overlay**. KKBox has no column for it and no public measurement of it exists
(`docs/calibration.md` §4), so it is `swept: true` and pinned to a fraction of `L` -- the
honest version of not knowing is one visible knob derived from a quantity that *is*
grounded, rather than a second invented rupee number floating free. Keeping it in its own
file keeps that visible.

**`P(still reachable)` is 1 here, and that is a modelling decision.** Twitter's term is
`alpha * P(still reachable)`; on a UPI AutoPay revocation the rail is gone by construction,
so the probability is one and the term collapses to `alpha * R`. On card or e-NACH a
revoked mandate may leave other contact intact, and that is exactly where a measured
probability below one would enter. Nothing here measures it, so nothing here claims it.
"""

from __future__ import annotations


def reachability_loss_inr(
    reachability_value_inr: float, alpha: float, still_reachable: float = 1.0
) -> float:
    """`alpha * R * P(the channel is actually lost)`.

    `still_reachable` defaults to 1: a UPI AutoPay revocation takes the rail with it. The
    argument exists so that a rail where that is not true has somewhere to say so, rather
    than needing this formula rewritten.
    """
    return alpha * reachability_value_inr * still_reachable
