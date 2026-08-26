"""SPIKE S3 — can uvicorn and Streamlit run together and talk to each other?

The video demo depends on both processes being up at once (docs/stack.md, spike S3).
Finding out on demo day that they do not co-exist on Windows is the failure this guards
against, so the check is a test rather than a one-off manual run.

Two levels, deliberately:
  - the API contract, in-process and fast (runs everywhere, including CI)
  - the real two-process boot via scripts/dev.py (slow; opt in with RUN_S3=1)
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from mandateguard.app.api import app

ROOT = Path(__file__).resolve().parents[2]


def test_api_health_contract():
    """The UI reads these exact keys; if they move, the Streamlit surface breaks."""
    resp = TestClient(app).get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert "theta_placeholder" in body


@pytest.mark.skipif(
    os.environ.get("RUN_S3") != "1",
    reason="slow two-process boot; set RUN_S3=1 to run",
)
def test_both_processes_boot_and_talk():
    """Start uvicorn and Streamlit via scripts/dev.py, hit both ports, then stop."""
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "dev.py")],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        api_ok = ui_ok = False
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline and not (api_ok and ui_ok):
            if proc.poll() is not None:
                pytest.fail(f"dev.py exited early:\n{proc.stdout.read() if proc.stdout else ''}")
            try:
                if not api_ok:
                    api_ok = httpx.get("http://127.0.0.1:8000/health", timeout=1).status_code == 200
                if api_ok and not ui_ok:
                    ui_ok = httpx.get("http://127.0.0.1:8501", timeout=2).status_code == 200
            except httpx.HTTPError:
                time.sleep(0.5)

        assert api_ok, "FastAPI never answered on :8000"
        assert ui_ok, "Streamlit never answered on :8501"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
