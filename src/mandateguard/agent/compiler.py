"""Job 1 of four: compile a regulation into rules a machine can evaluate (T4.1).

The model is given the committed circular text and the exact vocabulary it is allowed to
write expressions in, and asked for structured rules. Nothing it returns is trusted:

1. the response is parsed against a JSON schema the request declared;
2. each rule is validated by `models.PolicyRule`, which requires a clause and a quote;
3. `policy.loader.check_rule_citations` checks every quote is a literal substring of the
   circular, and every clause number is one the circular actually has;
4. `agent.expression` parses every expression under a call-free whitelist and rejects any
   field name `MandateAuditContext` does not define.

Only a proposal that survives all four is written to disk, and it is written to
`policy/mandate_policy.proposed.yaml` -- **never** over the reviewed rulebook. The diff
between the two files is the human-in-the-loop step, and a compiler that could overwrite
its own reviewed output would delete that step.

### What the shipped rulebook actually is

`policy/mandate_policy.yaml` holds twenty reviewed rules. They were produced by running
this job's prompt against the circular during the T4.1 working session, then read clause by
clause and edited by hand before being committed -- three of the edits are recorded in the
rules themselves (`debit_within_customer_cap` is marked as an inference, `opt_out_facility`
keeps the circular's typo, and no velocity rule was compiled despite clause 8's heading
inviting one). This module re-runs the same job so that the compile is repeatable rather
than a one-off, and so that the next circular amendment is a diff instead of a rewrite.

There is one gap, and it is stated rather than hidden: **no cassette has been recorded for
this job**, because no API credential was available on the machine where Phase 4 was built.
`scripts/compile_policy.py` therefore raises a cassette miss until someone records one. The
committed rulebook does not depend on that recording -- it is already reviewed and already
checked against the source text by the loader on every import -- but the *repeatability*
claim does, and until the cassette exists the claim is that the rules are checkable, not
that the compile has been reproduced. See `docs/limitations.md`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from mandateguard.agent.client import LLMClient
from mandateguard.models import MandateAuditContext, PolicyRule, Verdict
from mandateguard.policy.loader import POLICY_DIR, PolicySource, check_rule_citations

JOB = "policy_compiler"
PROPOSAL_PATH = POLICY_DIR / "mandate_policy.proposed.yaml"

RULE_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "properties": {
        "rules": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "rule_id": {"type": "string"},
                    "clause": {"type": "string"},
                    "quote": {"type": "string"},
                    "description": {"type": "string"},
                    "applies_when": {"type": "string"},
                    "expression": {"type": "string"},
                    "verdict_on_fail": {"enum": ["non_compliant", "needs_human"]},
                    "remedy": {"type": "string"},
                },
                "required": [
                    "rule_id",
                    "clause",
                    "quote",
                    "description",
                    "applies_when",
                    "expression",
                    "verdict_on_fail",
                    "remedy",
                ],
                "additionalProperties": False,
            },
        },
        "not_compiled": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "clause": {"type": "string"},
                    "why": {"type": "string"},
                },
                "required": ["clause", "why"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["rules", "not_compiled"],
    "additionalProperties": False,
}

SYSTEM = """\
You compile financial regulation into machine-evaluable rules for an Indian recurring-\
mandate system. Precision matters more than coverage: a rule that overstates the \
regulation is worse than a clause left uncompiled, because a downstream system will act \
on it.

Hard requirements, all of them checked by code after you answer:

* `quote` must be a VERBATIM span copied from the circular below, including any \
typographical errors it contains. Do not tidy, paraphrase, or join text from two clauses.
* `clause` must be the clause number that span comes from, e.g. "6(a)" or "10(c)".
* `expression` must be true exactly when the mandate COMPLIES with that clause.
* `applies_when` is a guard: when it is false the rule is skipped, not failed. Use it for \
clauses that only bind in some cases, and for exemptions carved out by other clauses.
* `verdict_on_fail` is "non_compliant" for a breach, and "needs_human" when a failure means \
the regulation does not reach this mandate at all rather than that a rule was broken.
* `remedy` says what to do about a failure, in one or two plain sentences.

The expression language is deliberately tiny. Legal: `and`, `or`, `not`, the comparisons \
`== != < <= > >= in "not in" is "is not"`, field names, literals, and literal tuples. \
ILLEGAL, and rejected: function calls of any kind, attribute access, subscripting, \
arithmetic. There is no `all(...)` and no `len(...)`; spell a multi-field check out as \
several `in` comparisons joined by `and`.

The ONLY names you may use are these fields, with these types:

{fields}

Two things to get right rather than to cover:

* Do not invent an obligation to match a heading. If a clause's heading names something \
its text does not require, put it in `not_compiled` with the reason.
* Look for clauses whose breach runs backwards -- an exemption that can be over-claimed is \
a rule, and it is the one a rulebook of obligations always misses.

The circular follows in full.

---

{circular}
"""

PROMPT = """\
Compile the circular into rules. Return every obligation the text actually states, and \
list in `not_compiled` every clause you deliberately did not turn into a rule, with the \
reason. Prefer several narrow rules over one broad one: each rule should fail for exactly \
one reason, so that a failure names a single clause.\
"""


def _field_catalogue() -> str:
    """The audit context's fields and types, as the prompt shows them to the model.

    Generated from the model rather than typed out, for the same reason every number in
    `docs/` is generated: a hand-maintained copy of a schema drifts from the schema, and
    the drift shows up as rules referencing fields that no longer exist.
    """
    lines = []
    for name, field in MandateAuditContext.model_fields.items():
        annotation = getattr(field.annotation, "__name__", str(field.annotation))
        note = f" -- {field.description}" if field.description else ""
        lines.append(f"* `{name}`: {annotation}{note}")
    return "\n".join(lines)


def build_request(circular: str) -> tuple[str, str, Mapping[str, Any]]:
    """The exact (system, prompt, schema) triple this job sends. Also its cassette key."""
    return SYSTEM.format(fields=_field_catalogue(), circular=circular), PROMPT, RULE_SCHEMA


def compile_rules(
    client: LLMClient, circular: str
) -> tuple[list[PolicyRule], list[dict[str, str]]]:
    """Run the compile and return `(rules, deliberately-not-compiled clauses)`.

    Raises rather than repairs. A model that returns a rule quoting text the circular does
    not contain has made the one mistake this whole design exists to catch, and quietly
    dropping that rule would hide how often it happens -- which is a number T4.7 reports.
    """
    system, prompt, schema = build_request(circular)
    result = client.run(JOB, system, prompt, schema=schema)
    if result.refused:
        raise RuntimeError(
            f"the {JOB!r} job was refused (stop_reason={result.stop_reason!r}); no rules "
            "were produced and none were invented."
        )

    try:
        payload = json.loads(result.text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{JOB} returned text that is not JSON: {exc}") from exc

    rules: list[PolicyRule] = []
    for raw in payload.get("rules", []):
        try:
            rules.append(PolicyRule.model_validate(raw))
        except ValidationError as exc:
            raise RuntimeError(
                f"{JOB} produced a rule that is not a PolicyRule: {raw.get('rule_id')!r}\n{exc}"
            ) from exc

    check_rule_citations(rules, circular, "the circular text supplied to the compiler")
    _check_expressions(rules)
    return rules, list(payload.get("not_compiled", []))


def _check_expressions(rules: list[PolicyRule]) -> None:
    """Whitelist-check every expression before the proposal is written.

    Duplicates what `Policy` enforces at load time on purpose: the loader protects the
    running system, and this protects the reviewer, who should never be asked to read a
    diff containing a rule that could not have loaded anyway.
    """
    from mandateguard.agent.expression import ExpressionError, referenced_names

    known = set(MandateAuditContext.model_fields)
    for rule in rules:
        for field, text in (("expression", rule.expression), ("applies_when", rule.applies_when)):
            try:
                unknown = sorted(referenced_names(text) - known)
            except ExpressionError as exc:
                raise RuntimeError(f"rule {rule.rule_id!r} has an illegal {field}: {exc}") from exc
            if unknown:
                raise RuntimeError(
                    f"rule {rule.rule_id!r} reads unknown field(s) {unknown} in its {field}."
                )


def write_proposal(
    source: PolicySource,
    rules: list[PolicyRule],
    not_compiled: list[dict[str, str]],
    path: Path = PROPOSAL_PATH,
) -> Path:
    """Write the proposal next to the reviewed rulebook, for `git diff --no-index`.

    `sort_keys=False` keeps each rule's fields in declaration order so the diff reads the
    way the rule reads. `default_flow_style=False` keeps it block-formatted for the same
    reason: a flow-style YAML rule is one line and diffs as one line, which hides which
    field changed.
    """
    document = {
        "version": 0,
        "source": json.loads(source.model_dump_json()),
        "not_compiled": not_compiled,
        "rules": [
            {
                "rule_id": rule.rule_id,
                "clause": rule.clause,
                "quote": rule.quote,
                "description": rule.description,
                "applies_when": rule.applies_when,
                "expression": rule.expression,
                "verdict_on_fail": Verdict(rule.verdict_on_fail).value,
                "remedy": rule.remedy,
            }
            for rule in rules
        ],
    }
    path.write_text(
        "# PROPOSAL -- not loaded by anything. Diff this against mandate_policy.yaml,\n"
        "# read every rule against the clause it cites, then merge by hand.\n"
        + yaml.safe_dump(document, sort_keys=False, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    return path
