"""T5.5 -- the FastAPI service. Four endpoints, and the reasons they are shaped this way.

    uv run uvicorn mandateguard.app.api:app --reload
    open http://127.0.0.1:8000/docs

| endpoint | answers |
|---|---|
| `POST /allocate` | given these mandates and this budget, who is asked and who is not |
| `POST /explain` | why was this mandate not asked, in rupees |
| `POST /audit` | is this mandate compliant, and under which clauses |
| `GET /ledger` | what did a run decide, asked and not-asked |
| `GET /replay/{decision_id}` | re-run one historical decision and compare it |

### Every endpoint goes through the guard

`safety/guard.py` is the only path to acting, and an HTTP layer is exactly the fourth call
site that would otherwise skip it (`docs/seekha.md` #104). So `/allocate` asks the guard for
each contact it would make, and the response says how many were authorised, how many were
refused, and which rung of the degradation ladder the service is on.

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

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from mandateguard.agent.auditor import RulesAuditor
from mandateguard.agent.explainer import RefusalExplainer, RefusalFacts
from mandateguard.allocator.mckp import MCKPPolicy
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


__all__ = ["app"]
