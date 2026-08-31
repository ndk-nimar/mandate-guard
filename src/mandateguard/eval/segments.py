"""T3.7 -- Pinterest's testable prediction, and whether this model can even make it.

Pinterest (KDD 2018) reported an **inverted U**: their optimiser sent the fewest
notifications to the *most active* users and to the *most dormant* ones, and concentrated
on the middle. Both ends for different reasons -- the most active do not need a nudge, and
the most dormant will not answer one.

The analogue here is the hazard axis. The healthiest mandates are nowhere near their
coverage end and need nothing; the most doomed are going regardless. If our allocator
independently produces the same curve, that is external validation of a kind nothing else
in this project offers.

Read the value function before running it
-----------------------------------------
It is worth being clear about what this model *can* produce, because the answer bounds the
result before any data is involved. One ask on a mandate with hazard `h`:

```
prevented = alive * (1 - b) * (h - h_eff)        where h_eff = h * (1 - uplift * efficacy)
          = alive * (1 - b) * h * uplift * efficacy
value     = prevented * L_lapse  -  alive * b * L_revocation  -  fatigue  -  k[c]
```

The gain is **linear in `h`**. The backfire cost does not involve `h` at all. So within a
single week the value of asking is *monotonically increasing* in hazard, and the
week-by-week decision rule is a **threshold**, not an inverted U: ask everybody above a
cut-off. There is no `h` so large that an ask becomes worthless again.

That is a structural statement, and it means **the right half of Pinterest's curve cannot
come from the pricing.** Pinterest had a mechanism this model does not: their most dormant
users had a lower *response probability*, so the uplift itself decayed at the far end. Here
`efficacy_prior` is a property of the **channel** and is identical for every mandate. A
mandate three days from expiry and one that will certainly lapse are assumed equally
persuadable.

So if an inverted U appears at all it has to come from somewhere else -- and there is
exactly one candidate: `alive`. Over a twelve-week horizon the doomed die, `alive` falls
toward zero, and both the gain and the backfire shrink with it while `fatigue` and `k[c]`
do not. A mandate that is already gone is not worth paying for. That is a **cumulative**
effect over the horizon rather than an instantaneous one, which is why this module counts
asks per mandate across the whole run rather than reading one week's decisions.

Whether that is enough to bend the curve down is a measurement, and `docs/eval.md` §7 is
the answer. What is settled in advance is the interpretation: an inverted U here would be
survival attrition, **not** Pinterest's mechanism, and reporting it as agreement would be
claiming a match between two different phenomena that happen to have the same shape.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from mandateguard.data.paths import ensure
from mandateguard.eval.world import BookMandate, RunMetrics


class Segment(BaseModel):
    """One hazard bucket: how risky, how many mandates, how many asks they got."""

    model_config = {"frozen": True}

    index: int
    hazard_low: float
    hazard_high: float
    mandates: int
    asks: int

    @property
    def asks_per_mandate(self) -> float:
        return self.asks / self.mandates if self.mandates else 0.0

    @property
    def hazard_mid(self) -> float:
        return 0.5 * (self.hazard_low + self.hazard_high)


class SegmentProfile(BaseModel):
    """The whole curve, plus the verdict on its shape."""

    model_config = {"frozen": True}

    arm: str
    segments: list[Segment]

    @property
    def peak(self) -> Segment:
        """The busiest bucket. Ties break to the lower index so the answer is total."""
        return max(self.segments, key=lambda s: (s.asks_per_mandate, -s.index))

    @property
    def is_inverted_u(self) -> bool:
        """True when the peak is interior *and* both ends are genuinely quieter.

        "Interior peak" alone is too weak a test -- with twelve buckets and a lumpy book,
        a curve that rises monotonically except for one noisy final bucket would pass it.
        Both tails have to be below half the peak before this is a U rather than a wobble.
        """
        peak = self.peak
        if peak.index in (0, len(self.segments) - 1):
            return False
        first, last = self.segments[0], self.segments[-1]
        return (
            first.asks_per_mandate < peak.asks_per_mandate / 2
            and last.asks_per_mandate < peak.asks_per_mandate / 2
        )

    @property
    def is_threshold(self) -> bool:
        """True when asks only ever rise with hazard -- the shape the pricing predicts."""
        rates = [s.asks_per_mandate for s in self.segments]
        return all(a <= b + 1e-9 for a, b in zip(rates, rates[1:], strict=False))


def profile(book: list[BookMandate], metrics: RunMetrics, buckets: int = 10) -> SegmentProfile:
    """Bucket the book by mean projected hazard and count asks per bucket.

    **Equal-count buckets, not equal-width.** The hazard distribution is extremely
    skewed -- `eval.md` §1 puts the median at about 0.0016 -- so equal-width bins would put
    almost every mandate in the first one and the curve would have nine empty points and no
    shape at all. `risk/calibration.py` bins the reliability diagram the same way for the
    same reason.

    The bucketing key is each mandate's **mean hazard over the horizon**, not its week-0
    hazard: the allocator gets twelve chances at every mandate and a mandate's riskiness
    over the run is what determines how many it takes.
    """
    if not book:
        return SegmentProfile(arm=metrics.arm, segments=[])

    ranked = sorted(
        ((sum(m.hazards) / len(m.hazards), m.mandate_id) for m in book),
        # Hazard first, then id: two mandates with identical risk must not swap places
        # between runs, because this is drawn into a committed PNG (ADR 0003).
        key=lambda pair: (pair[0], pair[1]),
    )
    size = len(ranked)
    segments = []
    for index in range(buckets):
        start = index * size // buckets
        stop = (index + 1) * size // buckets
        members = ranked[start:stop]
        if not members:
            continue
        segments.append(
            Segment(
                index=index,
                hazard_low=members[0][0],
                hazard_high=members[-1][0],
                mandates=len(members),
                asks=sum(metrics.asks_by_mandate.get(mandate_id, 0) for _, mandate_id in members),
            )
        )
    return SegmentProfile(arm=metrics.arm, segments=segments)


def format_profile(found: SegmentProfile) -> str:
    """The table, and the verdict read off it rather than typed."""
    if not found.segments:
        return "_no mandates to segment_"

    lines = [
        f"Ten equal-count hazard buckets over the book, asks counted across the whole "
        f"horizon, arm `{found.arm}`.",
        "",
        "| bucket | mean hazard (low - high) | mandates | asks | asks per mandate |",
        "|---:|---:|---:|---:|---:|",
    ]
    for segment in found.segments:
        lines.append(
            f"| {segment.index + 1} | {segment.hazard_low:.5f} - {segment.hazard_high:.5f} "
            f"| {segment.mandates:,} | {segment.asks:,} | {segment.asks_per_mandate:.3f} |"
        )

    peak = found.peak
    lines += ["", f"**The busiest bucket is {peak.index + 1} of {len(found.segments)}.**"]
    if found.is_inverted_u:
        lines += [
            "The peak is interior and both tails sit below half of it, so the curve **is** "
            "an inverted U -- the same shape Pinterest reported.",
            "",
            "**It is not the same mechanism, and calling it agreement would be wrong.** "
            "Pinterest's right-hand tail came from their most dormant users being less "
            "*responsive*; this model has no such term, because `efficacy_prior` belongs "
            "to the channel and is identical for every mandate. The only thing that can "
            "bend our curve down at the risky end is `alive`: over twelve weeks the doomed "
            "die, and both the gain and the backfire shrink with the survival weight while "
            "the channel cost does not. So this is survival attrition wearing Pinterest's "
            "shape. Two different phenomena, one silhouette.",
        ]
    elif found.is_threshold:
        lines += [
            "Asks rise monotonically with hazard: this is a **threshold**, not an inverted "
            "U, and it is exactly what the value function predicts. The gain from an ask is "
            "linear in `h` and the backfire cost does not involve `h` at all, so within a "
            "week the value of asking only ever increases with risk -- there is no hazard "
            "so high that an ask stops being worth making.",
            "",
            "**Pinterest's right-hand tail requires a mechanism this model does not have.** "
            "Their most dormant users were less *responsive*, so the uplift decayed at the "
            "far end. Here `efficacy_prior` is a property of the channel, identical for "
            "every mandate: a mandate three days from expiry and one certain to lapse are "
            "assumed equally persuadable. That is a real modelling gap and it is the "
            "honest reading of this plot -- not a result about allocation.",
        ]
    else:
        lines += [
            "The peak is interior but at least one tail is above half of it, so this is "
            "neither a clean threshold nor a clean inverted U. The shape is reported as "
            "measured rather than rounded to whichever published curve it is nearer.",
        ]
    return "\n".join(lines)


def plot(found: SegmentProfile, path: Path) -> Path:
    """Asks per mandate against hazard bucket. Written deterministically.

    Matplotlib stamps its own version into every PNG it writes and this file is committed,
    so the metadata is stripped -- ADR 0003, and the same treatment `eval/sweep.py` and
    `risk/calibration.py` already give their figures.
    """
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    ensure(path.parent)
    figure, axis = plt.subplots(figsize=(7.2, 4.4))

    indices = [s.index + 1 for s in found.segments]
    rates = [s.asks_per_mandate for s in found.segments]
    axis.bar(indices, rates, color="#4C72B0", width=0.72)
    axis.plot(indices, rates, color="#C44E52", marker="o", markersize=4, linewidth=1.4)

    peak = found.peak
    axis.axvline(peak.index + 1, color="#C44E52", linestyle=":", linewidth=1.0, alpha=0.7)

    axis.set_xticks(
        indices,
        [f"{s.index + 1}\n{s.hazard_mid:.4f}" for s in found.segments],
        fontsize=7,
    )
    axis.set_xlabel("hazard bucket (equal count) — bucket number and mean hazard")
    axis.set_ylabel("asks per mandate, whole horizon")
    shape = (
        "inverted U" if found.is_inverted_u else "threshold" if found.is_threshold else "neither"
    )
    axis.set_title(f"T3.7 Asks by risk segment, arm {found.arm} — {shape}")
    axis.grid(True, axis="y", linewidth=0.3, alpha=0.4)

    figure.tight_layout()
    figure.savefig(path, dpi=140, metadata={"Software": None})
    plt.close(figure)
    return path
