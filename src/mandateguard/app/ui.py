"""Streamlit surface. Talks to the FastAPI service over httpx -- never imports the
allocator directly, so the API boundary stays real rather than decorative."""

import httpx
import streamlit as st

API_BASE = "http://127.0.0.1:8000"

st.title("MandateGuard")

try:
    payload = httpx.get(f"{API_BASE}/health", timeout=5.0).json()
    st.success(f"API reachable: {payload['status']}")
    st.metric("Shadow price theta", f"Rs {payload['theta_placeholder']}")
except Exception as exc:  # noqa: BLE001 - surface any wiring failure to the operator
    st.error(f"API unreachable at {API_BASE}: {exc}")
