"""Budget-sweep and sensitivity-grid tests (T2.7, T2.8).

Most of this runs on hand-built curves rather than on a simulation, because what is being
tested is the *reading* of a curve -- where its optimum is, which mistake it says is
expensive, what a cell in the plane means -- and a hand-built curve is the only way to
know the answer independently of the code that produced it.

The one thing tested against a real run is the distinction the plane turns on: a cell
where the challenger asked nobody is not the same as a cell where selecting worked, even
though both come out positive.
"""

from __future__ import annotations

import pytest

from mandateguard.allocator.baselines import ChronologicalCap, GreedyEV
from mandateguard.eval import sweep, world
from tests.test_world import BOOK, make_params


def curve(arm: str, profits: dict[float, float]) -> sweep.ArmSweep:
    """An `ArmSweep` with the profits stated directly, so the optimum is known."""
    return sweep.ArmSweep(
        arm=arm,
        points=[
            sweep.SweepPoint(
                budget_inr=budget,
                metrics=world.RunMetrics(
                    arm=arm,
                    weeks=12,
                    mandates=100,
                    mandates_retained=0.0,
                    revocations_caused=0.0,
                    arr_retained_inr=profit,
                    asks_spent=int(budget),
                    net_value_inr=0.0,
                    theta_inr=None,
                    lapses=0.0,
                    revocations_natural=0.0,
                    budget_spent_inr=0.0,
                    channel_cost_inr=0.0,
                ),
            )
            for budget, profit in sorted(profits.items())
        ],
    )


# --------------------------------------------------------------------------------
# The budget ladder.
# --------------------------------------------------------------------------------


def test_the_ladder_runs_from_nothing_to_one_ask_per_mandate():
    """Past saturation the budget cannot buy anything, so a higher rung would be a
    duplicate point wearing a different label."""
    ladder = sweep.budget_ladder(channel_cost_inr=0.05, book_size=1000, steps=10)
    assert ladder[0] == 0.0
    assert ladder[-1] == pytest.approx(50.0)
    assert ladder == sorted(ladder)
    assert len(ladder) == len(set(ladder))


def test_the_ladder_is_geometric_so_the_bottom_is_not_wasted():
    """A linear ladder spends most of its points in the flat region past saturation. The
    interesting behaviour is the difference between 10 asks and 100, not between 9,000
    and 9,100."""
    ladder = sweep.budget_ladder(channel_cost_inr=1.0, book_size=100, steps=8)
    ratios = [b / a for a, b in zip(ladder[1:], ladder[2:], strict=False)]
    assert max(ratios) == pytest.approx(min(ratios), rel=1e-3)


# --------------------------------------------------------------------------------
# Reading a curve.
# --------------------------------------------------------------------------------


def test_a_monotonically_falling_curve_reports_that_doing_nothing_is_optimal():
    """Not a degenerate case to be tuned away. At the shipped parameters it is the
    finding: every rupee of ask budget destroys value."""
    falling = curve("P1", {0.0: 100.0, 10.0: 90.0, 20.0: 70.0})
    assert falling.optimum.budget_inr == 0.0
    assert falling.optimum_is_doing_nothing
    assert falling.gain_over_floor_inr == 0.0
    assert falling.asymmetry is None


def test_an_interior_optimum_is_found_and_measured_against_the_floor():
    hill = curve("P3", {0.0: 100.0, 10.0: 130.0, 20.0: 160.0, 40.0: 140.0, 80.0: 90.0})
    assert hill.optimum.budget_inr == 20.0
    assert hill.gain_over_floor_inr == pytest.approx(60.0)
    assert not hill.optimum_is_doing_nothing


def test_a_flat_top_is_reported_at_the_cheaper_end():
    """Ties break toward the smaller budget, so a plateau is reported where the money
    stops being *needed* rather than where it starts being harmful. Reporting the
    expensive end would overstate what the optimum costs."""
    plateau = curve("P3", {0.0: 100.0, 10.0: 150.0, 20.0: 150.0, 40.0: 120.0})
    assert plateau.optimum.budget_inr == 10.0


def test_the_asymmetry_says_which_mistake_is_expensive_rather_than_assuming():
    """Zhang's shape has under-asking costing about twice what over-asking costs, and the
    curve here is built to match it: at half the optimum budget the gain drops by 32% of
    itself, at double it drops by 16%. The harness must report the pair, not a verdict."""
    zhang = curve("P3", {0.0: 0.0, 10.0: 68.0, 20.0: 100.0, 40.0: 84.0})
    under, over = zhang.asymmetry
    assert under == pytest.approx(0.32)
    assert over == pytest.approx(0.16)
    assert under > over


def test_the_nearest_sampled_budget_is_found_on_a_log_scale():
    """The ladder is geometric, so "closest" has to be measured the same way -- nearest
    in rupees would always pick the larger neighbour."""
    geometric = curve("P3", {0.0: 0.0, 1.0: 1.0, 10.0: 2.0, 100.0: 3.0})
    assert geometric.profit_near(3.0).budget_inr == 1.0
    assert geometric.profit_near(4.0).budget_inr == 10.0


def test_the_sweep_table_names_every_arm():
    text = sweep.format_sweep([curve("P0", {0.0: 1.0}), curve("P3", {0.0: 1.0, 5.0: 9.0})])
    assert "| P0 |" in text
    assert "| P3 |" in text


# --------------------------------------------------------------------------------
# Reading the plane.
# --------------------------------------------------------------------------------


def cell(asks: int, challenger: float, reference: float, floor: float) -> sweep.GridCell:
    return sweep.GridCell(
        uplift_scale=1.0,
        backfire_first_ask=0.006,
        challenger_profit_inr=challenger,
        reference_profit_inr=reference,
        floor_profit_inr=floor,
        challenger_asks=asks,
    )


def test_declining_to_ask_is_not_the_same_as_selecting_well():
    """The distinction the whole plane turns on. `GreedyEV` buys only positive-value
    asks, so where asking never pays it asks nobody -- and then beats rotation by a wide
    margin *for not spending*. That is a real result and a completely different one from
    "selecting the right mandates works"."""
    declined = cell(asks=0, challenger=100.0, reference=40.0, floor=100.0)
    assert declined.advantage_inr == pytest.approx(60.0)
    assert declined.beats_reference
    assert not declined.selection_paid
    assert not declined.beats_floor

    selected = cell(asks=25, challenger=130.0, reference=40.0, floor=100.0)
    assert selected.selection_paid
    assert selected.beats_floor


def test_the_plane_parenthesises_the_cells_where_nobody_was_asked():
    """A reader skimming the table has to be able to see which wins are ours and which
    are the reference's losses, without reading the prose."""
    grid = sweep.Grid(
        challenger="P3",
        reference="P2",
        budget_inr=10.0,
        cells=[
            sweep.GridCell(
                uplift_scale=1.0,
                backfire_first_ask=0.001,
                challenger_profit_inr=130.0,
                reference_profit_inr=40.0,
                floor_profit_inr=100.0,
                challenger_asks=25,
            ),
            sweep.GridCell(
                uplift_scale=1.0,
                backfire_first_ask=0.02,
                challenger_profit_inr=100.0,
                reference_profit_inr=40.0,
                floor_profit_inr=100.0,
                challenger_asks=0,
            ),
        ],
    )
    text = sweep.format_grid(grid)
    assert "+90" in text
    assert "(+60)" in text
    assert grid.share_where_asking_pays == pytest.approx(0.5)


# --------------------------------------------------------------------------------
# Against a real run.
# --------------------------------------------------------------------------------


def test_the_grid_finds_the_frontier_between_asking_and_not_asking():
    """A gentle-backfire, strong-uplift world should buy asks; a harsh-backfire,
    weak-uplift one should not. If the grid cannot separate those two, it cannot separate
    anything, and the region it draws is decoration."""
    params = make_params()
    grid = sweep.sensitivity_grid(
        BOOK, params, uplifts=[0.1, 1.0], backfires=[0.0001, 0.4], budget_inr=1.0
    )
    assert grid.cell(1.0, 0.0001).selection_paid
    assert not grid.cell(0.1, 0.4).selection_paid
    assert 0.0 < grid.share_where_asking_pays < 1.0


def test_the_grid_reproduces_itself():
    """No RNG anywhere (ADR 0003). Two grids over one book must agree exactly."""
    params = make_params()
    first = sweep.sensitivity_grid(BOOK, params, [1.0], [0.001, 0.01], 1.0)
    second = sweep.sensitivity_grid(BOOK, params, [1.0], [0.001, 0.01], 1.0)
    assert first.model_dump() == second.model_dump()


def test_the_grid_moves_backfire_together_at_the_configured_ratio():
    """The plane is over *how irritating an ask is*, not over how fast irritation
    compounds. Varying both independently would be a four-dimensional sweep whose extra
    axes nobody could read off a heatmap."""
    params = make_params(
        intervention={
            "uplift_scale": 1.0,
            "backfire_first_ask": 0.01,
            "backfire_twelfth_ask": 0.1,
            "natural_revocation_share": 0.634,
        }
    )
    # ChronologicalCap is the reference here on purpose: it asks the *same* mandate every
    # week, so the second ask carries `backfire(2)` and the ladder becomes observable.
    # RoundRobin rotates, every ask is a first ask, and the ratio would be invisible.
    grid = sweep.sensitivity_grid(BOOK, params, [1.0], [0.02], 1.0, reference=ChronologicalCap)
    flat = params.model_copy(
        update={
            "intervention": params.intervention.model_copy(
                update={"backfire_first_ask": 0.02, "backfire_twelfth_ask": 0.02}
            )
        }
    )
    flat_run = world.run(BOOK, ChronologicalCap(flat), flat, 1.0)
    assert grid.cells[0].reference_profit_inr != pytest.approx(flat_run.profit_inr)


def test_the_budget_sweep_runs_every_arm_at_every_budget():
    params = make_params()
    sweeps = sweep.budget_sweep(BOOK, params, [0.0, 1.0], arms=[GreedyEV(params)])
    assert len(sweeps) == 1
    assert [p.budget_inr for p in sweeps[0].points] == [0.0, 1.0]
