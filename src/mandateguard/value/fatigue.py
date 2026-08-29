"""Contact recently made costs more than contact long ago. **Duolingo, KDD 2020.**

Duolingo's finding was that the marginal harm of a notification depends on how recently the
last one landed, and that the decay is roughly exponential. The functional form used here
is theirs:

```
fatigue = gamma * 0.5 ** (d / half_life)
```

`d` is days since this customer was last contacted, `half_life` about fifteen days. Just
after a contact the penalty is the full `gamma`; a fortnight later it is half; after two
months it is close to nothing. Never contacted at all costs nothing, which is the boundary
case the formula has to get right because most of the book is in it.

**This is not the same thing as backfire, and the difference matters.** Backfire
(`intervention.backfire`) is the chance an ask *causes a revocation* and it climbs with the
number of asks. Fatigue is a smooth rupee penalty on *recency*, and it decays. One says
"the fifth message might make them cancel"; the other says "a message today, three days
after the last one, is worth less than the same message next month". A model with only the
first cannot express spacing, and spacing is most of what a multi-period allocator does.

The template-reuse penalty
--------------------------
Duolingo also penalised repeating the same message. `rho_template_reuse` is a flat charge
for sending a customer the template they last received, and it is **the one place the LLM
layer touches the optimiser's arithmetic**. Without it, T4.3's notice composer is decorative
-- a nicer sentence with no consequence for the allocation. With it, generating a genuinely
different notice is worth `rho` rupees, and the optimiser can see that.

Both constants are **swept** (`docs/calibration.md` §4). Duolingo gives the shape and an
approximate half-life; it does not give a rupee magnitude for an Indian mandate book.
"""

from __future__ import annotations

DAYS_PER_WEEK = 7


def fatigue_inr(
    weeks_since_last_ask: int | None,
    gamma: float,
    half_life_days: int,
    template_reused: bool = False,
    rho_template_reuse: float = 0.0,
) -> float:
    """The rupee penalty for contacting this customer now rather than later.

    `weeks_since_last_ask` is `None` for a customer never contacted, and that costs
    nothing -- the exponential would only reach zero in the limit, so the never-contacted
    case has to be handled rather than approximated.
    """
    if weeks_since_last_ask is None:
        recency = 0.0
    else:
        days = max(0, weeks_since_last_ask) * DAYS_PER_WEEK
        recency = gamma * 0.5 ** (days / half_life_days)
    return recency + (rho_template_reuse if template_reused else 0.0)
