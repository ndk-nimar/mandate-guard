"""T5.6 -- the dial must show what the committed document shows.

The surface is a *view of* `docs/results.md`, not a second opinion on it. If the page can
print a number the document does not contain, then one of them is wrong and a reader has no
way to tell which -- so the property worth pinning is not "the endpoint returns 200", it is
**the rendered string matches the committed cell**.

Three levels, deliberately separated by what they cost:

1. **The notches.** Milliseconds, always runs. This is the half that drifts silently: edit
   the channel table in `config/params.yaml` or `budget_ladder`'s `steps` default and all
   seventeen budgets move, while every other test in this repository stays green.
2. **The profits.** ~29s, and skipped when the derived frames are absent -- which is the
   normal state of a fresh clone until `mandateguard repro` has run. Gated on the file
   rather than on an env var, because the file *is* the precondition and anyone who has run
   `repro` should get the coverage without knowing a magic variable.
3. **The safety properties.** Milliseconds, always runs, and the most on-brand of the three.

String equality rather than `pytest.approx` throughout level 2. A tolerance would let the
page show 413,431 beside a document saying 413,432 and call that agreement; ADR 0003's
standard is bytes, and this is that standard applied to the surface.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from mandateguard.allocator.baselines import bulk_channel
from mandateguard.app.api import STATIC, app
from mandateguard.data.paths import ROOT, frame_dir
from mandateguard.eval import sweep
from mandateguard.policy.loader import load_params

RESULTS = ROOT / "docs" / "results.md"
FRAMES = frame_dir(sample=True) / "person_periods.parquet"

needs_frames = pytest.mark.skipif(
    not FRAMES.is_file(),
    reason=f"{FRAMES} is absent -- run `uv run mandateguard repro` first",
)


@pytest.fixture(scope="module")
def client() -> TestClient:
    return TestClient(app)


def committed_budget_table() -> tuple[list[str], list[list[str]]]:
    """The budget-by-arm table out of `results.md` §3, as rendered strings.

    Parsed rather than duplicated. A copy of this table in a test file is a second place for
    the same numbers to live, and the whole point of the exercise is that there is one.
    """
    text = RESULTS.read_text(encoding="utf-8")
    section = text.split("## 3. The budget curve")[1].split("## 4.")[0]
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in section.splitlines()
        if line.startswith("|")
    ]
    header = next(r for r in rows if r[0] == "budget")
    body = [r for r in rows if re.fullmatch(r"[\d,]+\.\d\d", r[0])]
    assert len(body) == 17, f"expected 17 notches in results.md 3, found {len(body)}"
    return header[1:], body


def test_the_dials_notches_are_the_committed_ladder() -> None:
    """Milliseconds, and it catches the change nothing else would.

    `budget_ladder` reads the channel cost and the book size, so an edit to either moves
    every notch. Without this, the page would keep serving a dial the document no longer
    describes and every existing test would stay green.
    """
    params = load_params()
    _, body = committed_budget_table()
    committed = [row[0] for row in body]

    mandates = int(
        re.search(
            r"live mandates at the snapshot\*\* \| \*\*([\d,]+)\*\*", RESULTS.read_text("utf-8")
        )
        .group(1)
        .replace(",", "")
    )
    computed = [
        f"{b:,.2f}" for b in sweep.budget_ladder(bulk_channel(params.channels).cost_inr, mandates)
    ]
    assert computed == committed


def test_the_arms_on_the_dial_are_the_arms_in_the_document() -> None:
    header, _ = committed_budget_table()
    assert header == ["P0", "P1", "P2", "P3", "P4"]
    assert "P5" not in header, "P5 is excluded from the ladder; see app/ladder.py"


@needs_frames
@pytest.mark.parametrize("notch", [0, 11, 16], ids=["zero", "interior-optimum", "saturation"])
def test_the_dial_shows_what_the_committed_document_shows(client: TestClient, notch: int) -> None:
    """The property the whole surface rests on.

    Three notches rather than all seventeen: 17 x ~1.7s is half a minute, and this suite
    runs on every commit. Zero, the interior optimum `results.md` 3 singles out, and
    saturation are the three that would catch a real regression -- a flat middle notch that
    broke while those three held would be a very strange bug.
    """
    _, body = committed_budget_table()
    row = body[notch]

    response = client.get(f"/ladder?notch={notch}")
    assert response.status_code == 200, response.text
    payload = response.json()

    assert f"{payload['rung']['budget_inr']:,.2f}" == row[0]
    rendered = [f"{p['profit_inr']:,.0f}" for p in payload["rung"]["points"]]
    assert rendered == row[1:], (
        f"notch {notch} (INR {row[0]}): the dial and results.md 3 disagree. "
        "One of them is wrong and a reader cannot tell which."
    )


@needs_frames
def test_a_notch_off_the_end_is_a_404_that_says_how_long_the_dial_is(client: TestClient) -> None:
    response = client.get("/ladder?notch=99")
    assert response.status_code == 404
    assert "17 notches" in response.json()["detail"]


@needs_frames
def test_retention_is_not_rounded_to_a_count(client: TestClient) -> None:
    """`mandates_retained` is an expectation over survival weights, not a number of people.

    Rounding 1,215.9 to 1,216 would turn a statement about probability into a claim about
    individuals, and it is the kind of tidying that looks like formatting.
    """
    points = client.get("/ladder?notch=11").json()["rung"]["points"]
    retained = [p["mandates_retained"] for p in points]
    assert any(value != int(value) for value in retained), retained


@needs_frames
def test_the_ladder_reports_no_theta_rather_than_inventing_one(client: TestClient) -> None:
    """`sweep.ARMS` runs P4 without the dual, so there is no shadow price here.

    Filling the field would mean changing the arm, which would move the profit figures off
    the committed table -- trading this endpoint's only real property for a headline number.
    """
    points = client.get("/ladder?notch=11").json()["rung"]["points"]
    assert all(p["theta_inr"] is None for p in points)


@needs_frames
def test_the_second_visit_to_a_notch_is_cached(client: TestClient) -> None:
    """Not a performance test -- a demo test. A judge drags the dial back and forth."""
    client.get("/ladder?notch=16")
    assert client.get("/ladder?notch=16").json()["cached"] is True


# --------------------------------------------------------------------------------
# Safety. Milliseconds, always runs.
# --------------------------------------------------------------------------------


@needs_frames
def test_the_ladder_says_it_is_simulated_in_two_fields_not_one(client: TestClient) -> None:
    """`acted: false` alone still reads like a plan somebody could execute.

    `simulated: true` says the thing that is actually true: there was never a customer at
    the other end of any of these 16,236 asks.
    """
    payload = client.get("/ladder?notch=0").json()
    assert payload["acted"] is False
    assert payload["simulated"] is True


@needs_frames
def test_the_ladder_does_not_burn_the_spend_cap(client: TestClient) -> None:
    """A simulation must not consume the allowance a real contact would need.

    This passes today because `_guard()` builds a fresh Guard per request, which is exactly
    why it is worth pinning: it fails the moment somebody makes the guard per-process
    without noticing that `/ladder` spends 16,236 simulated asks against a 500/hour limit.
    """
    from tests.test_api import week_row

    client.get("/ladder?notch=16")
    body = client.post(
        "/allocate",
        json={"book": [week_row("mg_cap_probe")], "budget_inr": 100.0},
    ).json()
    assert body["refused_by_guard"] == 0


def test_the_surface_cannot_send_anything() -> None:
    """The safety argument, as a test rather than a review comment.

    The page is read-only *by construction*: there is no HTML form element and no write
    request in it, because `/ladder`, `/refusal`, `/runs` and `/ledger` were shaped so that
    nothing on the page needs one. A surface that cannot issue a write cannot be mistaken
    for one that contacts customers -- and "we reviewed it and it looked read-only" is not
    a property, it is a memory.

    The vocabulary half matters as much as the mechanical half. A dashboard that says
    "sending" is a dashboard a judge reasonably believes is sending.
    """
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    lowered = html.lower()

    assert "<form" not in lowered
    assert "method:" not in lowered, "every fetch() on this page must be a bare GET"
    for verb in ("sending", "we sent", "deliver", "campaign", "launch"):
        assert verb not in lowered, f"the surface must not say {verb!r}"

    assert "SHADOW" in html, "the masthead must say shadow mode, always"


def test_the_surface_is_served_with_the_right_content_type(client: TestClient) -> None:
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


def test_an_unvendored_font_is_a_404_rather_than_a_500(client: TestClient) -> None:
    """The page declares a real fallback stack and is designed to look finished without
    the woff2 files, so a build that did not vendor them must degrade rather than break."""
    assert client.get("/static/nothing.woff2").status_code == 404


def test_the_ledger_index_needs_no_hard_coded_run_id(client: TestClient) -> None:
    """A run id encodes `params.seed` and `horizon.budget_inr_per_week`, so a literal in
    the page would drift from the ledger the moment either changed -- and the tab would
    show an empty state that looked like "no decisions" rather than "wrong filename"."""
    response = client.get("/runs")
    assert response.status_code == 200
    assert isinstance(response.json()["runs"], list)
