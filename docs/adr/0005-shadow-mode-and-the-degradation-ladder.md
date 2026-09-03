# ADR 0005 — Shadow mode is the default, and degradation is a ladder rather than a fallback

Date: 2026-09-02
Status: **Accepted** (T5.3 / T5.4 shipped on it; recorded 2026-09-03 in T5.7)

## Context

This system proposes contacting real customers about a live payment mandate, on channels
that cost money, under a regulator that treats over-contact as a consumer-protection
matter. The failure that matters is not "the allocator picked a suboptimal channel". It is
**a system that acts when it should not have**: a bug that sends, a stale model that keeps
scoring, a policy file edited on Friday and a run on Monday, an LLM outage that turns a
compliance check into an exception nobody catches.

Each of those has an obvious local fix. The problem with the obvious local fixes is that
they are spread across the call sites that do the acting, and there is always one more call
site — the HTTP layer in T5.5 was the fourth one, written after the first three were
already guarded.

## Decision

**One authorisation call, one default, one ordered ladder.**

**`Guard.authorise` is the only path to acting.** There is no `send()` in the repository
that does not go through it. The spend cap, the rate limiter and the kill switch are checked
there, once. A limit checked in three places is a limit missing from the fourth.

**`safety.mode: shadow` in `config/params.yaml`.** In shadow mode `authorise` returns a
verdict that is recorded and not acted on: the system proposes, the ledger fills, nothing
reaches a customer. Going live is a deliberate edit by someone who can be named. The HTTP
response carries `acted: false` as a **field**, because a caller that reads `decisions` and
acts on them is precisely the failure this layer exists to prevent, and a sentence in the
docs does not stop it.

**Four states, strictly ordered, worst wins:**

| rung | trigger | behaviour |
|---|---|---|
| `NORMAL` | nothing wrong | everything runs |
| `RULES_ONLY` | the model is unavailable | allocation, audit and notices run without it |
| `CONSERVATIVE_FLOOR` | the hazard model is older than `safety.max_model_age_days` | no asks at all — the P0 floor |
| `HALTED` | kill switch on, or the policy hash does not match | nothing runs |

"Worst wins" is the load-bearing word. Two simultaneous problems must not resolve to the
milder of the two, and an unordered set of independent fallbacks does exactly that.

## Consequences

**The inverted default ships live by accident exactly once.** Live-unless-configured is the
same code with one word changed, and the cost of the wrong default is not symmetric: a
shadow run that should have been live wastes a week, and a live run that should have been
shadow reaches customers.

**`RULES_ONLY` records a state rather than creating a behaviour.** The deterministic path
was already the ordinary path ([ADR 0004](./0004-the-model-does-not-decide.md)), so this
rung names what the system was going to do anyway. That is why it is cheap and why it is
trustworthy — a fallback written for the outage is a fallback first exercised during one.

**`CONSERVATIVE_FLOOR` rests on an unmeasured threshold.** `max_model_age_days: 30` is a
decision, not a measurement: the KKBox frame gives no basis for a drift half-life and
inventing one would be a number without an origin (`calibration.md` §5). What it triggers is
defended in `limitations.md` §8.11; what it rests on is a plausible operational default and
nothing more.

**The cap bounds an allocation, not the service.** A `Guard` is built per request, because
per-process would let the day's first caller exhaust everyone's cap. The cost is recorded in
`limitations.md` §8.10 rather than argued away.

**What enforces it.** `tests/test_safety.py` proves the spend cap cannot be crossed and
covers each rung separately; `tests/test_chaos.py` kills the LLM, corrupts the policy file
and feeds nulls, and CI runs that suite as **its own step** — "the system degraded" is a
different claim from "the tests passed" and deserves its own green tick.

## Alternatives rejected

**A feature flag per capability.** Rejected: independent flags have no ordering, so a stale
model plus a hash mismatch resolves to whichever check ran last.

**Fail closed on everything.** Halting on an LLM outage would be safe and wrong — the
compliance path does not need the model, and a system that stops when it did not have to
teaches its operators to disable the guard.

**Guarding at the send sites.** The argument this ADR opens with. It is not that the
existing sites were wrong; it is that the next one will be added by someone who has not
read this file.
