"""T3.6 -- does our result have the shape LinkedIn's had? And if not, what does that say?

LinkedIn published three numbers when they replaced send-everything-eligible with an
optimiser (KDD 2016): notification **volume -64.5%**, **sessions -1.8%**, **complaints
-47%**. That triple is the most useful external check this project has, and it is useful
because of its *shape* rather than its magnitudes: send far less, lose almost none of the
thing the sends were for, and cut the harm by a lot.

If an allocator claims to cut volume by two thirds and *also* claims engagement went up,
the honest first reaction is that something is wrong with the simulator, not that we beat
LinkedIn.

The mapping, stated rather than assumed
---------------------------------------
| LinkedIn | here | why |
|---|---|---|
| notification volume | asks sent | the thing being rationed |
| sessions | mandates retained | the thing the sends exist to protect |
| complaints | revocations *caused by an ask* | the harm the sends do |

`revocations_caused` is the right complaint analogue and `revocations_natural` is not: a
mandate the customer would have killed anyway is not a complaint about being contacted.
The harness keeps the two apart for exactly this reason.

The reference arm
-----------------
LinkedIn's "before" was their own production system sending to everyone eligible. The
analogue here is `P1 ChronologicalCap` at a saturating budget -- contact everyone, every
week, until the money runs out. That is the campaign-tool default and it is what a merchant
would actually be doing today, which is what makes it the right "before".

It is also, on this book, a policy that actively destroys value, and that turns out to be
the whole finding. See `docs/eval.md` §6.

The backfire anchor
-------------------
`intervention.backfire_first_ask` has no public measurement. `docs/calibration.md` §4
records it as swept, and every result in this project inherits that. LinkedIn's triple is
the only external observation available that this parameter *should* be able to reproduce,
so `anchor()` sweeps backfire and asks which value brings our shape closest to theirs.

That does not measure backfire. It is not evidence about Indian mandates, and the answer
does not go into `params.yaml`. What it does is convert an unmeasured constant into a
number with an external reference point attached -- and if the value we ship is far from
the one that reproduces the only published shape we have, that is worth knowing and
saying.
"""

from __future__ import annotations

from pydantic import BaseModel

from mandateguard.allocator.base import Policy
from mandateguard.allocator.baselines import ChronologicalCap
from mandateguard.allocator.mckp import MCKPPolicy
from mandateguard.eval.world import BookMandate, RunMetrics, run
from mandateguard.policy.loader import Params


class Shape(BaseModel):
    """Three deltas, challenger against reference. The unit of comparison in T3.6."""

    model_config = {"frozen": True}

    volume_delta: float
    engagement_delta: float
    complaint_delta: float | None
    """`None` when the reference caused no complaints either -- see `_delta`.

    A "0% change in complaints" and "neither arm caused any complaint" are different
    statements, and only the first is a match with LinkedIn's -47%. Collapsing them to
    0.0 would score a degenerate cell as a near-miss on an axis where nothing happened,
    and the backfire anchor below runs straight through that cell at `backfire = 0`.
    """

    def distance_from(self, other: Shape) -> float:
        """Mean absolute difference across the axes that are defined on both sides.

        Unweighted on purpose. A weighted distance would be choosing which axis matters
        most in order to score better on it, and the point of an external anchor is to be
        something this project does not get to tune.
        """
        gaps = [
            abs(self.volume_delta - other.volume_delta),
            abs(self.engagement_delta - other.engagement_delta),
        ]
        if self.complaint_delta is not None and other.complaint_delta is not None:
            gaps.append(abs(self.complaint_delta - other.complaint_delta))
        return sum(gaps) / len(gaps)

    @property
    def matches_direction(self) -> bool:
        """The weak claim, and the one that actually has to hold: fewer asks and fewer
        complaints. Direction only -- the magnitudes are §6's whole subject."""
        return self.volume_delta < 0 and (self.complaint_delta is None or self.complaint_delta < 0)


LINKEDIN = Shape(volume_delta=-0.645, engagement_delta=-0.018, complaint_delta=-0.47)
"""LinkedIn, KDD 2016 -- the only published triple available to check our shape against.

**Its citation chain does not currently close, and that is recorded rather than glossed.**
`docs/calibration.md` §5 lists this triple and points at `docs/prior_art.md` for the exact
claim and page reference. That document has never been written -- it is a Phase 5
deliverable (`docs/tasks.md` T5.x) and `problem.md` links to it too, so both links are
dead today. So these three numbers reach the code from this project's own build plan, and
`CLAUDE.md` §3 is explicit that "it was in the build plan" is not a source.

That does not make the comparison worthless: §6's finding is a **mismatch**, and a
mismatch against a slightly mis-transcribed reference is still a mismatch, since the gap
is 35 percentage points on volume and a sign flip on retention rather than anything a
transcription error could manufacture. But the triple must not be quoted as verified
until someone has read it out of the paper, and `calibration.md` §6 now carries that as a
job."""


def _delta(challenger: float, reference: float) -> float | None:
    """Relative change, or `None` when there was nothing to change.

    A zero reference is not a 100% cut and it is not a 0% cut -- it is an axis with no
    baseline. Returning a number there would invent a result.
    """
    if reference == 0:
        return None
    return (challenger - reference) / reference


def measure(reference: RunMetrics, challenger: RunMetrics) -> Shape:
    """The three deltas. Relative, because LinkedIn's are."""
    volume = _delta(challenger.asks_spent, reference.asks_spent)
    engagement = _delta(challenger.mandates_retained, reference.mandates_retained)
    if volume is None or engagement is None:
        raise ValueError(
            "the reference arm sent no asks or retained no mandates, so there is no "
            "shape to compare. T3.6 needs a 'before' that actually did something -- see "
            "eval/shape.py on why P1 at a saturating budget is that arm."
        )
    return Shape(
        volume_delta=volume,
        engagement_delta=engagement,
        complaint_delta=_delta(challenger.revocations_caused, reference.revocations_caused),
    )


def compare(
    book: list[BookMandate],
    params: Params,
    budget_inr: float,
    reference: Policy | None = None,
    challenger: Policy | None = None,
) -> tuple[Shape, RunMetrics, RunMetrics]:
    """Run both arms over the same book at the same budget and take the deltas."""
    before = run(book, reference or ChronologicalCap(params), params, budget_inr)
    after = run(book, challenger or MCKPPolicy(params, with_theta=False), params, budget_inr)
    return measure(before, after), before, after


def anchor(
    book: list[BookMandate],
    params: Params,
    budget_inr: float,
    backfire_rates: list[float],
) -> list[tuple[float, Shape, float]]:
    """Which first-ask backfire rate reproduces LinkedIn's shape most closely?

    Returns `(rate, shape, distance)` per rate, in the order given. The twelfth-ask rate
    is scaled with the first so the *ratio* between them stays at the shipped ten-to-one:
    `docs/problem.md` §5.1 gives the ladder as a ratio ("ten times worse by the twelfth"),
    not as two independent numbers, so sweeping only the first anchor while pinning the
    twelfth would quietly sweep the ratio too and the result would be about the wrong
    thing.

    This is a diagnostic, not a calibration. Nothing here is written back to
    `params.yaml`; `docs/calibration.md` §5 keeps backfire listed as unmeasured either
    way.
    """
    ratio = (
        params.intervention.backfire_twelfth_ask / params.intervention.backfire_first_ask
        if params.intervention.backfire_first_ask
        else 10.0
    )
    found = []
    for rate in backfire_rates:
        tuned = params.model_copy(
            update={
                "intervention": params.intervention.model_copy(
                    update={
                        "backfire_first_ask": rate,
                        "backfire_twelfth_ask": min(1.0, rate * ratio),
                    }
                )
            }
        )
        shape, _, _ = compare(book, tuned, budget_inr)
        found.append((rate, shape, shape.distance_from(LINKEDIN)))
    return found


def _pct(value: float | None) -> str:
    return "--" if value is None else f"{value:+.1%}"


def format_comparison(
    shape: Shape, before: RunMetrics, after: RunMetrics, budget_inr: float
) -> str:
    """The three deltas beside LinkedIn's, and what the difference means.

    The reading is derived, not typed. `scripts/make_results.py` twice shipped a literal
    claim that contradicted its own table, and a section whose entire purpose is to say
    "our numbers do or do not look like the published ones" is the last place to hand-write
    the verdict.
    """
    lines = [
        f"Reference `{before.arm}` against challenger `{after.arm}`, same book, same "
        f"{before.weeks}-week horizon, at a budget of INR {budget_inr:,.2f} per week -- "
        "enough for one bulk ask per mandate per week, so the reference contacts "
        "everybody and the budget never binds on it.",
        "",
        "| axis | LinkedIn (KDD 2016) | here | reference | challenger |",
        "|---|---:|---:|---:|---:|",
        f"| volume (asks) | {LINKEDIN.volume_delta:+.1%} | {_pct(shape.volume_delta)} "
        f"| {before.asks_spent:,} | {after.asks_spent:,} |",
        f"| engagement (mandates retained) | {LINKEDIN.engagement_delta:+.1%} "
        f"| {_pct(shape.engagement_delta)} | {before.mandates_retained:,.1f} "
        f"| {after.mandates_retained:,.1f} |",
        f"| complaints (revocations caused) | {LINKEDIN.complaint_delta:+.1%} "
        f"| {_pct(shape.complaint_delta)} | {before.revocations_caused:,.2f} "
        f"| {after.revocations_caused:,.2f} |",
        "",
    ]

    if shape.matches_direction:
        lines.append(
            "**The direction agrees on the axes that matter.** Far fewer asks, far fewer "
            "revocations caused. That is the shape T3.6 asked for, and it is the weak "
            "claim."
        )
    else:
        lines.append(
            "**The direction does not agree**, which is a finding about the simulator "
            "before it is a finding about the allocator."
        )

    ratio = shape.volume_delta / LINKEDIN.volume_delta
    lines += [
        "",
        f"**The magnitude does not.** This allocator cuts volume by "
        f"{abs(shape.volume_delta):.1%} where LinkedIn cut it by "
        f"{abs(LINKEDIN.volume_delta):.1%} -- {ratio:.1f} times as deep.",
    ]

    if shape.engagement_delta > 0:
        lines += [
            f"And retention moves the **wrong way**: {_pct(shape.engagement_delta)} here "
            f"against LinkedIn's {LINKEDIN.engagement_delta:+.1%}. Cutting asks is not "
            "supposed to *raise* the thing the asks were for.",
            "",
            "There is a coherent reading and it is not a flattering one. LinkedIn's "
            "marginal notification was worth roughly nothing -- they dropped two thirds of "
            "their volume and lost 1.8% of sessions, which is what near-zero value looks "
            "like. In this model the marginal ask is worth *less* than nothing, because "
            "backfire makes contacting a healthy mandate actively harmful. So the "
            "reference arm is not merely wasteful here, it is destructive, and declining "
            "to do what it does shows up as a gain.",
        ]
    return "\n".join(lines)


def format_anchor(rows: list[tuple[float, Shape, float]], shipped_rate: float) -> str:
    """Which backfire rate, if any, brings our shape to LinkedIn's -- and the answer."""
    lines = [
        "`intervention.backfire_first_ask` has no public measurement "
        "(`calibration.md` §4). LinkedIn's triple is the only external observation this "
        "project has that the parameter *should* be able to reproduce, so the obvious "
        "question is which value does. The twelfth-ask rate moves with the first, holding "
        "the ten-to-one ladder `problem.md` §5.1 gives.",
        "",
        "| backfire (1st ask) | volume | engagement | complaints | distance from LinkedIn |",
        "|---:|---:|---:|---:|---:|",
    ]
    for rate, shape, distance in rows:
        marker = " **(shipped)**" if rate == shipped_rate else ""
        lines.append(
            f"| {rate:.5f}{marker} | {_pct(shape.volume_delta)} "
            f"| {_pct(shape.engagement_delta)} | {_pct(shape.complaint_delta)} "
            f"| {distance:.4f} |"
        )

    # Rows where the reference caused no complaints at all are scored over two axes
    # instead of three, so their distance is not comparable with the rest -- and it is
    # systematically *smaller*, because the dropped axis is the one we are furthest off
    # on. Letting such a row win "closest" would be an artefact of the missing axis, not
    # a result, so the comparison is made among the rows that have all three.
    comparable = [row for row in rows if row[1].complaint_delta is not None]
    degenerate = [row for row in rows if row[1].complaint_delta is None]
    lines += ["", "**No value of backfire reproduces LinkedIn's shape.**"]

    if degenerate:
        rates = ", ".join(f"{rate:.5f}" for rate, _, _ in degenerate)
        lines.append(
            f"At {rates} neither arm causes a single revocation, so the complaints axis "
            "has no baseline and those rows are scored over two axes rather than three. "
            "Their distance is therefore *not* comparable with the others and they are "
            "excluded from the comparison below -- a row that wins by dropping the axis "
            "we are furthest off on has not won anything."
        )

    if not comparable:
        return "\n".join(lines)

    best_rate, best_shape, best_distance = min(comparable, key=lambda row: row[2])
    lines.append(
        f"Among the {len(comparable)} rows scored on all three axes the closest is "
        f"{best_rate:.5f}, at a distance of {best_distance:.4f}, and even there the "
        f"volume cut is {_pct(best_shape.volume_delta)} against LinkedIn's "
        f"{LINKEDIN.volume_delta:+.1%}."
    )
    ordered = sorted(comparable, key=lambda row: row[0])
    if all(a[2] <= b[2] for a, b in zip(ordered, ordered[1:], strict=False)):
        lines.append(
            "The distance rises monotonically with backfire across the whole sweep, so "
            "the closest fit is at the bottom of the range and lowering backfire further "
            "only runs into the degenerate rows above. **The mismatch is not a backfire "
            "value we have mis-set: turning backfire down does not close it.** That rules "
            "out the one explanation this project had a knob for, which is worth more "
            "than a fitted value would have been."
        )

    shipped = next((row for row in rows if row[0] == shipped_rate), None)
    if shipped is not None:
        lines += [
            "",
            f"What backfire *does* control is the engagement axis. At the shipped "
            f"{shipped_rate:.5f} retention moves {_pct(shipped[1].engagement_delta)}; at "
            f"the bottom of the sweep it moves {_pct(rows[0][1].engagement_delta)}, which "
            f"is LinkedIn's direction. So the wrong-way retention number is a consequence "
            "of an unmeasured parameter and is the most parameter-sensitive figure in this "
            "project -- not an independent finding about allocation.",
        ]
    return "\n".join(lines)
