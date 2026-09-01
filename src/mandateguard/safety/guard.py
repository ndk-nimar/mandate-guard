"""T5.3 -- the layer that stands between a decision and an action.

Everything else in this repository decides. This decides whether the decision is allowed to
*happen*, and it is the only path to acting: there is no `send()` anywhere that does not go
through `Guard.authorise` first. That is what makes the spend cap a cap rather than a
convention -- a limit checked in three call sites is a limit missing from the fourth.

### Shadow by default, and why the default is the design

`safety.mode` is `shadow` in `config/params.yaml`. In shadow mode `authorise` returns a
verdict that is *recorded and not acted on*: the system proposes, the ledger fills, nothing
reaches a customer. Going live is a deliberate edit by someone who can be named.

The inverted default -- live unless configured otherwise -- is the same code with one word
changed, and it ships live by accident exactly once.

### The degradation ladder

Four states, strictly ordered, worst wins:

* `NORMAL` -- nothing wrong; everything runs.
* `RULES_ONLY` -- the model is unavailable. Allocation, audit and notices still run, without
  it. `agent/` already degrades this way, so this rung *records* the state rather than
  creating the behaviour.
* `CONSERVATIVE_FLOOR` -- the hazard model is stale. No asks at all: the P0 floor.
* `HALTED` -- the kill switch is on, or the policy hash does not match. Nothing runs.

`RULES_ONLY` is not a fallback bolted on here. `MandateAuditor(client=None)`,
`TemplateComposer` and `RefusalExplainer()` all run without a model already, because that
was the ordinary path in Phase 4 rather than an emergency one. This class names the state so
a ledger entry and an alert can carry it.

The stale-model rung is the counter-intuitive one. The instinct is that an old model is
better than no model. It is not: an old model still outputs a confident hazard, the
allocator still spends real money on it, and nobody sees the staleness in the output. Not
asking is the only action whose cost is bounded when the input is untrustworthy.

### What this is not

It is not a distributed limiter. Counters are in-process, so two workers each get the whole
allowance. Making that right needs shared state this project does not have, and the honest
version is to say so rather than to ship a limiter that looks global and is not
(`docs/limitations.md`).

It is not authorisation, authentication, or an audit of who flipped the mode. The kill
switch is a file anyone with write access can create, which is the point at 02:00 and a
weakness at every other hour.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import date
from enum import IntEnum, StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from mandateguard.policy.loader import ROOT, SafetyParams, policy_hash

__all__ = [
    "Action",
    "ActionKind",
    "Authorisation",
    "Degradation",
    "Guard",
    "Halted",
]


class ActionKind(StrEnum):
    """What is being asked for. Different kinds hit different limits."""

    CONTACT = "contact"
    """An outbound message to a customer. Costs rupees, consumes the rate limit."""

    MODEL_CALL = "model_call"
    """A request to the LLM. Costs USD, and is refused in RULES_ONLY rather than retried."""


class Degradation(IntEnum):
    """The ladder. `IntEnum` so `max()` is the combination rule and cannot be got wrong.

    Ordering by severity rather than alphabetically is load-bearing: two independent
    problems must resolve to the worse one, and `max(RULES_ONLY, HALTED)` being `HALTED`
    is a property of the type rather than of a comparison somebody wrote by hand.
    """

    NORMAL = 0
    RULES_ONLY = 1
    CONSERVATIVE_FLOOR = 2
    HALTED = 3


class Halted(RuntimeError):
    """The system is stopped. Raised only by `require_running`, never by `authorise`.

    `authorise` returns a refusal instead, because a refusal is a record and an exception
    is a stack trace: the ledger has to be able to say "this contact was not made because
    the kill switch was on", and that is a row, not a crash.
    """


class Action(BaseModel):
    model_config = ConfigDict(frozen=True)

    kind: ActionKind
    mandate_id: str = ""
    cost_inr: float = Field(default=0.0, ge=0)
    cost_usd: float = Field(default=0.0, ge=0)


class Authorisation(BaseModel):
    """The answer, and it is always a record rather than sometimes an exception."""

    model_config = ConfigDict(frozen=True)

    allowed: bool
    shadow: bool = Field(
        default=False, description="the action was permitted but must not be performed"
    )
    state: Degradation = Degradation.NORMAL
    reason: str = Field(min_length=1)

    @property
    def acted(self) -> bool:
        """True only when the caller should actually do the thing.

        Separate from `allowed` on purpose: in shadow mode an action is allowed -- it
        passed every limit and would have been correct -- and must still not happen. A
        caller that branches on `allowed` alone sends in shadow mode, and the whole point
        of shadow mode is that this cannot be a one-word mistake.
        """
        return self.allowed and not self.shadow


class Guard:
    """The single gate. Construct once per run; ask before every action."""

    def __init__(
        self,
        params: SafetyParams,
        *,
        expected_policy_hash: str | None = None,
        model_trained_on: date | None = None,
        today: date | None = None,
        clock: Callable[[], float] | None = None,
        kill_switch_file: Path | None = None,
    ) -> None:
        import time

        self.params = params
        self.expected_policy_hash = expected_policy_hash
        self.model_trained_on = model_trained_on
        self.today = today or date.today()
        self._clock = clock or time.monotonic
        self.kill_switch_file = (
            ROOT / params.kill_switch_file if kill_switch_file is None else kill_switch_file
        )

        self.spent_inr = 0.0
        self.spent_usd = 0.0
        self.contacts = 0
        self._tripped: str | None = None
        self._llm_down = False
        self._window: deque[float] = deque()

    # ---------------------------------------------------------------- state

    def trip(self, reason: str) -> None:
        """Stop the system from inside the process. Irreversible for this Guard's life.

        No `untrip`. Resuming after a kill is a decision a person makes by starting the
        system again, having looked at why it stopped -- not by calling a method that
        happens to be next to the one that stopped it.
        """
        self._tripped = reason

    def mark_llm_unavailable(self, reason: str = "the model layer reported unavailable") -> None:
        self._llm_down = True
        self._llm_reason = reason

    def kill_switch_present(self) -> bool:
        return self.kill_switch_file.is_file()

    @property
    def model_age_days(self) -> int | None:
        if self.model_trained_on is None:
            return None
        return (self.today - self.model_trained_on).days

    def state(self) -> tuple[Degradation, str]:
        """The current rung and why. Recomputed on every call rather than cached.

        Cached, a kill switch created after the Guard was built would be invisible for the
        life of the run -- and "I touched the file and it kept sending" is the failure this
        whole class exists to prevent.
        """
        reasons: list[tuple[Degradation, str]] = [(Degradation.NORMAL, "no degradation")]

        if self._tripped is not None:
            reasons.append((Degradation.HALTED, f"kill switch tripped: {self._tripped}"))
        if self.kill_switch_present():
            reasons.append(
                (Degradation.HALTED, f"kill switch file present at {self.kill_switch_file}")
            )
        if self.expected_policy_hash is not None:
            current = policy_hash()
            if current != self.expected_policy_hash:
                reasons.append(
                    (
                        Degradation.HALTED,
                        f"policy hash mismatch: running under {current}, expected "
                        f"{self.expected_policy_hash}. The rulebook changed under a running "
                        "system; halting is the only action that cannot be wrong.",
                    )
                )
        age = self.model_age_days
        if age is not None and age > self.params.max_model_age_days:
            reasons.append(
                (
                    Degradation.CONSERVATIVE_FLOOR,
                    f"hazard model is {age} days old (limit {self.params.max_model_age_days}). "
                    "A stale model still outputs a confident hazard and the allocator still "
                    "spends real money on it, so the floor is the bounded action.",
                )
            )
        if self._llm_down:
            reasons.append((Degradation.RULES_ONLY, getattr(self, "_llm_reason", "model down")))

        worst = max(reasons, key=lambda item: item[0])
        return worst

    # ---------------------------------------------------------------- the gate

    def authorise(self, action: Action) -> Authorisation:
        """The only way to act. Checks the ladder, then the caps, then shadow mode.

        Order matters: a halted system must not consume rate-limit slots deciding it is
        halted, and a capped run must not have its shadow status decide whether the cap
        applied. Shadow is applied last precisely so that **every limit is exercised in
        shadow mode too** -- a shadow run that skipped the caps would tell you nothing
        about whether the live run would stay inside them.
        """
        state, why = self.state()

        if state is Degradation.HALTED:
            return Authorisation(allowed=False, state=state, reason=why)

        if state is Degradation.CONSERVATIVE_FLOOR and action.kind is ActionKind.CONTACT:
            return Authorisation(allowed=False, state=state, reason=why)

        if state is Degradation.RULES_ONLY and action.kind is ActionKind.MODEL_CALL:
            return Authorisation(
                allowed=False,
                state=state,
                reason=f"{why}; running rules-only, so no model call is made",
            )

        if action.kind is ActionKind.CONTACT:
            refusal = self._check_contact(action, state)
            if refusal is not None:
                return refusal

        shadow = self.params.mode == "shadow"
        self._record(action, shadow=shadow)
        return Authorisation(
            allowed=True,
            shadow=shadow,
            state=state,
            reason=(
                "shadow mode: authorised and deliberately not performed"
                if shadow
                else f"authorised ({self.contacts} contacts, INR {self.spent_inr:.2f} spent)"
            ),
        )

    def _check_contact(self, action: Action, state: Degradation) -> Authorisation | None:
        if self.spent_inr + action.cost_inr > self.params.max_spend_inr_per_run + 1e-9:
            return Authorisation(
                allowed=False,
                state=state,
                reason=(
                    f"spend cap: INR {self.spent_inr:.2f} spent, this contact costs "
                    f"INR {action.cost_inr:.2f}, cap is INR "
                    f"{self.params.max_spend_inr_per_run:.2f}. Refused before spending, not "
                    "after."
                ),
            )
        if not self._rate_limit_has_room():
            return Authorisation(
                allowed=False,
                state=state,
                reason=(
                    f"rate limit: {self.params.max_sends_per_window} contacts already sent in "
                    f"the last {self.params.window_seconds}s. The budget stops the system "
                    "spending too much; this stops it spending correctly but too fast."
                ),
            )
        return None

    def _rate_limit_has_room(self) -> bool:
        now = self._clock()
        cutoff = now - self.params.window_seconds
        while self._window and self._window[0] <= cutoff:
            self._window.popleft()
        return len(self._window) < self.params.max_sends_per_window

    def _record(self, action: Action, *, shadow: bool) -> None:
        """Count the action, in shadow mode too.

        Counting a shadow action is the whole reason a shadow run is informative: it is a
        dry run of the limits, and a dry run that did not consume the allowance would
        report that the live run fits when it does not.
        """
        self.spent_inr += action.cost_inr
        self.spent_usd += action.cost_usd
        if action.kind is ActionKind.CONTACT:
            self.contacts += 1
            self._window.append(self._clock())

    def require_running(self) -> None:
        """Raise if the system is halted. For loops that should stop, not record.

        `authorise` returns a refusal so the ledger can hold it. This exists for the outer
        loop, where continuing to iterate over 16,000 mandates producing 16,000 identical
        "halted" rows is not an audit trail, it is a log flood.
        """
        state, why = self.state()
        if state is Degradation.HALTED:
            raise Halted(why)
