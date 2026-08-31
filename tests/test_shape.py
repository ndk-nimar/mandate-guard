"""T3.6 tests -- the external sanity check, and the honesty of how it is scored.

The section this covers reports a **mismatch**: our shape does not reproduce LinkedIn's.
That makes the arithmetic behind the comparison more load-bearing than usual, not less. A
mismatch reported by a scorer that quietly drops an axis, or that turns "neither arm did
this" into "no change", is not a finding -- it is a bug wearing a finding's clothes.
"""

from __future__ import annotations

import pytest

from mandateguard.eval import shape
from mandateguard.eval.world import BookMandate, RunMetrics
from mandateguard.policy.loader import load_params
from tests.test_world import BOOK, make_params


def metrics(arm: str, asks: int, retained: float, caused: float) -> RunMetrics:
    """A `RunMetrics` with only the three axes T3.6 reads set to anything meaningful."""
    return RunMetrics(
        arm=arm,
        weeks=12,
        mandates=100,
        mandates_retained=retained,
        revocations_caused=caused,
        arr_retained_inr=0.0,
        asks_spent=asks,
        net_value_inr=0.0,
        theta_inr=None,
        lapses=0.0,
        revocations_natural=0.0,
        budget_spent_inr=0.0,
        channel_cost_inr=0.0,
    )


def spread_book(size: int = 120, weeks: int = 12) -> list[BookMandate]:
    """A book whose mandates differ in risk and in worth, over the shipped horizon.

    `tests/test_world.BOOK` has three mandates and a two-week horizon, which is right for
    checking survival arithmetic exactly and wrong here: with a INR 1 budget both arms
    contact everyone they can and the volume delta is zero, so the direction T3.6 asks
    about is not expressible at all. The shape only exists once some mandates are worth
    contacting and most are not, which is the situation the real book is in.
    """
    return [
        BookMandate(
            mandate_id=f"m{index:03d}",
            hazards=[0.001 + 0.4 * (index / size) for _ in range(weeks)],
            ltv_remaining_inr=100.0 + 400.0 * ((index * 7) % size) / size,
            reachability_value_inr=15.0,
            recovery_after_lapse=0.41,
            recovery_after_revocation=0.08,
        )
        for index in range(size)
    ]


# --------------------------------------------------------------------------------
# The published triple, and the arithmetic of comparing against it.
# --------------------------------------------------------------------------------


def test_the_linkedin_triple_is_what_the_paper_published():
    """Sourced, not chosen. If this drifts, every claim in §6 drifts with it and there is
    nothing in the output that would show it."""
    assert shape.LINKEDIN.volume_delta == -0.645
    assert shape.LINKEDIN.engagement_delta == -0.018
    assert shape.LINKEDIN.complaint_delta == -0.47


def test_the_three_deltas_are_relative_changes_against_the_reference():
    before = metrics("P1", asks=1000, retained=900.0, caused=100.0)
    after = metrics("P4", asks=250, retained=855.0, caused=40.0)
    measured = shape.measure(before, after)

    assert measured.volume_delta == pytest.approx(-0.75)
    assert measured.engagement_delta == pytest.approx(-0.05)
    assert measured.complaint_delta == pytest.approx(-0.60)


def test_an_axis_with_no_baseline_is_undefined_rather_than_zero():
    """ "Complaints did not change" and "neither arm caused a complaint" are different
    statements, and only the first is a match with LinkedIn's -47%.

    This is not hypothetical: the backfire anchor sweep runs straight through this cell
    at `backfire = 0`, where no ask can cause a revocation at all. Reporting 0.0 there
    would score a cell in which nothing happened as a near-miss.
    """
    before = metrics("P1", asks=1000, retained=900.0, caused=0.0)
    after = metrics("P4", asks=250, retained=880.0, caused=0.0)
    assert shape.measure(before, after).complaint_delta is None


def test_an_undefined_axis_is_skipped_rather_than_counted_as_agreement():
    """A two-axis distance and a three-axis distance are not comparable, and the missing
    axis is systematically the one we are furthest off on -- so treating an undefined
    delta as a perfect match would make a degenerate row win."""
    full = shape.Shape(volume_delta=-0.9, engagement_delta=0.0, complaint_delta=-0.9)
    partial = shape.Shape(volume_delta=-0.9, engagement_delta=0.0, complaint_delta=None)

    assert partial.distance_from(shape.LINKEDIN) == pytest.approx(
        (abs(-0.9 + 0.645) + abs(0.0 + 0.018)) / 2
    )
    assert full.distance_from(shape.LINKEDIN) > partial.distance_from(shape.LINKEDIN)


def test_a_reference_arm_that_did_nothing_is_an_error_not_a_shape():
    """T3.6 needs a "before" that actually sent something. A reference which asked nobody
    would make the volume delta undefined and the whole comparison vacuous, and silently
    returning something would hide that."""
    before = metrics("P1", asks=0, retained=900.0, caused=0.0)
    after = metrics("P4", asks=10, retained=905.0, caused=1.0)
    with pytest.raises(ValueError, match="no shape to compare"):
        shape.measure(before, after)


def test_direction_is_the_weak_claim_and_is_checked_separately_from_magnitude():
    """§6's whole subject is that the direction agrees and the magnitude does not, so the
    two have to be separable rather than one verdict."""
    agrees = shape.Shape(volume_delta=-0.99, engagement_delta=0.07, complaint_delta=-0.99)
    assert agrees.matches_direction
    assert agrees.distance_from(shape.LINKEDIN) > 0.2

    disagrees = shape.Shape(volume_delta=0.2, engagement_delta=0.0, complaint_delta=-0.5)
    assert not disagrees.matches_direction


# --------------------------------------------------------------------------------
# The comparison on a real (if tiny) world.
# --------------------------------------------------------------------------------


def test_the_challenger_asks_less_and_harms_less_than_the_reference():
    """The direction T3.6 asks for, on the harness's own three-mandate world.

    Deliberately not asserted on the magnitudes. Those are §6's finding and they are a
    property of the book and of two swept parameters, so pinning them here would turn a
    result into a regression test and make the finding unable to change when the inputs do.
    """
    params = load_params()
    book = spread_book()
    # Saturating: one bulk ask per mandate per week, so the reference contacts everybody
    # and the budget never binds on it -- the same reference point §6 uses.
    budget = len(book) * min(c.cost_inr for c in params.channels if c.intrusive)
    measured, before, after = shape.compare(book, params, budget)

    assert before.asks_spent > after.asks_spent
    assert measured.matches_direction


def test_the_anchor_sweep_holds_the_backfire_ladder_rather_than_only_its_first_rung():
    """`problem.md` §5.1 gives backfire as a *ratio* -- ten times worse by the twelfth ask
    -- not as two independent numbers. Sweeping the first anchor while pinning the twelfth
    would sweep the ratio too, and the result would be about the wrong thing."""
    params = make_params(
        intervention={
            "uplift_scale": 1.0,
            "backfire_first_ask": 0.01,
            "backfire_twelfth_ask": 0.10,
            "natural_revocation_share": 0.634,
        }
    )
    rows = shape.anchor(BOOK, params, 1.0, [0.02])
    assert len(rows) == 1
    # The sweep rebuilds params internally; check the ladder it would have built.
    assert params.intervention.backfire_twelfth_ask / params.intervention.backfire_first_ask == 10.0


def test_the_anchor_returns_a_row_per_rate_in_the_order_given():
    """The table is printed straight from this, and a sweep that reordered itself would
    put the shipped-value marker on the wrong row (ADR 0003)."""
    rates = [0.0, 0.001, 0.01]
    rows = shape.anchor(BOOK, make_params(), 1.0, rates)
    assert [rate for rate, _, _ in rows] == rates
    assert all(distance >= 0 for _, _, distance in rows)


# --------------------------------------------------------------------------------
# The rendering, which is where a claim could drift from its own table.
# --------------------------------------------------------------------------------


def test_the_comparison_reads_its_verdict_off_the_numbers():
    """`make_results.py` twice shipped a literal claim that contradicted its own table.
    A section whose entire purpose is "do our numbers look like the published ones" is
    the last place to hand-write the answer."""
    params = load_params()
    book = spread_book()
    budget = len(book) * min(c.cost_inr for c in params.channels if c.intrusive)
    measured, before, after = shape.compare(book, params, budget)
    rendered = shape.format_comparison(measured, before, after, budget)

    assert "LinkedIn (KDD 2016)" in rendered
    assert f"{shape.LINKEDIN.volume_delta:+.1%}" in rendered
    if measured.matches_direction:
        assert "direction agrees" in rendered
    else:
        assert "direction does not agree" in rendered


def test_the_anchor_rendering_marks_the_shipped_rate_and_flags_degenerate_rows():
    params = make_params()
    rates = [0.0, 0.001, params.intervention.backfire_first_ask]
    rendered = shape.format_anchor(shape.anchor(BOOK, params, 1.0, rates), rates[-1])

    assert "(shipped)" in rendered
    assert "distance from LinkedIn" in rendered


def test_an_undefined_complaint_axis_renders_as_a_dash_not_a_number():
    """The one rendering bug that would turn a degenerate cell into a claim."""
    partial = shape.Shape(volume_delta=-0.9, engagement_delta=0.0, complaint_delta=None)
    rendered = shape.format_anchor([(0.0, partial, 0.1)], 0.006)
    assert "| -- |" in rendered
