"""The one place this repository talks to a model, and the cassettes that let CI not.

Phase 4's stated goal is "an LLM layer that is a shipped system, not an API call". Three
things separate the two, and all three live here.

**Determinism.** [ADR 0003](../../../docs/adr/0003-determinism-of-derived-data.md) says
every derived file is byte-identical across runs. A model call is not byte-identical
across runs, so either Phase 4 is exempt from the project's central rule or the model call
has to be *recorded*. It is recorded. A run in `record` mode calls the API and writes the
exact response to `tests/cassettes/`; every run after that -- including every CI run, on a
machine with no API key -- replays it. `docs/llm_eval.md` is therefore regenerable by a
stranger who clones the repo, which is what GATE 5 asks for.

**A cassette miss is a failure, not a fallback.** The tempting design is: replay if we can,
call the API if we cannot. That design silently spends money in CI, and worse, silently
produces a *different* `llm_eval.md` on a machine that happens to have a key. So a miss
raises. The only way to get a new cassette is to ask for one, in `record` mode, on purpose.

**Cost is measured, not estimated.** Every call returns its token counts and the USD they
cost at the rates in `config/params.yaml`. T4.7 reports cost per verdict from these, and
`spend_cap_usd_per_run` stops a run that goes wrong -- the first rung of T5.3's spend cap.

What this module does **not** do is retry a refused or malformed response into looking
fine. Validation belongs to the caller (`agent/auditor.py`, `agent/linter.py`), because
the caller knows what a good answer looks like and this module does not.

### The honest caveat about replayed latency

`LLMResult.latency_ms` in replay is the latency that was *recorded*, not the latency of the
replay. That keeps `llm_eval.md` byte-identical, and it means the p95 latency in that
document is a measurement from the recording session on one machine and one network -- it
is labelled as such wherever it is printed. A p95 computed over cassette reads would be a
measurement of local disk, which would be worse: precise, reproducible and meaningless.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from mandateguard.policy.loader import ROOT, LLMParams

CASSETTE_DIR = ROOT / "tests" / "cassettes"
MODE_ENV = "MANDATEGUARD_LLM_MODE"

__all__ = [
    "CASSETTE_DIR",
    "AnthropicClient",
    "CassetteClient",
    "CassetteMissError",
    "LLMClient",
    "LLMResult",
    "LLMUsage",
    "RecordingClient",
    "SpendCapExceeded",
    "build_client",
    "cassette_key",
]


class CassetteMissError(RuntimeError):
    """No recording exists for this exact request.

    Carries the key and the job so the message can say what to run to fix it. Deliberately
    not a subclass of anything the callers catch: a miss must surface, not degrade.
    """


class SpendCapExceeded(RuntimeError):
    """The run has spent its budget. Raised before the call that would exceed it."""


class LLMUsage(BaseModel):
    """Token counts and what they cost, in USD. See `LLMParams` for why not rupees."""

    model_config = ConfigDict(frozen=True)

    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    cache_read_input_tokens: int = Field(default=0, ge=0)
    cache_creation_input_tokens: int = Field(default=0, ge=0)

    def cost_usd(self, prices: LLMParams) -> float:
        return (
            self.input_tokens * prices.price_input_usd_per_mtok
            + self.output_tokens * prices.price_output_usd_per_mtok
            + self.cache_read_input_tokens * prices.price_cache_read_usd_per_mtok
            + self.cache_creation_input_tokens * prices.price_cache_write_usd_per_mtok
        ) / 1_000_000


class LLMResult(BaseModel):
    """One model response, plus everything needed to audit or replay it."""

    model_config = ConfigDict(frozen=True)

    job: str
    key: str = Field(description="cassette key; also the request's identity for replay")
    model: str
    text: str
    stop_reason: str | None = None
    usage: LLMUsage
    latency_ms: int = Field(ge=0)
    replayed: bool = Field(
        default=False, description="True when this came from a cassette rather than the API"
    )

    @property
    def refused(self) -> bool:
        """`stop_reason == "refusal"` means the model declined; `text` is not an answer.

        Checked by callers before parsing. A refusal that gets JSON-parsed produces a
        confusing error three layers away from its cause.
        """
        return self.stop_reason == "refusal"


class LLMClient(Protocol):
    """What every layer above Phase 4 is allowed to know about the model."""

    def run(
        self,
        job: str,
        system: str,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
    ) -> LLMResult: ...


# --------------------------------------------------------------------------------
# Request identity.
# --------------------------------------------------------------------------------


def cassette_key(
    job: str,
    model: str,
    system: str,
    prompt: str,
    schema: Mapping[str, Any] | None,
    max_tokens: int,
    effort: str,
) -> str:
    """A stable 16-hex identity for a request.

    `sort_keys=True` is not tidiness. Python dict order is insertion order, and a schema
    assembled in a different order on a different code path would hash differently and miss
    a cassette that holds the identical request -- the same class of bug as the unordered
    `COPY` that ADR 0003 exists to prevent.
    """
    payload = json.dumps(
        {
            "job": job,
            "model": model,
            "system": system,
            "prompt": prompt,
            "schema": schema,
            "max_tokens": max_tokens,
            "effort": effort,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _cassette_path(job: str, key: str, root: Path = CASSETTE_DIR) -> Path:
    return root / job / f"{key}.json"


# --------------------------------------------------------------------------------
# Clients.
# --------------------------------------------------------------------------------


class AnthropicClient:
    """The live client. Costs money on every call.

    Adaptive thinking is on and `effort` comes from `config/params.yaml`, so a cheaper or
    more thorough run is a config edit rather than a code edit. The system prompt is marked
    cacheable because it is the large, stable half of every Phase 4 request -- the circular
    text for the compiler, the rulebook for the auditor -- and it is identical across the
    120 golden cases.
    """

    def __init__(self, params: LLMParams, *, spend_cap_usd: float | None = None) -> None:
        import anthropic  # imported here so that a cassette-only run needs no SDK at import time

        self._client = anthropic.Anthropic()
        self.params = params
        self.spend_cap_usd = (
            params.spend_cap_usd_per_run if spend_cap_usd is None else spend_cap_usd
        )
        self.spent_usd = 0.0

    def run(
        self,
        job: str,
        system: str,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
    ) -> LLMResult:
        if self.spent_usd >= self.spend_cap_usd:
            raise SpendCapExceeded(
                f"this run has spent {self.spent_usd:.4f} USD against a cap of "
                f"{self.spend_cap_usd:.2f} (llm.spend_cap_usd_per_run). Refusing the {job!r} "
                "call. Raise the cap in config/params.yaml if the run is genuinely this big."
            )

        output_config: dict[str, Any] = {"effort": self.params.effort}
        if schema is not None:
            output_config["format"] = {"type": "json_schema", "schema": dict(schema)}

        started = time.perf_counter()
        message = self._client.messages.create(
            model=self.params.model,
            max_tokens=self.params.max_tokens,
            system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
            thinking={"type": "adaptive"},
            output_config=output_config,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)

        text = "".join(block.text for block in message.content if block.type == "text")
        usage = LLMUsage(
            input_tokens=message.usage.input_tokens,
            output_tokens=message.usage.output_tokens,
            cache_read_input_tokens=getattr(message.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation_input_tokens=getattr(message.usage, "cache_creation_input_tokens", 0)
            or 0,
        )
        self.spent_usd += usage.cost_usd(self.params)

        return LLMResult(
            job=job,
            key=cassette_key(
                job,
                self.params.model,
                system,
                prompt,
                schema,
                self.params.max_tokens,
                self.params.effort,
            ),
            model=message.model,
            text=text,
            stop_reason=message.stop_reason,
            usage=usage,
            latency_ms=latency_ms,
        )


class CassetteClient:
    """Replays recorded responses. The only client CI ever constructs.

    Needs neither an API key nor the `anthropic` package: a fork with no secrets can still
    regenerate every Phase 4 document, which is the difference between a demo and a result.
    """

    def __init__(self, params: LLMParams, *, root: Path = CASSETTE_DIR) -> None:
        self.params = params
        self.root = root
        self.spent_usd = 0.0

    def run(
        self,
        job: str,
        system: str,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
    ) -> LLMResult:
        key = cassette_key(
            job,
            self.params.model,
            system,
            prompt,
            schema,
            self.params.max_tokens,
            self.params.effort,
        )
        path = _cassette_path(job, key, self.root)
        if not path.is_file():
            raise CassetteMissError(
                f"no cassette for job {job!r} key {key} at {path}. Nothing was called and "
                "nothing was guessed. To record it: set "
                f"{MODE_ENV}=record with ANTHROPIC_API_KEY set, run the script that makes "
                "this request, and commit the new file. A prompt edit changes the key, so "
                "a miss right after editing a prompt is the expected outcome, not a bug."
            )
        recorded = json.loads(path.read_text(encoding="utf-8"))
        result = LLMResult.model_validate(recorded["response"] | {"job": job, "key": key})
        self.spent_usd += result.usage.cost_usd(self.params)
        return result.model_copy(update={"replayed": True})


class RecordingClient:
    """Calls the API and writes what came back. Used by hand, never by CI.

    Writes with `sort_keys=True` and a trailing newline so that re-recording an unchanged
    response produces a zero-line diff -- the reviewer sees only what actually changed.
    """

    def __init__(self, inner: AnthropicClient, *, root: Path = CASSETTE_DIR) -> None:
        self.inner = inner
        self.root = root

    @property
    def spent_usd(self) -> float:
        return self.inner.spent_usd

    def run(
        self,
        job: str,
        system: str,
        prompt: str,
        *,
        schema: Mapping[str, Any] | None = None,
    ) -> LLMResult:
        result = self.inner.run(job, system, prompt, schema=schema)
        path = _cassette_path(job, result.key, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "key": result.key,
            "job": job,
            "recorded_on": datetime.now(UTC).date().isoformat(),
            "request": {
                "model": self.inner.params.model,
                "max_tokens": self.inner.params.max_tokens,
                "effort": self.inner.params.effort,
                "system": system,
                "prompt": prompt,
                "schema": dict(schema) if schema is not None else None,
            },
            "response": result.model_dump(exclude={"job", "key", "replayed"}),
        }
        path.write_text(
            json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return result


def build_client(params: LLMParams, *, mode: str | None = None) -> LLMClient:
    """Pick a client from `MANDATEGUARD_LLM_MODE`, defaulting to replay.

    Replay is the default because the wrong default here costs money and breaks
    reproducibility at the same time. Choosing `live` or `record` is always deliberate.
    """
    resolved = (mode or os.environ.get(MODE_ENV) or "cassette").strip().lower()
    if resolved == "cassette":
        return CassetteClient(params)
    if resolved == "live":
        return AnthropicClient(params)
    if resolved == "record":
        return RecordingClient(AnthropicClient(params))
    raise ValueError(
        f"{MODE_ENV}={resolved!r} is not a mode. Use 'cassette' (replay, the default), "
        "'live' (call the API, record nothing) or 'record' (call the API and write the "
        "cassette)."
    )
