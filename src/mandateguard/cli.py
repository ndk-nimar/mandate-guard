"""`mandateguard` -- the command line a reviewer actually types.

    uv run mandateguard repro --check
    uv run mandateguard replay --decision-id "P4-sample-s20260905-b500.00:mg_1:w3"
    uv run mandateguard verify-ledger data/ledger/P4-sample-s20260905-b500.00.jsonl
    uv run mandateguard audit --rail enach --amount 20000

Each command is the smallest thing that demonstrates one claim: the whole eval can be
rebuilt, a decision can be re-run, a log can be checked, and a mandate can be judged with
its clauses named. The scripts under `scripts/` remain the way runs are *produced*; this
is the way they are *interrogated*, which is a different audience and deserves a different
surface. `repro` is the one exception, and it exists because GATE 5 is about a stranger
typing one thing rather than four.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from mandateguard.agent.auditor import RulesAuditor
from mandateguard.eval import repro
from mandateguard.ledger.replay import ReplayRefused, replay
from mandateguard.ledger.store import Ledger, LedgerBroken
from mandateguard.models import MandateAuditContext, MandateCategory, Rail
from mandateguard.policy.loader import load_params, load_policy, policy_hash

app = typer.Typer(add_completion=False, help=__doc__)


def _find_ledger(decision_id: str, ledger_path: Path | None) -> Ledger:
    """Locate the ledger holding a decision, from the run id embedded in its id.

    Saves the caller from repeating a path they already encoded in the id they typed. An
    explicit `--ledger` still wins, because a ledger copied elsewhere is a normal thing to
    want to inspect.
    """
    if ledger_path is not None:
        return Ledger(ledger_path)
    run_id = decision_id.split(":", 1)[0]
    from mandateguard.data.paths import ROOT

    return Ledger(ROOT / "data" / "ledger" / f"{run_id}.jsonl")


@app.command("repro")
def repro_command(
    check: Annotated[
        bool,
        typer.Option("--check", help="fail if any regenerated artifact differs from the commit"),
    ] = False,
    verbose: Annotated[
        bool, typer.Option("--verbose", help="stream each script's output instead of logging it")
    ] = False,
) -> None:
    """Rebuild every sample-derived artifact from `data/sample/`, then diff it (GATE 5).

    About three minutes and no download: the committed 5,079-subscriber slice is the only
    input. `--check` turns the run into a claim -- byte-identical or exit 1 -- and the
    report always names the two artifacts that are full-data and therefore *not* rebuilt
    here, because a clean column of `ok` that quietly omits them is the more dangerous
    output.
    """
    report = repro.run(check=check, quiet=not verbose)
    for line in report.lines():
        typer.echo(line)
    if not report.ok:
        raise typer.Exit(1)


@app.command("replay")
def replay_command(
    decision_id: Annotated[str, typer.Option("--decision-id", help="run_id:mandate_id:wN")],
    ledger: Annotated[
        Path | None, typer.Option("--ledger", help="override the ledger path")
    ] = None,
) -> None:
    """Re-run a historical decision and compare it byte for byte.

    Re-runs the whole run, because an allocation is not a function of one mandate: this one
    was not asked partly because others were, and week 3's state is the product of weeks 0
    to 2. See `ledger/replay.py`.
    """
    store = _find_ledger(decision_id, ledger)
    if not store.path.is_file():
        typer.echo(f"no ledger at {store.path}", err=True)
        raise typer.Exit(2)
    try:
        result = replay(store, decision_id)
    except ReplayRefused as exc:
        typer.echo(f"REFUSED: {exc}", err=True)
        raise typer.Exit(2) from exc

    typer.echo(result.line())
    if not result.identical:
        raise typer.Exit(1)


@app.command("verify-ledger")
def verify_command(
    path: Annotated[Path, typer.Argument(help="the JSONL ledger to check")],
) -> None:
    """Walk the hash chain and report the first row that does not hold."""
    try:
        stats = Ledger(path).verify()
    except FileNotFoundError:
        typer.echo(f"no ledger at {path}", err=True)
        raise typer.Exit(2) from None
    except LedgerBroken as exc:
        typer.echo(f"BROKEN: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(
        f"{stats.entries:,} entries, {stats.asked:,} asked, {stats.not_asked:,} not asked "
        f"({stats.refusal_share:.1%} refusals)"
    )
    typer.echo(f"head: {stats.head}")


@app.command("audit")
def audit_command(
    rail: Annotated[Rail, typer.Option("--rail")] = Rail.UPI_AUTOPAY,
    amount: Annotated[
        float, typer.Option("--amount", help="the amount about to be debited")
    ] = 499.0,
    category: Annotated[MandateCategory, typer.Option("--category")] = MandateCategory.GENERAL,
    notice_hours: Annotated[float | None, typer.Option("--notice-hours")] = 36.0,
    afa: Annotated[
        bool, typer.Option("--afa/--no-afa", help="was this debit AFA-validated")
    ] = False,
    mandate_id: Annotated[str, typer.Option("--mandate-id")] = "cli",
) -> None:
    """Judge one mandate against the compiled rulebook, with the clauses named.

    Every default is the compliant value, so a single flag produces a single finding and the
    output is about the flag rather than about the fixture.
    """
    fields = frozenset({"merchant_name", "amount", "debit_datetime", "mandate_reference", "reason"})
    context = MandateAuditContext(
        mandate_id=mandate_id,
        rail=rail,
        category=category,
        amount_inr=amount,
        afa_on_this_transaction=afa,
        pre_debit_notice_hours=notice_hours,
        pre_debit_notice_fields=fields,
        post_transaction_notice_fields=fields | {"transaction_reference", "grievance_redressal"},
    )
    verdict = RulesAuditor().audit(context)
    typer.echo(f"{verdict.verdict.value.upper()}  (policy {verdict.policy_hash})")
    typer.echo(verdict.reason)
    applied = [o for o in verdict.outcomes if o.applied]
    typer.echo(f"\n{len(applied)} of {len(verdict.outcomes)} rules applied.")
    for outcome in applied:
        if outcome.passed is False:
            typer.echo(f"  FAIL clause {outcome.clause} ({outcome.rule_id}): {outcome.remedy}")


@app.command("policy")
def policy_command() -> None:
    """Print the compiled rulebook's identity, re-verified against its circular."""
    policy = load_policy()
    typer.echo(f"{policy.source.circular_no}, {policy.source.dated}")
    typer.echo(f"{len(policy.rules)} rules · policy hash {policy_hash()}")
    typer.echo(f"model configured: {load_params().llm.model}")


if __name__ == "__main__":  # pragma: no cover
    app()
