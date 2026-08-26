"""SPIKE S1 — can we recover the shadow price theta from the LP relaxation?

The whole project's headline number is theta: "the next ask is worth Rs X". Theta is the
dual variable on the budget constraint of the MCKP LP relaxation. If the solver does not
hand back usable duals on this machine, the number does not exist and we fall back to
scipy/HiGHS (see docs/stack.md, spike S1).

This is a toy problem, not the real allocator: 5 mandates, 3 channels, one budget.
"""

import pulp
import pytest

# 5 mandates x 3 channels. value[i][c] = rupee gain, cost[c] = rupee cost of the channel.
CHANNEL_COST = {"sms": 0.15, "whatsapp": 0.35, "call": 40.0}
VALUE = {
    0: {"sms": 120.0, "whatsapp": 180.0, "call": 400.0},
    1: {"sms": 90.0, "whatsapp": 100.0, "call": 130.0},
    2: {"sms": 200.0, "whatsapp": 240.0, "call": 260.0},
    3: {"sms": 30.0, "whatsapp": 45.0, "call": 300.0},
    4: {"sms": 75.0, "whatsapp": 150.0, "call": 155.0},
}
BUDGET = 60.0


def _build(relaxed: bool):
    """Build the MCKP. relaxed=True gives the LP relaxation (duals available)."""
    cat = "Continuous" if relaxed else "Binary"
    prob = pulp.LpProblem("mckp_spike", pulp.LpMaximize)
    x = {
        (i, c): pulp.LpVariable(f"x_{i}_{c}", lowBound=0, upBound=1, cat=cat)
        for i in VALUE
        for c in CHANNEL_COST
    }

    prob += pulp.lpSum(VALUE[i][c] * x[i, c] for i, c in x)

    # At most one channel per mandate — this is what makes it multiple-CHOICE knapsack.
    for i in VALUE:
        prob += pulp.lpSum(x[i, c] for c in CHANNEL_COST) <= 1, f"one_channel_{i}"

    # The budget constraint. Its dual is theta.
    prob += (
        pulp.lpSum(CHANNEL_COST[c] * x[i, c] for i, c in x) <= BUDGET,
        "budget",
    )
    return prob, x


def test_lp_relaxation_yields_a_usable_shadow_price():
    """The core assertion: the budget constraint has a finite, positive dual."""
    prob, _ = _build(relaxed=True)
    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    assert pulp.LpStatus[status] == "Optimal"

    theta = prob.constraints["budget"].pi
    assert theta is not None, "CBC returned no dual — fall back to scipy/HiGHS"
    assert isinstance(theta, float)
    assert theta == pytest.approx(theta)  # not NaN
    assert theta > 0, f"budget should bind and price positively, got {theta}"

    print(f"\n[S1] theta (shadow price of one rupee of budget) = {theta:.4f}")


def test_theta_is_economically_meaningful():
    """theta must behave like a price: relax the budget by 1, objective rises by ~theta.

    A dual that exists but does not predict the objective's response is useless for the
    "the next ask is worth Rs X" claim, so assert the economics, not just the number.
    """
    prob, _ = _build(relaxed=True)
    prob.solve(pulp.PULP_CBC_CMD(msg=False))
    base_obj = pulp.value(prob.objective)
    theta = prob.constraints["budget"].pi

    delta = 1.0
    prob2, x2 = _build(relaxed=True)
    prob2.constraints["budget"].changeRHS(BUDGET + delta)
    prob2.solve(pulp.PULP_CBC_CMD(msg=False))
    bumped_obj = pulp.value(prob2.objective)

    predicted = base_obj + theta * delta
    assert bumped_obj == pytest.approx(predicted, rel=1e-3), (
        f"theta={theta} did not predict the objective response: "
        f"actual={bumped_obj}, predicted={predicted}"
    )

    print(f"\n[S1] +Rs{delta} budget -> +Rs{bumped_obj - base_obj:.4f} (theta={theta:.4f})")


def test_integer_solve_also_works():
    """The shipping policy solves the integer problem; only the relaxation gives duals."""
    prob, x = _build(relaxed=False)
    status = prob.solve(pulp.PULP_CBC_CMD(msg=False))
    assert pulp.LpStatus[status] == "Optimal"

    chosen = {(i, c) for (i, c), var in x.items() if var.value() and var.value() > 0.5}
    for i in VALUE:  # the multiple-choice constraint must hold exactly
        assert sum(1 for (j, _) in chosen if j == i) <= 1

    spend = sum(CHANNEL_COST[c] for _, c in chosen)
    assert spend <= BUDGET + 1e-6

    print(f"\n[S1] integer solution: {sorted(chosen)}, spend=Rs{spend:.2f}")
