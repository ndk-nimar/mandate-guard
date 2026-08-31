"""T3.7 tests -- the segment profile, and the shape verdict read off it.

Like §6, this section reports a **negative**: our allocator does not reproduce Pinterest's
inverted U, it produces a threshold. So the classifier that decides which of those two
shapes a curve is has to be right about both, and it has to be hard to fool -- a verdict
of "threshold" is only worth reporting if "inverted U" was reachable.
"""

from __future__ import annotations

from mandateguard.allocator.mckp import MCKPPolicy
from mandateguard.eval import segments, world
from mandateguard.eval.world import BookMandate, RunMetrics
from mandateguard.policy.loader import load_params


def curve(rates: list[float], per_bucket: int = 100) -> segments.SegmentProfile:
    """A profile with a given asks-per-mandate shape, for testing the classifier alone."""
    return segments.SegmentProfile(
        arm="test",
        segments=[
            segments.Segment(
                index=index,
                hazard_low=0.001 * (index + 1),
                hazard_high=0.001 * (index + 2),
                mandates=per_bucket,
                asks=round(rate * per_bucket),
            )
            for index, rate in enumerate(rates)
        ],
    )


def spread_book(size: int = 200, weeks: int = 12) -> list[BookMandate]:
    """A book spanning low to high risk, so both shapes are expressible."""
    return [
        BookMandate(
            mandate_id=f"m{index:03d}",
            hazards=[0.001 + 0.09 * (index / size)] * weeks,
            ltv_remaining_inr=300.0,
            reachability_value_inr=45.0,
            recovery_after_lapse=0.41,
            recovery_after_revocation=0.08,
        )
        for index in range(size)
    ]


# --------------------------------------------------------------------------------
# The classifier, which decides what the section says.
# --------------------------------------------------------------------------------


def test_a_rising_curve_is_a_threshold_and_not_an_inverted_u():
    rising = curve([0.0, 0.0, 0.0, 0.1, 0.3, 0.8])
    assert rising.is_threshold
    assert not rising.is_inverted_u
    assert rising.peak.index == 5


def test_a_curve_quiet_at_both_ends_is_an_inverted_u():
    """The shape Pinterest reported: the healthiest and the most doomed get the fewest."""
    hump = curve([0.05, 0.4, 0.9, 0.35, 0.05])
    assert hump.is_inverted_u
    assert not hump.is_threshold
    assert hump.peak.index == 2


def test_an_interior_peak_alone_is_not_enough_to_be_called_an_inverted_u():
    """The test that keeps this classifier from flattering the result.

    A curve that climbs steadily and then dips once at the end has an interior peak, and
    a naive check would call that Pinterest's shape. It is not one: the low-risk end is
    still busy. Both tails have to fall below half the peak first.
    """
    wobble = curve([0.6, 0.7, 0.8, 0.9, 0.85])
    assert wobble.peak.index == 3
    assert not wobble.is_inverted_u


def test_a_flat_curve_counts_as_a_threshold_rather_than_a_hump():
    """Non-decreasing, not strictly increasing -- a policy that treats every segment the
    same has not discovered an inverted U."""
    flat = curve([0.3, 0.3, 0.3, 0.3])
    assert flat.is_threshold
    assert not flat.is_inverted_u


def test_the_peak_is_total_so_the_verdict_cannot_move_between_runs():
    """ADR 0003: this figure is committed. Two buckets with identical rates must not
    swap which one is called the peak."""
    tied = curve([0.1, 0.5, 0.5, 0.1])
    assert tied.peak.index == 1
    assert curve([0.1, 0.5, 0.5, 0.1]).peak.index == tied.peak.index


# --------------------------------------------------------------------------------
# The bucketing.
# --------------------------------------------------------------------------------


def test_buckets_hold_equal_counts_rather_than_equal_widths():
    """The hazard distribution is extremely skewed, so equal-width bins would put almost
    every mandate in the first one and the curve would have no shape to read."""
    params = load_params()
    book = spread_book()
    metrics = world.run(book, MCKPPolicy(params, with_theta=False), params, 50.0)
    found = segments.profile(book, metrics, buckets=10)

    assert len(found.segments) == 10
    sizes = {s.mandates for s in found.segments}
    assert max(sizes) - min(sizes) <= 1
    assert sum(s.mandates for s in found.segments) == len(book)


def test_every_ask_the_run_made_lands_in_exactly_one_bucket():
    """A profile that dropped or double-counted asks would still draw a plausible curve,
    which is precisely why the total is checked against the run rather than eyeballed."""
    params = load_params()
    book = spread_book()
    metrics = world.run(book, MCKPPolicy(params, with_theta=False), params, 50.0)
    found = segments.profile(book, metrics)

    assert sum(s.asks for s in found.segments) == metrics.asks_spent
    assert metrics.asks_spent > 0, "a run with no asks cannot test the accounting"


def test_buckets_are_ordered_by_risk_and_reproduce_between_runs():
    params = load_params()
    book = spread_book()
    metrics = world.run(book, MCKPPolicy(params, with_theta=False), params, 50.0)
    first = segments.profile(book, metrics)
    second = segments.profile(book, metrics)

    assert first.model_dump() == second.model_dump()
    lows = [s.hazard_low for s in first.segments]
    assert lows == sorted(lows)


def test_an_empty_book_is_not_an_error():
    metrics = RunMetrics(
        arm="P4",
        weeks=12,
        mandates=0,
        mandates_retained=0.0,
        revocations_caused=0.0,
        arr_retained_inr=0.0,
        asks_spent=0,
        net_value_inr=0.0,
        theta_inr=None,
        lapses=0.0,
        revocations_natural=0.0,
        budget_spent_inr=0.0,
        channel_cost_inr=0.0,
    )
    assert segments.profile([], metrics).segments == []
    assert "no mandates" in segments.format_profile(segments.profile([], metrics))


# --------------------------------------------------------------------------------
# The prediction the module makes before it is run.
# --------------------------------------------------------------------------------


def test_the_allocator_produces_a_threshold_because_value_is_linear_in_hazard():
    """T3.7's actual answer, and it is settled by the value function rather than by luck.

    The gain from an ask is `alive * (1 - b) * h * uplift * efficacy * L_lapse` -- linear
    in `h` -- while the backfire cost does not involve `h` at all. So within a week the
    value of asking only ever rises with risk, and there is no hazard so high that an ask
    stops being worth making. Pinterest's right-hand tail needs a falling *response
    probability* at the dormant end, and `efficacy_prior` here belongs to the channel and
    is identical for every mandate.

    If this test ever fails, the model gained a per-mandate responsiveness term and §7
    needs rewriting rather than the test needing relaxing.
    """
    params = load_params()
    book = spread_book()
    metrics = world.run(book, MCKPPolicy(params, with_theta=False), params, 50.0)
    found = segments.profile(book, metrics)

    assert found.is_threshold
    assert not found.is_inverted_u
    assert found.peak.index == len(found.segments) - 1


def test_the_write_up_names_the_shape_it_measured():
    """`make_results.py` twice shipped prose that contradicted its own table. A section
    whose whole job is "which of two shapes is this" cannot hand-write the answer."""
    threshold = segments.format_profile(curve([0.0, 0.0, 0.2, 0.9]))
    assert "**threshold**" in threshold
    assert "efficacy_prior" in threshold

    hump = segments.format_profile(curve([0.05, 0.4, 0.9, 0.35, 0.05]))
    assert "inverted U" in hump
    assert "survival attrition" in hump, "an inverted U here is not Pinterest's mechanism"


def test_the_plot_is_written_whatever_the_shape_turns_out_to_be(tmp_path):
    """T3.7 ships the figure either way. A plot drawn only when it agrees with the paper
    it is being checked against is not evidence."""
    path = segments.plot(curve([0.0, 0.0, 0.2, 0.9]), tmp_path / "segments.png")
    assert path.exists() and path.stat().st_size > 0
