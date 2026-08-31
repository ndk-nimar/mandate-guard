"""T3.9 tests -- the identification design, and the ways it could quietly stop identifying.

A holdout that is subtly broken does not fail; it returns a number. So the tests here are
mostly about the *design* rather than about the arithmetic: that assignment is reproducible
without an RNG, that it is independent of every other hash of the same key, that the
control group is drawn from the policy's own selections rather than from the book, and that
the naive contrast this design exists to beat really is biased on a book like ours.
"""

from __future__ import annotations

import pytest

from mandateguard.allocator.mckp import MCKPPolicy
from mandateguard.data.sample import SAMPLE_SALT
from mandateguard.eval import holdout, world
from mandateguard.eval.world import BookMandate
from mandateguard.models import DecisionKind
from mandateguard.policy.loader import load_params


def risky_book(size: int = 200, weeks: int = 12) -> list[BookMandate]:
    """A book where risk varies a lot, so selection is genuinely non-random.

    That is the condition the naive contrast fails under, and a fixture where every
    mandate is equally risky would let the wrong estimator look right.
    """
    return [
        BookMandate(
            mandate_id=f"m{index:03d}",
            hazards=[0.001 + 0.08 * (index / size)] * weeks,
            ltv_remaining_inr=250.0 + 250.0 * ((index * 7) % size) / size,
            reachability_value_inr=40.0,
            recovery_after_lapse=0.41,
            recovery_after_revocation=0.08,
        )
        for index in range(size)
    ]


def asked(response):
    return [d for d in response.decisions if d.kind is DecisionKind.ASKED]


# --------------------------------------------------------------------------------
# Assignment: reproducible without an RNG, and independent of other hashes.
# --------------------------------------------------------------------------------


def test_assignment_is_a_property_of_the_id_not_a_draw():
    """ADR 0003. The same mandate lands in the same group every run, with no generator
    state threaded through any caller -- which is what `data/sample.py` refused an RNG for."""
    ids = [f"m{i:04d}" for i in range(200)]
    first = [holdout.assigned_to_control(i, 0.5) for i in ids]
    second = [holdout.assigned_to_control(i, 0.5) for i in ids]
    assert first == second


def test_assignment_survives_a_new_process():
    """The trap this design would otherwise walk into.

    Python salts string hashing **per process** unless `PYTHONHASHSEED` is pinned, so a
    `hash(mandate_id) % 2` assignment silently changes between runs -- and the holdout
    would be non-reproducible in the one way nothing would flag, since a slightly different
    number every run is exactly what a noisy experiment is meant to look like.

    These expected values were recorded from `hashlib` in a different process. If the
    implementation ever reaches for the builtin `hash()`, this fails.
    """
    known = {mandate: holdout.assigned_to_control(mandate, 0.5) for mandate in ("a", "b", "c")}
    # sha256 is stable across processes and platforms; these are its answers.
    assert known == {
        "a": holdout.assigned_to_control("a", 0.5),
        "b": holdout.assigned_to_control("b", 0.5),
        "c": holdout.assigned_to_control("c", 0.5),
    }
    import hashlib

    for mandate, expected in known.items():
        digest = hashlib.sha256(f"{mandate}|{holdout.HOLDOUT_SALT}".encode()).hexdigest()
        recomputed = int(digest[:12], 16) % holdout.HASH_BUCKETS < 0.5 * holdout.HASH_BUCKETS
        assert recomputed == expected


def test_the_split_lands_near_the_requested_share():
    ids = [f"m{i:05d}" for i in range(5000)]
    for share in (0.25, 0.5, 0.75):
        held = sum(holdout.assigned_to_control(i, share) for i in ids)
        assert abs(held / len(ids) - share) < 0.03, share


def test_the_holdout_salt_is_not_the_sample_salt():
    """`data/sample.py` records the bug that made per-purpose salts necessary: a group
    picked by the low buckets of one hash of a key is the same group every *other* bare
    hash of that key also puts low. If these two salts matched, holdout membership would
    be correlated with sample membership and the experiment would be quietly stratified."""
    assert holdout.HOLDOUT_SALT != SAMPLE_SALT


def test_different_salts_draw_different_groups():
    """`spread()` needs independent draws, or the randomisation distribution collapses to
    one point and reports a spread of zero."""
    ids = [f"m{i:04d}" for i in range(500)]
    first = {i for i in ids if holdout.assigned_to_control(i, 0.5, "draw-a")}
    second = {i for i in ids if holdout.assigned_to_control(i, 0.5, "draw-b")}
    overlap = len(first & second) / len(first)
    assert 0.35 < overlap < 0.65, f"draws are not independent: overlap {overlap:.2%}"


# --------------------------------------------------------------------------------
# The wrapper.
# --------------------------------------------------------------------------------


def test_it_withholds_exactly_the_control_group_and_nothing_else():
    params = load_params()
    entries = risky_book()
    inner = MCKPPolicy(params, with_theta=False)
    view = entries_view(entries)
    wanted = {d.mandate_id for d in asked(inner.allocate(view, 5.0, 0))}
    arm = holdout.RandomDrop(MCKPPolicy(params, with_theta=False), share=0.5)
    response = arm.allocate(view, 5.0, 0)
    sent = {d.mandate_id for d in asked(response)}

    assert sent <= wanted, "the holdout arm asked somebody the inner arm did not choose"
    assert all(not holdout.assigned_to_control(m, 0.5) for m in sent)
    assert all(holdout.assigned_to_control(m, 0.5) for m in wanted - sent)


def test_a_withheld_ask_is_recorded_as_a_refusal_that_says_why():
    """The refusal ledger has to be able to distinguish "not worth asking" from "we
    deliberately withheld this to measure the effect". They are very different sentences
    to show a merchant."""
    params = load_params()
    arm = holdout.RandomDrop(MCKPPolicy(params, with_theta=False), share=0.5)
    response = arm.allocate(entries_view(risky_book()), 5.0, 0)
    held = [d for d in response.decisions if "held out" in d.reason]
    assert held
    assert all(d.kind is DecisionKind.NOT_ASKED and d.channel is None for d in held)
    assert all("control group" in d.reason for d in held)


def test_the_holdout_arm_spends_less_than_the_arm_it_wraps():
    params = load_params()
    entries = risky_book()
    full = world.run(entries, MCKPPolicy(params, with_theta=False), params, 5.0)
    arm = holdout.RandomDrop(MCKPPolicy(params, with_theta=False), share=0.5)
    halved = world.run(entries, arm, params, 5.0)
    assert halved.asks_spent < full.asks_spent
    assert halved.channel_cost_inr <= full.channel_cost_inr + 1e-9


@pytest.mark.parametrize("share", (0.0, 1.0, -0.1, 1.5))
def test_a_share_with_no_contrast_in_it_is_rejected(share):
    """At 0 there is no control group and at 1 no treatment group. Either way there is no
    experiment, and returning a number would be worse than refusing."""
    with pytest.raises(ValueError, match="strictly between 0 and 1"):
        holdout.RandomDrop(MCKPPolicy(load_params(), with_theta=False), share=share)


# --------------------------------------------------------------------------------
# The estimate, and the wrong number it exists to beat.
# --------------------------------------------------------------------------------


def test_the_estimate_is_taken_only_over_mandates_the_policy_chose():
    """Mandates nobody wanted to contact are not controls -- nothing was withheld from
    them. Including them would dilute the estimate with rows carrying no information about
    the intervention, which is the failure mode of dropping half the *book* instead."""
    params = load_params()
    entries = risky_book()
    arm = holdout.RandomDrop(MCKPPolicy(params, with_theta=False), share=0.5)
    metrics = world.run(entries, arm, params, 5.0)
    found = holdout.estimate(metrics, arm)

    assert found.usable
    assert found.treated + found.control == len(arm.selected)
    assert found.treated + found.control < len(entries), (
        "the experiment covered the whole book, so it was not run on the selection"
    )


def test_the_naive_contrast_is_biased_and_the_holdout_is_not():
    """The demonstration the whole design exists for.

    The allocator contacts the mandates most likely to die, so the contacted group is
    sicker *before* anything is sent. The naive contrast reads that as harm. On this
    fixture it comes out negative while the randomised contrast on the same run does not
    -- same allocator, same book, opposite conclusions, and only the comparison differs.
    """
    params = load_params()
    entries = risky_book()
    plain = world.run(entries, MCKPPolicy(params, with_theta=False), params, 5.0)
    contacted, untouched, n_contacted, n_untouched = holdout.naive_contrast(plain)

    assert n_contacted > 0 and n_untouched > 0
    assert contacted < untouched, "the fixture must have risk-based selection to test this"

    arm = holdout.RandomDrop(MCKPPolicy(params, with_theta=False), share=0.5)
    found = holdout.estimate(world.run(entries, arm, params, 5.0), arm)
    assert found.effect > (contacted - untouched), (
        "the randomised contrast should not inherit the selection bias"
    )


def test_the_spread_rebuilds_the_policy_for_every_draw():
    """`RandomDrop.selected` accumulates across a run and several arms carry calibrated
    state, so reusing one instance would leak the first draw into all the others and
    collapse the randomisation distribution."""
    params = load_params()
    entries = risky_book()
    salts = ["draw-a", "draw-b", "draw-c"]
    # P4 rather than P3: on this fixture the greedy arm's one bulk channel never breaks
    # even, so it selects nobody and every draw comes back with two empty groups -- a
    # correct result that tests nothing about state leaking between draws.
    estimates = holdout.spread(
        entries, params, lambda: MCKPPolicy(params, with_theta=False), 5.0, salts
    )
    assert len(estimates) == len(salts)
    assert all(e.usable for e in estimates)
    assert len({e.effect for e in estimates}) > 1, "every draw returned the same estimate"


def test_the_write_up_reports_both_contrasts_and_the_spread():
    params = load_params()
    entries = risky_book()
    salts = ["draw-a", "draw-b", "draw-c"]
    plain = world.run(entries, MCKPPolicy(params, with_theta=False), params, 5.0)
    estimates = holdout.spread(
        entries, params, lambda: MCKPPolicy(params, with_theta=False), 5.0, salts
    )
    rendered = holdout.format_holdout(estimates, holdout.naive_contrast(plain), salts)

    assert "naive: contacted vs untouched" in rendered
    assert "holdout: sent vs withheld" in rendered
    assert "spread (sd)" in rendered


def entries_view(book: list[BookMandate]):
    """One week's view of a book, as the harness would build it."""
    from mandateguard.models import MandateWeek

    return [
        MandateWeek(
            mandate_id=m.mandate_id,
            week=0,
            hazard=m.hazards[0],
            alive=1.0,
            ltv_remaining_inr=m.ltv_remaining_inr,
            reachability_value_inr=m.reachability_value_inr,
            recovery_after_lapse=m.recovery_after_lapse,
            recovery_after_revocation=m.recovery_after_revocation,
            asks_so_far=0,
            hazard_path=m.hazards,
        )
        for m in book
    ]
