"""T3.2 -- the four-term rupee price of one ask, assembled from four papers.

Every arm from P3 onward asks this module the same question: *what is it worth to contact
this mandate, through this channel, this week?* Sharing one answer is deliberate. If P3
and P4 priced asks differently, "the optimiser beats the greedy sort" would be a statement
about two value functions, and the ladder is supposed to isolate **allocation**.

```
net =   mu * P(death this ask prevents) * loss_on_lapse        <- Pinterest, LinkedIn
      - nu * P(revocation this ask causes) * loss_on_revocation <- LinkedIn, Twitter/X
      - fatigue(days since last contact, template reuse)        <- Duolingo
      - k[channel]                                              <- the cost table
```

with the backfire probability itself scaled by how soft the channel is (Chrome).

The one arithmetic decision worth arguing
-----------------------------------------
Uplift **multiplies the hazard** rather than adding to a survival probability:

```
h_effective = h * (1 - uplift_scale * efficacy_prior[c])
```

So an ask saves a *share of the deaths that would have happened*. The alternative reading
-- `efficacy_prior` as an outright conversion rate -- lets an ask "save" a mandate that was
never going to die this week, which is exactly how a simulator ends up reporting a 40%
lift. Adyen's contextual bandit beats a fixed retry schedule by about 6%, and that is the
most trustworthy public number in payments; anything near 40% is a red flag, not a result.

What is *not* in here
---------------------
No arm-specific logic, no budget, no ranking. This module prices one ask in isolation and
returns the decomposition rather than a single float, so `/explain` (T5.5) and the refusal
ledger (T5.1) can show a customer-facing reason with the four terms in it instead of one
number nobody can argue with.
"""

from __future__ import annotations

from pydantic import BaseModel

from mandateguard.models import Channel, MandateWeek
from mandateguard.policy.loader import Params
from mandateguard.value.channel_priors import ChannelLadder, build_ladder
from mandateguard.value.fatigue import fatigue_inr
from mandateguard.value.ltv import loss_on_lapse_inr, loss_on_revocation_inr
from mandateguard.value.prices import Prices


class AskPrice(BaseModel):
    """One ask, priced, with its terms kept apart.

    Returned decomposed rather than netted because the refusal ledger has to be able to
    say *why* -- "the backfire cost exceeded the value of the deaths it would prevent" is
    an explanation; "-2.67" is not.
    """

    model_config = {"frozen": True}

    mandate_id: str
    week: int
    channel: str
    deaths_prevented: float
    revocations_caused: float
    gain_inr: float
    backfire_inr: float
    fatigue_inr: float
    channel_cost_inr: float

    @property
    def net_inr(self) -> float:
        return self.gain_inr - self.backfire_inr - self.fatigue_inr - self.channel_cost_inr

    @property
    def worth_asking(self) -> bool:
        return self.net_inr > 0

    def reason(self) -> str:
        """A plain-language justification, in rupees. Feeds `Decision.reason` (T4.5)."""
        if self.worth_asking:
            return (
                f"asking via {self.channel} is worth INR {self.net_inr:,.2f}: it prevents "
                f"{self.deaths_prevented:.4f} expected lapses worth INR {self.gain_inr:,.2f}, "
                f"against INR {self.backfire_inr:,.2f} of revocation risk, "
                f"INR {self.fatigue_inr:,.2f} of fatigue and INR {self.channel_cost_inr:,.2f} "
                "of channel cost"
            )
        return (
            f"not asked: via {self.channel} the expected value is INR {self.net_inr:,.2f}. "
            f"It would prevent only INR {self.gain_inr:,.2f} of lapses while risking "
            f"INR {self.backfire_inr:,.2f} of revocations, INR {self.fatigue_inr:,.2f} of "
            f"fatigue and INR {self.channel_cost_inr:,.2f} of cost"
        )


class Pricer:
    """Prices asks against one configuration. Built once, asked many times."""

    def __init__(self, params: Params, ladder: ChannelLadder | None = None) -> None:
        self.params = params
        self.prices = Prices(
            mu_good_outcome=params.value.mu_good_outcome,
            nu_complaint=params.value.nu_complaint,
        )
        self.ladder = ladder or build_ladder(
            params.channels, params.value.backfire_avoided_per_softer_step
        )

    def effective_hazard(self, entry: MandateWeek, channel: Channel) -> float:
        """`h * (1 - uplift_scale * efficacy)`, floored at zero.

        The floor matters: a configuration sweeping `uplift_scale` above
        `1 / efficacy_prior` would otherwise produce a negative hazard, and the simulation
        would start *creating* mandates. The sweep does reach `uplift_scale = 16`.
        """
        reduction = self.params.intervention.uplift_scale * channel.efficacy_prior
        return entry.hazard * max(0.0, 1.0 - reduction)

    def backfire(self, entry: MandateWeek, channel: Channel) -> float:
        """`b(n)` for the next ask, softened by how gentle the channel is (Chrome)."""
        base = self.params.intervention.backfire(entry.asks_so_far + 1)
        return base * self.ladder.backfire_multiplier(channel)

    def price(
        self, entry: MandateWeek, channel: Channel, template_reused: bool = False
    ) -> AskPrice:
        backfire = self.backfire(entry, channel)
        effective = self.effective_hazard(entry, channel)

        # Both sides are scaled by `alive`: an ask on a mandate that probably already
        # died neither saves nor annoys anybody, in expectation.
        prevented = entry.alive * (1.0 - backfire) * (entry.hazard - effective)
        caused = entry.alive * backfire

        lapse_loss = loss_on_lapse_inr(entry.ltv_remaining_inr, entry.recovery_after_lapse)
        revocation_loss = loss_on_revocation_inr(
            entry.ltv_remaining_inr,
            entry.recovery_after_revocation,
            entry.reachability_value_inr,
            self.params.value.alpha_reachability,
        )
        return AskPrice(
            mandate_id=entry.mandate_id,
            week=entry.week,
            channel=channel.name,
            deaths_prevented=prevented,
            revocations_caused=caused,
            gain_inr=self.prices.good_outcome_inr(prevented * lapse_loss),
            backfire_inr=self.prices.complaint_inr(caused * revocation_loss),
            fatigue_inr=fatigue_inr(
                entry.weeks_since_last_ask,
                self.params.value.gamma_fatigue,
                self.params.value.fatigue_half_life_days,
                template_reused,
                self.params.value.rho_template_reuse,
            ),
            channel_cost_inr=channel.cost_inr,
        )

    def best_channel(self, entry: MandateWeek, budget_inr: float) -> AskPrice | None:
        """The most valuable affordable ask for one mandate, or `None` if none is worth it.

        This is the *per-mandate* half of the multiple-choice knapsack. T3.3 solves the
        whole thing at once under a shared budget; this is what a greedy arm can do
        without an LP, and it is also the tie-break-free way to answer "what would you
        have sent?" in `/explain`.
        """
        affordable = [c for c in self.params.channels if c.cost_inr <= budget_inr]
        priced = [self.price(entry, channel) for channel in affordable]
        best = max(priced, key=lambda p: (p.net_inr, -p.channel_cost_inr, p.channel), default=None)
        return best if best is not None and best.worth_asking else None
