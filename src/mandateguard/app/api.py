"""T5.5 / T5.6 -- the FastAPI service, and the page it serves.

    uv run uvicorn mandateguard.app.api:app --reload
    open http://127.0.0.1:8000/        the surface
    open http://127.0.0.1:8000/docs    the API

| endpoint | answers |
|---|---|
| `POST /allocate` | given these mandates and this budget, who is asked and who is not |
| `GET /ladder` | every arm at one notch of the budget dial (T5.6) |
| `GET /refusal` | the four rupee terms behind a decision not to ask (T5.6) |
| `POST /explain` | why was this mandate not asked, in rupees |
| `POST /audit` | is this mandate compliant, and under which clauses |
| `GET /runs` | which ledgers exist |
| `GET /ledger` | what did a run decide, asked and not-asked |
| `GET /replay/{decision_id}` | re-run one historical decision and compare it |
| `GET /policy` | which rulebook is running, and where it came from |
| `GET /` | the surface: a read-only page, GET-only, shadow mode in its masthead |

### Every endpoint that could cause a contact goes through the guard

`safety/guard.py` is the only path to acting, and an HTTP layer is exactly the fourth call
site that would otherwise skip it (`docs/seekha.md` #104). So `/allocate` asks the guard for
each contact it would make, and the response says how many were authorised, how many were
refused, and which rung of the degradation ladder the service is on.

`/ladder` and `/refusal` are the deliberate exception, and they are not a loophole: they
replay a committed historical book to reproduce `docs/results.md`, so there is nothing to
authorise. Running them through `authorise` was tried and rejected on a number -- `P1` at
the top notch makes 16,236 simulated asks against a 500/hour rate limit, so a guarded
ladder would chart the limiter rather than the arms. They still halt on a policy-hash
mismatch, and they say `simulated: true` as well as `acted: false`.

In the shipped configuration that rung is **shadow**, so `/allocate` returns a plan and
nothing is sent. The response says so in a field rather than in documentation, because a
caller reading `decisions` and acting on them is the failure mode this whole layer exists to
make impossible.

### What `/allocate` deliberately does not do

It does not fit a hazard model. The caller supplies `MandateWeek` rows -- hazard, alive,
LTV, asks so far -- because those come from the person-period frame and the forecast, which
are a batch job measured in minutes, not a request. An endpoint that rebuilt them per call
would be a demo that times out on the second click. `eval/snapshot.py` is where a book comes
from; this is where a decision comes from.

### `/replay` is slow on purpose

Six seconds on the sample book, because replaying one decision means re-running the whole
run (`ledger/replay.py`). That is not an optimisation waiting to happen -- it is what makes
the answer a replay rather than a fresh decision about the same mandate. The endpoint says
so in its own description, so nobody files it as a performance bug.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from mandateguard.agent.auditor import RulesAuditor
from mandateguard.agent.explainer import RefusalExplainer, RefusalFacts
from mandateguard.allocator.baselines import bulk_channel
from mandateguard.allocator.mckp import MCKPPolicy
from mandateguard.app import ladder
from mandateguard.app.ladder import LadderView
from mandateguard.data.paths import ROOT
from mandateguard.ledger.replay import ReplayRefused, ReplayResult, replay
from mandateguard.ledger.store import Ledger, LedgerBroken
from mandateguard.models import (
    Decision,
    DecisionKind,
    MandateAuditContext,
    MandateVerdict,
    MandateWeek,
)
from mandateguard.policy.loader import load_params, load_policy, policy_hash
from mandateguard.safety.guard import Action, ActionKind, Degradation, Guard
from mandateguard.value.price import Pricer

app = FastAPI(
    title="MandateGuard",
    version="0.5.0",
    description=__doc__,
)

LEDGER_DIR = ROOT / "data" / "ledger"

_PARAMS = load_params()
_POLICY = load_policy()
_AUDITOR = RulesAuditor(_POLICY)
_EXPLAINER = RefusalExplainer()
"""Loaded once at import. `load_policy` re-hashes a 10 KB circular and re-checks twenty
quotes; doing that per request would make the policy check a latency budget rather than a
guarantee, and it would still only prove the file had not changed since the last request."""


def _guard() -> Guard:
    """A fresh guard per request.

    Per-request rather than per-process because the counters would otherwise accumulate
    across unrelated callers and the first caller of the day would exhaust the cap for
    everyone. That is also the honest limit: these counters are per-request, so the cap
    bounds one allocation rather than a service (`docs/limitations.md` §8.10).
    """
    return Guard(_PARAMS.safety, expected_policy_hash=policy_hash())


# --------------------------------------------------------------------------------
# Health.
# --------------------------------------------------------------------------------


@app.get("/health", tags=["service"])
def health() -> dict[str, str | int]:
    """Liveness, plus the two facts that decide whether an answer can be trusted.

    The policy hash and the degradation rung are here rather than on a separate admin
    endpoint because a caller that can reach `/health` can reach `/allocate`, and "which
    rulebook is this service running" is not an operator's private question.
    """
    state, why = _guard().state()
    return {
        "status": "ok" if state is not Degradation.HALTED else "halted",
        "service": "mandateguard",
        "policy_hash": policy_hash(),
        "rules": len(_POLICY.rules),
        "mode": _PARAMS.safety.mode,
        "degradation": state.name,
        "degradation_reason": why,
        "theta_placeholder": 0,
    }


# --------------------------------------------------------------------------------
# /allocate.
# --------------------------------------------------------------------------------


class AllocateRequest(BaseModel):
    book: list[MandateWeek] = Field(min_length=1)
    budget_inr: float = Field(ge=0)
    week: int = Field(default=0, ge=0)


class AllocateResponse(BaseModel):
    """The plan, and whether any of it may be acted on."""

    decisions: list[Decision]
    theta_inr: float | None = Field(
        default=None, description="the shadow price; None for arms with no dual"
    )
    budget_inr: float
    budget_spent_inr: float
    asked: int
    not_asked: int
    authorised: int = Field(description="contacts the guard permitted")
    refused_by_guard: int
    mode: str
    degradation: str
    acted: bool = Field(description="false in shadow mode: this is a plan, and nothing was sent")
    policy_hash: str


@app.post("/allocate", tags=["decide"], response_model=AllocateResponse)
def allocate(request: AllocateRequest) -> AllocateResponse:
    """Decide who to ask this week, then ask the guard whether any of it may happen.

    The caller supplies hazards and LTVs rather than a customer list: those come from the
    person-period frame and the forecast, which are a batch job. See the module docstring.
    """
    guard = _guard()
    state, why = guard.state()
    if state is Degradation.HALTED:
        raise HTTPException(status_code=503, detail=why)

    policy = MCKPPolicy(_PARAMS)
    response = policy.allocate(request.book, request.budget_inr, request.week)

    channels = {c.name: c for c in _PARAMS.channels}
    authorised = refused = 0
    for decision in response.decisions:
        if decision.kind is not DecisionKind.ASKED:
            continue
        cost = channels[decision.channel].cost_inr if decision.channel in channels else 0.0
        verdict = guard.authorise(
            Action(kind=ActionKind.CONTACT, mandate_id=decision.mandate_id, cost_inr=cost)
        )
        if verdict.allowed:
            authorised += 1
        else:
            refused += 1

    return AllocateResponse(
        decisions=response.decisions,
        theta_inr=response.theta_inr,
        budget_inr=request.budget_inr,
        budget_spent_inr=response.budget_spent_inr,
        asked=sum(1 for d in response.decisions if d.kind is DecisionKind.ASKED),
        not_asked=sum(1 for d in response.decisions if d.kind is DecisionKind.NOT_ASKED),
        authorised=authorised,
        refused_by_guard=refused,
        mode=_PARAMS.safety.mode,
        degradation=state.name,
        acted=_PARAMS.safety.mode == "live" and state is Degradation.NORMAL,
        policy_hash=policy_hash(),
    )


# --------------------------------------------------------------------------------
# /ladder -- the budget dial. Simulated, and it says so in a field.
# --------------------------------------------------------------------------------


@app.get("/ladder", tags=["decide"], response_model=LadderView)
def ladder_rung(notch: int = Query(0, ge=0, description="0..16, a notch of the budget dial")):
    """Every arm at one budget on the committed sample, for the surface's dial.

    **This endpoint asks the guard for its state and never for authorisation, which is a
    deliberate exception and the only one.** `Guard.authorise` gates *acting*, and there is
    nothing here to act on: this replays a historical committed book to reproduce the table
    in `docs/results.md` §3, exactly as `scripts/make_results.py` does offline -- and that
    script does not call the guard either, correctly, because `world.run` is a simulator
    rather than a send queue.

    Running it through `authorise` was considered and rejected on a number. `P1` at the top
    notch spends 16,236 asks against `safety.max_sends_per_window` of 500 per hour, so a
    guarded ladder would start refusing simulated contacts at the 501st and the chart would
    report the rate limiter's counters instead of the arms' behaviour. A safety layer that
    silently changes the measurement it is protecting is not protecting it.

    What it *does* keep is the halt: a service running under a mismatched policy hash must
    not serve numbers, including simulated ones. And the response carries both `acted:
    false` and `simulated: true`, because the first alone still reads like a plan somebody
    could execute.
    """
    guard = _guard()
    state, why = guard.state()
    if state is Degradation.HALTED:
        raise HTTPException(status_code=503, detail=why)

    try:
        notches = ladder.budgets(_PARAMS)
    except FileNotFoundError as exc:
        # The GATE 5 path: a fresh clone has no derived frames until make_results.py runs.
        # 503 with the message load_snapshot already wrote, which names the two scripts.
        raise HTTPException(status_code=503, detail=str(exc)) from exc

    if notch >= len(notches):
        raise HTTPException(
            status_code=404,
            detail=f"notch {notch} is off the dial: this ladder has {len(notches)} notches "
            f"(0..{len(notches) - 1}), from INR {notches[0]:,.2f} to INR {notches[-1]:,.2f}.",
        )

    rung, cached = ladder.rung(_PARAMS, notch)
    channel = bulk_channel(_PARAMS.channels)
    return LadderView(
        snapshot_id=ladder.SNAPSHOT_ID,
        mandates=len(ladder.book(_PARAMS)),
        weeks=_PARAMS.horizon.weeks,
        bulk_channel=channel.name,
        bulk_channel_cost_inr=channel.cost_inr,
        budgets=notches,
        arms_excluded=["P5"],
        excluded_because=(
            "P5 WhittleIndex is ~27.9s per point, so a 17-notch ladder would be about eight "
            "minutes. docs/results.md 2 is where it is measured; 3 is a five-arm table for "
            "the same reason."
        ),
        rung=rung,
        cached=cached,
        mode=_PARAMS.safety.mode,
        degradation=state.name,
        acted=False,
        simulated=True,
        policy_hash=policy_hash(),
    )


# --------------------------------------------------------------------------------
# /refusal -- the four terms behind a decision not to ask.
# --------------------------------------------------------------------------------


class RefusalTerms(BaseModel):
    """One not-asked mandate, priced, with the terms kept apart so they can be read.

    **The four money fields sum.** `net = gain - backfire - fatigue - channel_cost`, which
    is `AskPrice.net_inr`'s own definition. An earlier draft of this panel listed lapse
    loss, backfire, fatigue and *reachability* as four peers -- and those do not sum,
    because reachability is **inside** backfire: `value/ltv.py` prices a revocation at
    `L*(1-r) + alpha*R`, where the second term is the channel to that customer. Four
    numbers that do not add up, on the panel that is this project's differentiator, is the
    worst possible place for that mistake.

    So reachability arrives as `reachability_at_risk_inr`, an annotation on the backfire
    row rather than a fifth term -- and it is the whole point of the panel: the refusal is
    not about five paise of email, it is about possibly losing the ability to ever reach
    this customer again.
    """

    mandate_id: str
    week: int
    channel: str = Field(description="the channel that would have been used, had we asked")
    hazard: float
    ltv_remaining_inr: float

    gain_inr: float = Field(description="lapses this ask would prevent, in rupees")
    backfire_inr: float = Field(description="revocations this ask would cause, in rupees")
    reachability_at_risk_inr: float = Field(
        description="the part of backfire that is the lost channel, not the lost mandate"
    )
    fatigue_inr: float = Field(description="patience spent")
    channel_cost_inr: float = Field(description="what it costs to send")
    net_inr: float = Field(description="gain - backfire - fatigue - channel_cost")

    deaths_prevented: float
    revocations_caused: float
    kind: str = Field(
        description="not_worth_asking when the best ask still nets negative; outbid when it "
        "was worth making and the budget went to someone else"
    )
    reason: str = Field(description="the allocator's own words, from Decision.reason")


class RefusalView(BaseModel):
    notch: int
    budget_inr: float
    week: int
    asked: int
    not_asked: int
    refusals: list[RefusalTerms]
    simulated: bool
    mode: str


@app.get("/refusal", tags=["decide"], response_model=RefusalView)
def refusal(
    notch: int = Query(0, ge=0, description="which notch of the dial to price"),
    limit: int = Query(5, ge=1, le=50, description="how many refusals to return"),
):
    """Why the allocator declined to ask, in rupees, for the most expensive refusals.

    One week of `P4`, not twelve: this panel answers "what was this decision made of",
    which is a single-period question. The dial next to it answers the horizon question.

    Sorted by how close the refusal was -- least negative `net_inr` first -- because the
    marginal refusals are the interesting ones. A mandate with a net of -900 was never a
    candidate; a mandate at -0.40 is the allocator working.
    """
    state, why = _guard().state()
    if state is Degradation.HALTED:
        raise HTTPException(status_code=503, detail=why)

    try:
        notches = ladder.budgets(_PARAMS)
        book = ladder.book(_PARAMS)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if notch >= len(notches):
        raise HTTPException(
            status_code=404, detail=f"notch {notch} is off a {len(notches)}-notch dial"
        )

    budget = notches[notch]
    week = [
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
            weeks_since_last_ask=None,
        )
        for m in book
    ]
    plan = MCKPPolicy(_PARAMS).allocate(week, budget, 0)
    rows = {row.mandate_id: row for row in week}
    entries = {m.mandate_id: m for m in book}

    pricer = Pricer(_PARAMS)
    alpha = _PARAMS.value.alpha_reachability
    nu = _PARAMS.value.nu_complaint

    priced: list[RefusalTerms] = []
    for decision in plan.decisions:
        if decision.kind is not DecisionKind.NOT_ASKED:
            continue
        row = rows[decision.mandate_id]
        # NOT `pricer.best_channel`. That returns None unless the best ask is *worth
        # making*, so using it here silently dropped every mandate the allocator refused
        # because nothing was worth it -- which is the entire population this panel exists
        # to explain. The one row that survived was an `outbid` mandate, and a panel
        # titled "why we did not ask" that can only show mandates we wanted to ask is
        # worse than no panel. So price every channel and take the best, sign and all.
        priced_channels = [pricer.price(row, channel) for channel in _PARAMS.channels]
        price = max(priced_channels, key=lambda p: (p.net_inr, -p.channel_cost_inr, p.channel))
        reach = entries[decision.mandate_id].reachability_value_inr
        priced.append(
            RefusalTerms(
                mandate_id=price.mandate_id,
                week=0,
                channel=price.channel,
                hazard=row.hazard,
                ltv_remaining_inr=row.ltv_remaining_inr,
                gain_inr=price.gain_inr,
                backfire_inr=price.backfire_inr,
                reachability_at_risk_inr=nu * price.revocations_caused * alpha * reach,
                fatigue_inr=price.fatigue_inr,
                channel_cost_inr=price.channel_cost_inr,
                net_inr=price.net_inr,
                deaths_prevented=price.deaths_prevented,
                revocations_caused=price.revocations_caused,
                kind="outbid" if price.net_inr > 0 else "not_worth_asking",
                reason=decision.reason,
            )
        )

    # Least-negative first: the marginal refusals are the interesting ones. A mandate at
    # -900 was never a candidate; a mandate at -0.40 is the allocator working.
    priced.sort(key=lambda p: -p.net_inr)
    return RefusalView(
        notch=notch,
        budget_inr=budget,
        week=0,
        asked=sum(1 for d in plan.decisions if d.kind is DecisionKind.ASKED),
        not_asked=len(priced),
        refusals=priced[:limit],
        simulated=True,
        mode=_PARAMS.safety.mode,
    )


# --------------------------------------------------------------------------------
# /explain.
# --------------------------------------------------------------------------------


class ExplainRequest(BaseModel):
    facts: RefusalFacts


class ExplainResponse(BaseModel):
    mandate_id: str
    text: str
    source: str
    allowed_amounts: list[str] = Field(
        description="every rupee figure this explanation is permitted to contain"
    )


@app.post("/explain", tags=["decide"], response_model=ExplainResponse)
def explain(request: ExplainRequest) -> ExplainResponse:
    """Why a mandate was not asked, in rupees.

    `allowed_amounts` is in the response deliberately. It is the set the explanation was
    checked against, so a caller can re-run the fabrication check themselves rather than
    trusting that this service ran it.
    """
    explanation = _EXPLAINER.explain(request.facts)
    return ExplainResponse(
        mandate_id=explanation.mandate_id,
        text=explanation.text,
        source=explanation.source,
        allowed_amounts=sorted(request.facts.allowed_amounts()),
    )


# --------------------------------------------------------------------------------
# /audit.
# --------------------------------------------------------------------------------


@app.post("/audit", tags=["comply"], response_model=MandateVerdict)
def audit(context: MandateAuditContext) -> MandateVerdict:
    """Judge one mandate against the compiled rulebook.

    Returns the full `MandateVerdict`, including the rules that did **not** apply. That is
    more than a caller usually wants and it is the point: "clause 6(a) did not apply because
    this is a FASTag mandate" and "clause 6(a) was never evaluated" have to be
    distinguishable from outside the service, not only from inside it.
    """
    return _AUDITOR.audit(context)


# --------------------------------------------------------------------------------
# /ledger.
# --------------------------------------------------------------------------------


class RunIndex(BaseModel):
    runs: list[str]


@app.get("/runs", tags=["audit"], response_model=RunIndex)
def runs() -> RunIndex:
    """Which ledgers exist. `/ledger` needs a `run_id` and nothing else hands one out.

    A surface cannot hard-code the name: a run id looks like
    `P4-sample-s20260905-b500.00`, which encodes `params.seed` and
    `horizon.budget_inr_per_week`. Both live in `config/params.yaml`, so a typed literal
    would drift from the ledger the moment either changed, and the ledger tab would show an
    empty state that looked like "no decisions" rather than "wrong filename".

    `data/ledger/` is gitignored, so an empty list is the ordinary state of a fresh clone
    rather than an error: run `scripts/make_ledger.py --sample` and there will be one.
    """
    if not LEDGER_DIR.is_dir():
        return RunIndex(runs=[])
    return RunIndex(runs=sorted(path.stem for path in LEDGER_DIR.glob("*.jsonl")))


class LedgerPage(BaseModel):
    run_id: str
    entries: int
    asked: int
    not_asked: int
    refusal_share: float
    head: str
    page: list[dict]


@app.get("/ledger", tags=["audit"], response_model=LedgerPage)
def ledger(
    run_id: str = Query(..., description="the run whose ledger to read"),
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=500),
    kind: str | None = Query(None, description="'asked' or 'not_asked' to filter"),
) -> LedgerPage:
    """Read a run's decisions, and verify the chain while doing it.

    The chain is walked on every call rather than cached. A ledger endpoint that served
    rows without checking them would be a viewer, and the one thing this ledger claims over
    a log file is that reading it tells you whether it has been edited.
    """
    path = LEDGER_DIR / f"{run_id}.jsonl"
    store = Ledger(path)
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no ledger for run {run_id!r}")
    try:
        stats = store.verify()
    except LedgerBroken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    wanted = [entry for entry in store if kind is None or entry.decision.kind.value == kind]
    page = [entry.model_dump(mode="json") for entry in wanted[offset : offset + limit]]
    return LedgerPage(
        run_id=run_id,
        entries=stats.entries,
        asked=stats.asked,
        not_asked=stats.not_asked,
        refusal_share=stats.refusal_share,
        head=stats.head,
        page=page,
    )


# --------------------------------------------------------------------------------
# /replay.
# --------------------------------------------------------------------------------


@app.get("/replay/{decision_id:path}", tags=["audit"], response_model=ReplayResult)
def replay_decision(decision_id: str) -> ReplayResult:
    """Re-run one historical decision and compare it byte for byte.

    **Slow on purpose.** Replaying one decision means re-running the whole run, because an
    allocation is not a function of one mandate. About six seconds on the sample book. A
    refusal comes back as 409 rather than 500: "this cannot be replayed under today's
    policy" is an answer, not a server fault.
    """
    run_id = decision_id.split(":", 1)[0]
    path = LEDGER_DIR / f"{run_id}.jsonl"
    if not path.is_file():
        raise HTTPException(status_code=404, detail=f"no ledger for run {run_id!r}")
    try:
        return replay(Ledger(path), decision_id)
    except ReplayRefused as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


# --------------------------------------------------------------------------------
# /policy.
# --------------------------------------------------------------------------------


class PolicySummary(BaseModel):
    circular_no: str
    dated: date
    url: str
    policy_hash: str
    rules: int
    clauses: list[str]


@app.get("/policy", tags=["comply"], response_model=PolicySummary)
def policy() -> PolicySummary:
    """What rulebook this service is running, and where it came from."""
    return PolicySummary(
        circular_no=_POLICY.source.circular_no,
        dated=_POLICY.source.dated,
        url=_POLICY.source.url,
        policy_hash=policy_hash(),
        rules=len(_POLICY.rules),
        clauses=sorted({rule.clause for rule in _POLICY.rules}),
    )


# --------------------------------------------------------------------------------
# The surface. Last, because the file is service-first and an HTML route at the top
# would make the OpenAPI document open on a presentation concern.
# --------------------------------------------------------------------------------

STATIC = Path(__file__).parent / "static"

_ASSETS: dict[str, tuple[Path, str]] = {
    "newsreader.woff2": (STATIC / "fonts" / "newsreader.woff2", "font/woff2"),
    "jetbrains-mono.woff2": (STATIC / "fonts" / "jetbrains-mono.woff2", "font/woff2"),
}
"""An allowlist, not a directory mount, and for two reasons.

**Python does not know what a woff2 is.** `mimetypes.guess_type("a.woff2")` returns
`(None, None)` on 3.12, so Starlette's `FileResponse` -- and therefore any `StaticFiles`
mount -- serves both fonts as `text/plain`. Browsers usually tolerate that for `@font-face`.
"Usually" is not a standard to demo on, and it fails outright under `nosniff`.

**A mount publishes whatever lands in the directory.** In a project whose central claim is
that the guard is the only path to acting, an accidentally-public directory is a needlessly
open door. Three files are named here; a fourth has to be added on purpose.
"""


@app.get("/", include_in_schema=False, response_class=FileResponse)
def surface() -> FileResponse:
    """The page. Read-only, GET-only, and it says shadow mode in its masthead.

    `include_in_schema=False` keeps `/docs` about the service. The page is a *client* of
    this API in exactly the way `app/ui.py` is -- it crosses the HTTP boundary over `fetch`
    and imports no allocator -- so the boundary stays real rather than decorative.
    """
    return FileResponse(STATIC / "index.html", media_type="text/html; charset=utf-8")


@app.get("/static/{name}", include_in_schema=False, response_class=FileResponse)
def asset(name: str) -> FileResponse:
    """Serve one named asset with an explicit media type. 404 on anything not listed."""
    if name not in _ASSETS:
        raise HTTPException(status_code=404, detail=f"no asset {name!r}")
    path, media_type = _ASSETS[name]
    if not path.is_file():
        # A vendored font that did not ship must not 500. The page declares a real
        # fallback stack and is designed to look finished without these.
        raise HTTPException(status_code=404, detail=f"{name} is not vendored in this build")
    return FileResponse(path, media_type=media_type)


__all__ = ["app"]
