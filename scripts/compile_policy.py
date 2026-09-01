"""T4.1 entry point: re-compile the RBI circular into a reviewable rule proposal.

    MANDATEGUARD_LLM_MODE=record uv run python scripts/compile_policy.py    # calls the API
    uv run python scripts/compile_policy.py                                 # replays a cassette
    uv run python scripts/compile_policy.py --check                         # no model at all

`--check` is the mode CI runs and the mode worth understanding. It calls nothing: it loads
`policy/mandate_policy.yaml`, re-hashes the committed circular text, and re-checks every
one of the twenty rules against it -- clause number, verbatim quote, expression grammar,
field names. That is the whole guarantee this task claims. The compile is how the rules got
written; the check is why they can be trusted, and it needs no credential to run.

The two model modes write `policy/mandate_policy.proposed.yaml` and never touch the
reviewed rulebook. Merging is a human reading each rule against the clause it cites.
"""

from __future__ import annotations

import argparse
import sys

from mandateguard.agent import compiler
from mandateguard.agent.client import CassetteMissError, build_client
from mandateguard.policy.loader import (
    POLICY_PATH,
    load_params,
    load_policy,
    policy_hash,
    source_text,
)


def check() -> int:
    """Re-verify the committed rulebook against the committed circular. No model."""
    policy = load_policy()  # raises on any hash, clause, quote, grammar or field failure
    circular = source_text(policy)
    clauses = sorted({rule.clause for rule in policy.rules}, key=_clause_sort_key)

    print(f"## Compiled policy -- {policy.source.circular_no}\n")
    print("| field | value |")
    print("|---|---|")
    print(f"| source | {policy.source.name} |")
    print(f"| dated | {policy.source.dated} |")
    print(f"| retrieved | {policy.source.retrieved_on} from {policy.source.url} |")
    print(f"| circular text | `{policy.source.text_file}`, {len(circular):,} bytes |")
    print(f"| circular sha256 | `{policy.source.sha256[:16]}...` (verified) |")
    print(f"| policy version | {policy.version} |")
    print(f"| policy hash | `{policy_hash()}` |")
    print(f"| rules | **{len(policy.rules)}**, across {len(clauses)} clauses |")
    print(f"| clauses cited | {', '.join(clauses)} |")
    print()
    print("Every rule below carries a clause number that exists in the circular and a")
    print("quote that appears in it verbatim; both were checked while loading this table.")
    print()
    print("| clause | rule | fails into | guarded |")
    print("|---|---|---|---|")
    for rule in policy.rules:
        guarded = "--" if rule.applies_when.strip() == "True" else f"`{rule.applies_when}`"
        print(f"| {rule.clause} | `{rule.rule_id}` | {rule.verdict_on_fail} | {guarded} |")
    return 0


def _clause_sort_key(clause: str) -> tuple[int, str]:
    head = clause.split("(")[0]
    return (int(head) if head.isdigit() else 999, clause)


def recompile() -> int:
    """Run the compiler job and write a proposal beside the reviewed rulebook."""
    params = load_params()
    policy = load_policy()
    circular = source_text(policy)
    client = build_client(params.llm)

    try:
        rules, not_compiled = compiler.compile_rules(client, circular)
    except CassetteMissError as exc:
        print(f"{exc}\n", file=sys.stderr)
        print(
            "No cassette has ever been recorded for the compiler job -- see the note in\n"
            "src/mandateguard/agent/compiler.py and docs/limitations.md. The committed\n"
            "rulebook does not depend on one; `--check` verifies it without any model.",
            file=sys.stderr,
        )
        return 2

    path = compiler.write_proposal(policy.source, rules, not_compiled)
    print(f"proposed {len(rules)} rules -> {path}")
    print(f"{len(not_compiled)} clause(s) deliberately not compiled:")
    for entry in not_compiled:
        print(f"  {entry.get('clause')}: {entry.get('why')}")
    print()
    print(f"Review with:  git diff --no-index {POLICY_PATH} {path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify the committed rules against the committed circular; no model call",
    )
    args = parser.parse_args()
    return check() if args.check else recompile()


if __name__ == "__main__":
    raise SystemExit(main())
