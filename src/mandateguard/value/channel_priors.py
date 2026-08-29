"""A softer channel gives up a little and avoids a lot. **Chrome, USENIX Security 2021.**

Chrome replaced its permission prompt with a quieter UI across roughly 300 million users
and measured both sides of the trade. The softer surface **lost 2-5% of grants** and
**avoided 17-31% of permanent refusals**. That asymmetry -- roughly seven to one -- is the
strongest public evidence that the intrusiveness of an ask is a variable worth optimising
separately from whether to ask at all.

What this module does, and does not, add
----------------------------------------
The grant-loss half is **already in the config**: `efficacy_prior` runs from 0.02 for an
in-app nudge to 0.28 for an agent call, so the channel table already says that softer
channels convert less. Applying Chrome's 2-5% on top of that would be counting the same
effect twice.

What the table does *not* say is that softer channels also **burn less**. Backfire in
`intervention.backfire` is a property of the ask, not of the channel: it makes the fifth
contact more dangerous than the first, but says nothing about a letter being more
provocative than an in-app banner. That is the gap this module fills, and it is the only
thing it fills.

```
backfire[i, c, t] = intervention.backfire(n) * softness_multiplier[c]
```

The multiplier compounds one step at a time down the cost ladder, because the ladder *is*
the intrusiveness ordering -- the channels get more expensive precisely by becoming harder
to ignore. The most intrusive channel is the reference at 1.0.

The number is sourced, its application is not
---------------------------------------------
`backfire_avoided_per_softer_step` is the midpoint of Chrome's published 17-31% range.
Chrome measured *one* step, between two permission UIs, not a seven-rung ladder from
in-app to agent call, so treating each rung as one Chrome-sized step is this project's
extrapolation and is recorded as such in `docs/calibration.md` §4. What is not
extrapolation is the direction and the rough size of the asymmetry, and those are what
make the multi-channel formulation worth having.
"""

from __future__ import annotations

from pydantic import BaseModel

from mandateguard.models import Channel


class ChannelLadder(BaseModel):
    """The channels ordered by intrusiveness, with each one's backfire multiplier.

    Built once and passed around rather than recomputed per decision: it depends only on
    the config, and rebuilding it inside a loop over a million mandate-weeks would be a
    lot of work to arrive at the same seven numbers.
    """

    model_config = {"frozen": True}

    multipliers: dict[str, float]

    def backfire_multiplier(self, channel: Channel) -> float:
        """How much of the base backfire this channel actually carries.

        An unknown channel returns 1.0 -- the most intrusive assumption -- because
        silently discounting a channel nobody configured would be the one direction of
        error this project cannot afford.
        """
        return self.multipliers.get(channel.name, 1.0)


def build_ladder(channels: list[Channel], backfire_avoided_per_step: float) -> ChannelLadder:
    """Rank channels by cost and compound the avoided backfire down the ladder.

    Ties on cost are broken by efficacy then by name, so the ladder is total (ADR 0003):
    two channels at the same price must not swap rungs between runs and change every
    per-channel number with them.
    """
    ordered = sorted(channels, key=lambda c: (c.cost_inr, c.efficacy_prior, c.name))
    top = len(ordered) - 1
    return ChannelLadder(
        multipliers={
            channel.name: (1.0 - backfire_avoided_per_step) ** (top - rank)
            for rank, channel in enumerate(ordered)
        }
    )
