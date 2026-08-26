"""The Policy interface -- one shape, six implementations (P0..P5).

Every arm in the evaluation ladder implements this. Keeping them behind one interface is
what makes the six-arm comparison in docs/eval.md an apples-to-apples result rather than
six differently-shaped scripts.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from mandateguard.models import AllocationResponse, Decision, DecisionKind, Mandate


class Policy(ABC):
    """Decides which mandates to ask, through which channel, in a given week."""

    #: Arm label used in results.md and the six-arm chart, e.g. "P0", "P4".
    arm: str = "P?"

    @abstractmethod
    def allocate(self, mandates: list[Mandate], budget_inr: float, week: int) -> AllocationResponse:
        """Return a decision for *every* mandate -- asked and not-asked alike.

        Returning only the asked ones would make the refusal ledger impossible, so the
        contract is total: len(response.decisions) == len(mandates).
        """


class NoAskPolicy(Policy):
    """P0 -- the floor. Contacts nobody.

    Without this arm, "we retained X mandates" has no denominator: every other arm is
    measured as an improvement over doing nothing at all.
    """

    arm = "P0"

    def allocate(self, mandates: list[Mandate], budget_inr: float, week: int) -> AllocationResponse:
        return AllocationResponse(
            decisions=[
                Decision(
                    mandate_id=m.mandate_id,
                    week=week,
                    kind=DecisionKind.NOT_ASKED,
                    value_inr=0.0,
                    reason="P0 floor policy: never contacts anyone.",
                )
                for m in mandates
            ],
            theta_inr=None,
            budget_spent_inr=0.0,
        )
