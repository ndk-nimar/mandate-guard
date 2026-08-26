"""Start the FastAPI service and the Streamlit surface together.

Spike S3: proving these two run from one command on Windows. Ctrl-C stops both.

    uv run python scripts/dev.py
"""

from __future__ import annotations

import atexit
import contextlib
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
API_HOST, API_PORT = "127.0.0.1", 8000
UI_PORT = 8501
HEALTH_URL = f"http://{API_HOST}:{API_PORT}/health"

procs: list[subprocess.Popen] = []


def _shutdown() -> None:
    for p in procs:
        if p.poll() is None:
            p.terminate()
    for p in procs:
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()


def wait_for_api(timeout: float = 30.0) -> bool:
    """Poll /health until the service answers. Returns False if it never does."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if procs and procs[0].poll() is not None:
            return False  # uvicorn died; no point waiting out the timeout
        try:
            if httpx.get(HEALTH_URL, timeout=1.0).status_code == 200:
                return True
        except httpx.HTTPError:
            time.sleep(0.4)
    return False


def main() -> int:
    atexit.register(_shutdown)

    procs.append(
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "mandateguard.app.api:app",
                "--host",
                API_HOST,
                "--port",
                str(API_PORT),
            ],
            cwd=ROOT,
        )
    )

    if not wait_for_api():
        print(f"FAIL: API never became healthy at {HEALTH_URL}", file=sys.stderr)
        return 1
    print(f"API up at {HEALTH_URL}")

    procs.append(
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(ROOT / "src" / "mandateguard" / "app" / "ui.py"),
                "--server.port",
                str(UI_PORT),
                "--server.headless",
                "true",
            ],
            cwd=ROOT,
        )
    )
    print(f"UI up at http://localhost:{UI_PORT}")

    with contextlib.suppress(KeyboardInterrupt):
        procs[1].wait()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
