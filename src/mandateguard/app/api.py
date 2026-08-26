"""FastAPI service. Endpoints land here across T5.5; for now it carries the health
check that spike S3 uses to prove the two-process demo works on Windows."""

from fastapi import FastAPI

app = FastAPI(title="MandateGuard", version="0.1.0")


@app.get("/health")
def health() -> dict[str, str | int]:
    """Liveness probe, and the number the Streamlit surface fetches in spike S3."""
    return {"status": "ok", "service": "mandateguard", "theta_placeholder": 0}
