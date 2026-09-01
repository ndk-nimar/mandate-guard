"""T5.5 -- the FastAPI service.

The task's bar is "the OpenAPI docs render and all four return valid responses". That is a
low bar and it is met in the first two tests. The rest of the file is the part worth having:
the service must not be a way around the guarantees the layers below it enforce.

Three of those, specifically:

* an HTTP layer must not become the fourth call site that skips the safety guard;
* a plan returned in shadow mode must say, in the payload, that it is a plan;
* a refusal must arrive as an answer with a status code that means "no", not as a 500.
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from mandateguard.app.api import LEDGER_DIR, app
from mandateguard.ledger.store import Ledger, build_entry
from mandateguard.models import Decision, DecisionKind, MandateWeek
from mandateguard.policy.loader import policy_hash


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def week_row(mandate_id: str, hazard: float = 0.2, ltv: float = 2000.0) -> dict:
    return MandateWeek(
        mandate_id=mandate_id,
        week=0,
        hazard=hazard,
        alive=1.0,
        ltv_remaining_inr=ltv,
        reachability_value_inr=100.0,
        recovery_after_lapse=0.41,
        recovery_after_revocation=0.08,
        asks_so_far=0,
    ).model_dump(mode="json")


# --------------------------------------------------------------------------------
# The stated bar.
# --------------------------------------------------------------------------------


def test_the_openapi_document_renders(client):
    schema = client.get("/openapi.json")
    assert schema.status_code == 200
    paths = schema.json()["paths"]
    for route in ("/allocate", "/explain", "/audit", "/ledger", "/policy"):
        assert route in paths, route
    assert any(p.startswith("/replay") for p in paths)


def test_health_reports_the_rulebook_and_the_rung(client):
    """Which rulebook a service is running is not an operator's private question."""
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["policy_hash"] == policy_hash()
    assert body["rules"] == 20
    assert body["mode"] == "shadow"
    assert body["degradation"] == "NORMAL"


def test_policy_names_its_source(client):
    body = client.get("/policy").json()
    assert body["circular_no"] == "RBI/DPSS/2026-27/396"
    assert body["rules"] == 20
    assert "2" in body["clauses"]
    assert body["url"].startswith("https://www.rbi.org.in")


# --------------------------------------------------------------------------------
# /allocate.
# --------------------------------------------------------------------------------


def test_allocate_returns_a_decision_for_every_mandate(client):
    """The contract is total: a not-asked mandate is a record with a reason, not an
    omission. That is enforced in the harness and has to survive the HTTP boundary."""
    book = [week_row(f"mg_{i}", hazard=0.1 * i) for i in range(1, 6)]
    body = client.post("/allocate", json={"book": book, "budget_inr": 5.0, "week": 0}).json()
    assert len(body["decisions"]) == len(book)
    assert body["asked"] + body["not_asked"] == len(book)
    assert {d["mandate_id"] for d in body["decisions"]} == {b["mandate_id"] for b in book}


def test_a_zero_budget_still_sends_the_free_in_app_nudge(client):
    """`docs/problem.md` §5.3, surfaced through the API and worth pinning here because it
    surprises everyone once. Non-intrusive zero-cost channels do not consume the ask
    budget, so a budget of zero does not mean "contact nobody" -- it means "contact nobody
    through a channel that costs anything". "Not selected" never means "abandoned".

    This test was written the other way round first, expecting refusals at a zero budget.
    The premise was wrong, not the allocator.
    """
    book = [week_row(f"mg_{i}") for i in range(1, 6)]
    body = client.post("/allocate", json={"book": book, "budget_inr": 0.0}).json()
    assert body["asked"] == len(book)
    assert body["budget_spent_inr"] == 0.0
    assert {d["channel"] for d in body["decisions"]} == {"in_app"}


def test_every_not_asked_decision_carries_a_reason(client):
    """A mandate is only left alone when even a free ask prices negative -- tiny LTV, tiny
    hazard, so fatigue and backfire outweigh what the ask could save."""
    book = [week_row(f"mg_{i}", hazard=0.01, ltv=1.0) for i in range(1, 6)]
    body = client.post("/allocate", json={"book": book, "budget_inr": 0.0}).json()
    refusals = [d for d in body["decisions"] if d["kind"] == DecisionKind.NOT_ASKED.value]
    assert len(refusals) == len(book)
    assert all(d["reason"].strip() for d in refusals)
    assert all("INR" in d["reason"] for d in refusals), "a refusal has to carry its rupees"


def test_the_response_says_in_the_payload_that_nothing_was_sent(client):
    """A caller reading `decisions` and acting on them is the failure this layer exists to
    make impossible, so shadow mode is a field rather than a line of documentation."""
    body = client.post("/allocate", json={"book": [week_row("mg_1")], "budget_inr": 100.0}).json()
    assert body["mode"] == "shadow"
    assert body["acted"] is False


def test_allocate_runs_every_contact_past_the_guard(client):
    """The HTTP layer must not become the fourth call site that skips the guard."""
    book = [week_row(f"mg_{i}", hazard=0.9, ltv=50_000.0) for i in range(1, 40)]
    body = client.post("/allocate", json={"book": book, "budget_inr": 500.0}).json()
    assert body["authorised"] + body["refused_by_guard"] == body["asked"]
    assert body["degradation"] == "NORMAL"


def test_allocate_reports_the_shadow_price_when_the_budget_binds(client):
    book = [week_row(f"mg_{i}", hazard=0.5, ltv=20_000.0) for i in range(1, 25)]
    body = client.post("/allocate", json={"book": book, "budget_inr": 1.0}).json()
    assert body["theta_inr"] is None or body["theta_inr"] >= 0


def test_an_empty_book_is_rejected_at_the_boundary(client):
    assert client.post("/allocate", json={"book": [], "budget_inr": 5.0}).status_code == 422


def test_a_negative_budget_is_rejected(client):
    body = {"book": [week_row("mg_1")], "budget_inr": -1.0}
    assert client.post("/allocate", json=body).status_code == 422


def test_a_mandate_row_with_q_below_r_is_rejected(client):
    """`MandateWeek` enforces q > r, and the API must not be a way around a model
    invariant. A row that got past here would reach the pricer."""
    row = week_row("mg_1")
    row["recovery_after_lapse"] = 0.05
    row["recovery_after_revocation"] = 0.4
    assert client.post("/allocate", json={"book": [row], "budget_inr": 5.0}).status_code == 422


# --------------------------------------------------------------------------------
# /audit and /explain.
# --------------------------------------------------------------------------------


def test_audit_names_the_clauses_it_rests_on(client):
    body = client.post(
        "/audit",
        json={"mandate_id": "mg_1", "rail": "upi_autopay", "amount_inr": 20000.0},
    ).json()
    assert body["verdict"] == "non_compliant"
    assert "8(a)" in body["citations"]
    assert body["policy_hash"] == policy_hash()


def test_audit_returns_the_rules_that_did_not_apply_too(client):
    """ "clause 6(a) did not apply" and "clause 6(a) was never evaluated" have to be
    distinguishable from outside the service, not only from inside it."""
    body = client.post(
        "/audit", json={"mandate_id": "mg_1", "rail": "card", "amount_inr": 499.0}
    ).json()
    assert len(body["outcomes"]) == 20
    assert any(o["applied"] is False for o in body["outcomes"])


def test_audit_abstains_on_an_out_of_scope_rail(client):
    body = client.post(
        "/audit", json={"mandate_id": "mg_1", "rail": "enach", "amount_inr": 499.0}
    ).json()
    assert body["verdict"] == "needs_human"
    assert body["citations"] == ["2"]


def test_explain_returns_the_figures_it_was_checked_against(client):
    """So a caller can re-run the fabrication check rather than trusting that we did."""
    body = client.post(
        "/explain",
        json={
            "facts": {
                "mandate_id": "mg_1",
                "week": 3,
                "kind": "not_worth_asking",
                "channel": "sms",
                "gain_inr": "0.04",
                "backfire_inr": "0.09",
                "fatigue_inr": "0.05",
                "channel_cost_inr": "0.15",
                "net_inr": "-0.25",
            }
        },
    ).json()
    assert "INR" in body["text"]
    assert body["source"] == "deterministic"
    assert "0.29" in body["allowed_amounts"], "the sum the sentence itself prints"


def test_an_outbid_refusal_that_names_no_channel_is_rejected(client):
    """The model validator that keeps the two refusal kinds apart has to survive HTTP."""
    body = {"facts": {"mandate_id": "mg_1", "week": 0, "kind": "outbid", "net_inr": "5"}}
    assert client.post("/explain", json=body).status_code == 422


# --------------------------------------------------------------------------------
# /ledger and /replay.
# --------------------------------------------------------------------------------


@pytest.fixture
def ledger_run(tmp_path, monkeypatch):
    """A tiny ledger the endpoints can read, written where the API looks for it."""
    run_id = "TEST-api-s1-b1.00"
    monkeypatch.setattr("mandateguard.app.api.LEDGER_DIR", tmp_path)
    store = Ledger(tmp_path / f"{run_id}.jsonl")
    for week in range(3):
        for i in range(2):
            store.append(
                build_entry(
                    run_id=run_id,
                    arm="P0",
                    decision=Decision(
                        mandate_id=f"mg_{i}",
                        week=week,
                        kind=DecisionKind.NOT_ASKED,
                        value_inr=0.0,
                        reason="not asked: INR -0.25",
                    ),
                    policy_hash=policy_hash(),
                    model_version="rules-only",
                    seed=1,
                    snapshot_id="test",
                    created_at=date(2026, 9, 2),
                    explanation="Not contacted in week 0.",
                )
            )
    return run_id, store


def test_the_ledger_endpoint_verifies_the_chain_while_serving_it(client, ledger_run):
    """A ledger endpoint that served rows without checking them would be a viewer, and the
    one thing this ledger claims over a log file is that reading it tells you if it moved."""
    run_id, store = ledger_run
    body = client.get(f"/ledger?run_id={run_id}").json()
    assert body["entries"] == 6
    assert body["not_asked"] == 6
    assert body["refusal_share"] == 1.0
    assert body["head"] == store.head
    assert len(body["page"]) == 6


def test_a_tampered_ledger_answers_409_not_200(client, ledger_run):
    run_id, store = ledger_run
    lines = store.path.read_text(encoding="utf-8").splitlines()
    row = json.loads(lines[1])
    row["decision"]["reason"] = "edited"
    lines[1] = json.dumps(row)
    store.path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    response = client.get(f"/ledger?run_id={run_id}")
    assert response.status_code == 409
    assert "line 2" in response.json()["detail"]


def test_an_unknown_run_is_404(client, ledger_run):
    assert client.get("/ledger?run_id=nope").status_code == 404


def test_the_ledger_pages_and_filters(client, ledger_run):
    run_id, _ = ledger_run
    page = client.get(f"/ledger?run_id={run_id}&offset=2&limit=2").json()["page"]
    assert len(page) == 2
    assert client.get(f"/ledger?run_id={run_id}&kind=asked").json()["page"] == []


def test_replaying_an_unknown_decision_is_409_not_500(client, ledger_run):
    """ "this cannot be replayed" is an answer, not a server fault."""
    run_id, _ = ledger_run
    response = client.get(f"/replay/{run_id}:missing:w0")
    assert response.status_code == 409
    assert "wearing an old name" in response.json()["detail"]


def test_replaying_from_an_unknown_run_is_404(client):
    assert client.get("/replay/nosuchrun:mg_1:w0").status_code == 404


def test_the_ledger_directory_default_points_inside_the_repo():
    """Guards the monkeypatch above: if the default moved, these tests would pass against
    a directory the running service never reads."""
    assert LEDGER_DIR.name == "ledger"
    assert LEDGER_DIR.parent.name == "data"
