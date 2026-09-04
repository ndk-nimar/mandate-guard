"""The T0.5 two-process spike, kept deliberately small.

Its job is spike S3's question -- can uvicorn and Streamlit start together from one command
on Windows and talk over httpx -- and `scripts/dev.py` plus
`tests/spikes/test_two_process.py` still depend on it answering that. It is **not** the
product surface: T5.6 ships a hand-written page served by FastAPI itself, at
`http://127.0.0.1:8000/`.

It never imports the allocator, so the API boundary stays real rather than decorative. The
same is true of the T5.6 page, which crosses that boundary over `fetch`.

**What was removed on 2026-09-04.** This file used to render `/health`'s `theta_placeholder`
as `st.metric("Shadow price theta", ...)`. That field is a hardcoded `0`, and displaying it
under that label put a number with no origin on screen wearing the name of this project's
headline commercial figure -- which is the exact thing `docs/calibration.md` exists to
prevent. The real shadow price is published in `docs/eval.md` §4. The `/health` key itself
is left alone: `test_two_process.py` pins it as a contract, and renaming an API field is a
separate change with its own argument.
"""

import httpx
import streamlit as st

API_BASE = "http://127.0.0.1:8000"

st.title("MandateGuard")
st.caption("Spike surface. The real page is served by FastAPI at " + API_BASE + "/")

try:
    payload = httpx.get(f"{API_BASE}/health", timeout=5.0).json()
    st.success(f"API reachable: {payload['status']}")
    st.metric("Degradation rung", payload["degradation"])
    st.metric("Rules compiled", payload["rules"])
    st.caption(f"policy {payload['policy_hash']} - mode {payload['mode']}")
except Exception as exc:  # noqa: BLE001 - surface any wiring failure to the operator
    st.error(f"API unreachable at {API_BASE}: {exc}")
