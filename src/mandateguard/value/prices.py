"""Two prices, never one netted number. **LinkedIn, KDD 2016.**

LinkedIn's notification optimiser priced a good outcome (`mu`) and a complaint (`nu`)
*separately*, and their published result is what a system looks like when it does:
notification volume down 64.5%, sessions down only 1.8%, complaints down 47%.

The reason the two cannot be one number is that they are paid by different parties over
different horizons. A retained mandate is revenue this quarter. A complaint is a customer
who will be harder to reach for years, plus a support ticket, plus a rail that may be gone.
Netting them into a single "value per send" throws away the ratio, and the ratio is the
only thing that tells an optimiser when to stop.

It is also the failure this project's ladder demonstrates. `P1` and `P2` in
`docs/results.md` §2 behave exactly like a system with one netted price: they spend a
budget that never binds and destroy ARR, because nothing in them is priced to notice the
complaint side.

Both are 1.0 as shipped and both are **swept** (`docs/calibration.md` §4). LinkedIn's paper
establishes that the two must be separate; it does not tell anyone what either is worth in
rupees for an Indian mandate book, and pretending otherwise would be inventing a number
with a citation attached to it.
"""

from __future__ import annotations

from pydantic import BaseModel


class Prices(BaseModel):
    """The two sides of the objective, kept apart.

    `mu` scales the value of an outcome the customer is glad about; `nu` scales the cost
    of one they are not. A configuration with `mu == nu` is not "neutral" -- it is the
    specific claim that a complaint costs exactly what a save is worth, which is a strong
    claim nobody has evidence for. It is the shipped default because 1.0 and 1.0 is the
    least-assuming pair, and because the sweep varies them.
    """

    model_config = {"frozen": True}

    mu_good_outcome: float
    nu_complaint: float

    def good_outcome_inr(self, amount_inr: float) -> float:
        return self.mu_good_outcome * amount_inr

    def complaint_inr(self, amount_inr: float) -> float:
        return self.nu_complaint * amount_inr

    @property
    def complaint_is_priced_at_least_as_dearly(self) -> bool:
        """Whether a complaint costs at least as much as a save is worth.

        Not enforced -- `nu < mu` is a legitimate world and the sweep visits it. Exposed
        so that a report can *say* which world it is describing, because "we reduced
        complaints by 47%" reads very differently depending on the answer.
        """
        return self.nu_complaint >= self.mu_good_outcome
