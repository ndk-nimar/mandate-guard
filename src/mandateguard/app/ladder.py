"""T5.6 -- the budget ladder behind the dial, cached so a judge can drag it.

The page's whole argument is one gesture: move the budget, watch `P1` destroy tens of
thousands of rupees while `P4` creates hundreds. This module is what makes that gesture
cheap enough to be a gesture rather than a wait.

### It calls the sweep, it does not rebuild it

`rung()` runs `sweep.budget_sweep(book, params, [budget])` -- the exact function
`scripts/make_results.py` calls to produce `docs/results.md` §3. Reconstructing the arm
list here instead would look harmless and be wrong: `sweep.ARMS` maps `P4` to
`_mckp_without_theta` (`MCKPPolicy(params, with_theta=False)`) while §2's ladder uses
`MCKPPolicy(params)`, and the two differ by about INR 35 on the `P4` column. A test
asserting only `P4 > P0` would never catch it, and the page would quietly disagree with the
document it is a view of. `tests/test_ladder.py` asserts the rendered strings match instead.

### Notches, not a free budget

The dial has exactly the 17 log-spaced budgets `sweep.budget_ladder` produces, because
those are the only budgets `results.md` §3 vouches for. A continuous slider would make this
cache unbounded *and* let the dial land between rungs, showing numbers that appear in no
committed document -- which is the same failure as typing a number into a document.

### The book is lazy, and so is every rung

`data/processed/sample/person_periods.parquet` is gitignored and is **absent in a fresh
clone** until `make_results.py` has run. Building the book at import would therefore make
`import mandateguard.app.api` raise on exactly the GATE 5 path -- taking down `/health`,
whose entire job is to say whether the service is up. So nothing here runs until something
asks, and a missing frame surfaces as `FileNotFoundError` carrying `load_snapshot`'s own
message, which already names the scripts that fix it.

Per-rung rather than precomputed for two measured reasons. Precomputing all 17 costs about
29 seconds at startup: `scripts/dev.py` polls `/health` with a 30-second deadline, so that
would sit on the boundary and fail intermittently on demo day; and `tests/test_api.py`
builds its `TestClient` at module scope, so import-time work is paid by every CI run
forever. Lazy costs about 2.5s the first time a notch is touched and nothing every time
after -- and a judge drags the dial back and forth.

### No lock

Two fast clicks can enter the same solve concurrently under uvicorn's threadpool. The cost
is one duplicated 2.5s solve, not a wrong answer -- both callers compute the same rung from
the same immutable book. A lock would buy correctness that is already there and add a way
to deadlock the surface.

### P5 is absent, and that is not a decision taken here

`sweep.ARMS` already omits it and `scripts/make_results.py` argues why: `WhittleIndex` is
27.9s per point, so a 17-notch ladder would be about eight minutes. `results.md` §3 is a
five-arm table for the same reason. The page says so in words rather than showing five arms
where the document shows six.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from mandateguard.allocator.baselines import bulk_channel
from mandateguard.eval import snapshot, sweep, world
from mandateguard.policy.loader import Params

SNAPSHOT_ID = "sample"
"""The committed slice, and the only book this surface serves.

Named rather than parameterised: a dial that could be pointed at an unbuilt full-data book
would spend ninety minutes on its first click and time out behind a proxy."""


class LadderPoint(BaseModel):
    """One arm at one budget -- the cell `docs/results.md` §3 prints, plus §2's columns.

    Every field is named explicitly rather than dumped from `RunMetrics`, because
    `profit_inr` and `retention_rate` are **computed properties**: `model_dump()` drops
    them silently, and `profit_inr` is the exact curve §3 plots. A response that lost it
    would still be valid JSON and the chart would be of something else.
    """

    arm: str
    profit_inr: float
    gain_over_floor_inr: float
    """`profit_inr` minus `P0`'s. This, not profit, is what the chart plots.

    The five arms span 0.05% of profit in the interesting region (413,219 to 413,432), so a
    chart baselined at zero draws five identical bars and the dial appears to do nothing.
    Subtracting the floor is what makes the gesture legible."""

    mandates_retained: float
    """Fractional on purpose -- an expectation over survival weights, not a count. Render
    1,215.9; rounding it to 1,216 turns an expectation into a claim about individuals."""

    retention_rate: float
    revocations_caused: float
    asks_spent: int
    net_value_inr: float
    theta_inr: float | None
    """**`None` on every arm of this ladder, and that is correct rather than missing.**

    `sweep.ARMS` maps `P4` to `_mckp_without_theta`, which skips the LP relaxation because a
    sweep solving hundreds of duals to print none of them is waste. So this ladder has no
    shadow price, exactly as `docs/results.md` §3 has no theta column.

    The surface must therefore **not** display a theta here. Turning on `with_theta` to fill
    the field would change the arm and move the profit figures off the committed table --
    trading the one property this endpoint exists to have for a headline number. Theta is
    published in `docs/eval.md` §4, on its own budget ladder, and the page points there."""


class LadderRung(BaseModel):
    """One notch of the dial: every arm at one budget."""

    notch: int = Field(ge=0)
    budget_inr: float = Field(ge=0)
    asks_affordable: int = Field(ge=0)
    """How many bulk asks this budget buys. The dial's units are rupees; this is what the
    rupees mean, and it is the number a merchant actually reasons in."""

    points: list[LadderPoint]


class LadderView(BaseModel):
    """A rung, plus everything the page needs to draw the dial before any rung solves."""

    snapshot_id: str
    mandates: int
    weeks: int
    bulk_channel: str
    bulk_channel_cost_inr: float
    budgets: list[float]
    arms_excluded: list[str]
    excluded_because: str
    rung: LadderRung
    cached: bool
    mode: str
    degradation: str
    acted: bool
    """Always false. The page is a view of a simulation over a committed historical book."""

    simulated: bool
    """Always true, and separate from `acted` on purpose.

    `acted: false` alone says "we did not send"; a reader can still take it as a plan that
    someone could execute. `simulated: true` says the thing that is actually true here --
    there was never a customer at the other end of any of these asks."""

    policy_hash: str


_BOOK: list[world.BookMandate] | None = None
_RUNGS: dict[int, LadderRung] = {}


def book(params: Params) -> list[world.BookMandate]:
    """The committed book, built once per process.

    Delegates to `eval/snapshot.py` rather than loading frames here, so the surface and the
    replay path build the book the same way. A second loader would be free to drift, and a
    dial that disagreed with `replay` would be worse than no dial.
    """
    global _BOOK
    if _BOOK is None:
        _BOOK = snapshot.load_snapshot(SNAPSHOT_ID, params)
    return _BOOK


def budgets(params: Params) -> list[float]:
    """The dial's notches: `budget_ladder`'s output, never a typed list.

    Read from the channel table and the book size, so changing either moves the dial and
    `results.md` §3 together. A hard-coded ladder would drift from the document on the
    first edit to `config/params.yaml` and nothing would fail.
    """
    return sweep.budget_ladder(bulk_channel(params.channels).cost_inr, len(book(params)))


def rung(params: Params, notch: int) -> tuple[LadderRung, bool]:
    """Solve one notch, or return the cached one. Returns `(rung, was_cached)`."""
    if notch in _RUNGS:
        return _RUNGS[notch], True

    ladder = budgets(params)
    budget = ladder[notch]
    live = book(params)
    arms = sweep.budget_sweep(live, params, [budget])

    floor = next(a.points[0].metrics.profit_inr for a in arms if a.arm == "P0")
    cost = bulk_channel(params.channels).cost_inr
    solved = LadderRung(
        notch=notch,
        budget_inr=budget,
        asks_affordable=int(budget // cost) if cost > 0 else 0,
        points=[
            LadderPoint(
                arm=arm.arm,
                profit_inr=m.profit_inr,
                gain_over_floor_inr=m.profit_inr - floor,
                mandates_retained=m.mandates_retained,
                retention_rate=m.retention_rate,
                revocations_caused=m.revocations_caused,
                asks_spent=m.asks_spent,
                net_value_inr=m.net_value_inr,
                theta_inr=m.theta_inr,
            )
            for arm in arms
            for m in [arm.points[0].metrics]
        ],
    )
    _RUNGS[notch] = solved
    return solved, False


def reset() -> None:
    """Drop both caches. For tests that change params underneath this module."""
    global _BOOK
    _BOOK = None
    _RUNGS.clear()
