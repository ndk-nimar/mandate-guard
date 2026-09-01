"""T4.6/T4.7 -- the golden set, and the eval that scores it.

Three different claims are checked here, and they are worth keeping apart.

**The file is well-formed.** Every case parses, every context validates, every expectation
is a real verdict, and no two cases share an id. A malformed golden set fails every
downstream number silently.

**The set is honest.** It contains all three verdicts, it exercises every compiled clause,
and its abstains are not all of one kind. A golden set of 120 non-compliant cases would
score 100% against a system that returns `non_compliant` unconditionally.

**The system agrees with it.** This is the T4.7 number, and it is the least interesting of
the three -- see `docs/llm_eval.md` §6 for why 100% on this arm is worth less than it looks.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from mandateguard.agent.auditor import RulesAuditor
from mandateguard.models import MandateAuditContext, Verdict
from mandateguard.policy.loader import load_policy

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_PATH = ROOT / "tests" / "golden" / "mandates.jsonl"
EVAL_PATH = ROOT / "docs" / "llm_eval.md"


@pytest.fixture(scope="module")
def cases() -> list[dict]:
    lines = GOLDEN_PATH.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


@pytest.fixture(scope="module")
def auditor() -> RulesAuditor:
    return RulesAuditor()


# --------------------------------------------------------------------------------
# Well-formed.
# --------------------------------------------------------------------------------


def test_the_golden_set_meets_the_task_size(cases):
    """T4.6 asks for about 120 mandate edge cases."""
    assert len(cases) >= 120


def test_every_case_is_complete_and_uniquely_identified(cases):
    ids = [c["case_id"] for c in cases]
    assert len(set(ids)) == len(ids)
    for case in cases:
        assert case["why"].strip(), case["case_id"]
        assert case["expected_verdict"] in {v.value for v in Verdict}
        assert case["requires"] in {"rules", "extraction"}


def test_every_rules_case_carries_a_valid_context(cases):
    for case in cases:
        if case["requires"] == "rules":
            MandateAuditContext.model_validate(case["context"])


def test_a_finding_case_always_names_the_clauses_it_expects(cases):
    """An expectation of `non_compliant` with no clauses would pass against any breach."""
    for case in cases:
        if case["expected_verdict"] == Verdict.COMPLIANT.value:
            assert case["expected_citations"] == [], case["case_id"]
        else:
            assert case["expected_citations"], case["case_id"]


def test_the_file_is_sorted_so_a_diff_shows_changes_not_movement(cases):
    """ADR 0003 reaches the golden set too."""
    assert [c["case_id"] for c in cases] == sorted(c["case_id"] for c in cases)


def test_the_committed_file_matches_its_builder():
    """The file is generated, so a hand-edit is a defect. `--check` is what CI runs."""
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_golden.py"), "--check"],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stderr


# --------------------------------------------------------------------------------
# Honest.
# --------------------------------------------------------------------------------


def test_all_three_verdicts_are_represented(cases):
    """A set of one verdict scores 100% against a system that always returns it."""
    present = {c["expected_verdict"] for c in cases}
    assert present == {v.value for v in Verdict}


def test_no_verdict_dominates_the_set(cases):
    """Not a hard rule, a smell test: a set that is 90% one answer measures little."""
    counts = {v.value: 0 for v in Verdict}
    for case in cases:
        counts[case["expected_verdict"]] += 1
    assert max(counts.values()) / len(cases) < 0.7, counts


def test_every_compiled_clause_has_at_least_one_case(cases):
    """The direction things fall down: not "does every case cite a clause" but "does every
    clause have a case". A clause with no case is a rule nothing has ever tested."""
    cited = set()
    for case in cases:
        cited |= set(case["expected_citations"])
    compiled = {rule.clause for rule in load_policy().rules}
    assert compiled - cited == set()


def test_the_abstains_are_not_all_of_one_kind(cases):
    """Scope abstains and missing-input abstains are different questions, and a set with
    only the first would leave the auditor's uncertainty path untested."""
    abstains = [c for c in cases if c["expected_verdict"] == Verdict.NEEDS_HUMAN.value]
    kinds = {c["requires"] for c in abstains}
    assert kinds == {"rules", "extraction"}, kinds


def test_every_extraction_case_names_the_field_that_blocks_it(cases):
    """ "Abstained" and "abstained for the right reason" are different results. Without the
    blocking field, an auditor that abstains on everything scores perfectly here."""
    known = set(MandateAuditContext.model_fields)
    for case in cases:
        if case["requires"] == "extraction":
            assert case["blocking_field"] in known, case["case_id"]
            assert case["expected_verdict"] == Verdict.NEEDS_HUMAN.value


def test_the_boundary_cases_the_red_team_arm_names_are_present(cases):
    """T4.8's list, checked against the set rather than assumed. Two of its entries have no
    case and `scripts/build_golden.py` says why -- cross-border, which the framework
    explicitly treats identically, and which therefore has nothing to test."""
    ids = {c["case_id"] for c in cases}
    assert "afa_general_149990" in ids  # Rs.14,999
    assert "afa_general_150010" in ids  # Rs.15,001
    assert any(i.startswith("exempt_fastag") for i in ids)
    assert any(i.startswith("exempt_ncmc") for i in ids)
    assert "modification_without_afa" in ids
    assert "grandfathered_registered_without_afa" in ids
    assert "afa_enhanced_insurance_premium_100000" in ids
    assert "variable_debit_above_cap" in ids


# --------------------------------------------------------------------------------
# The system agrees.
# --------------------------------------------------------------------------------


def test_every_rules_case_gets_its_expected_verdict_and_citations(cases, auditor):
    """The T4.7 number. A disagreement here is a finding either way: the compiled rule is
    wrong or the expectation is, and the clause settles which."""
    wrong = []
    for case in cases:
        if case["requires"] != "rules":
            continue
        verdict = auditor.audit(MandateAuditContext.model_validate(case["context"]))
        got = (verdict.verdict.value, sorted(set(verdict.citations)))
        want = (case["expected_verdict"], case["expected_citations"])
        if got != want:
            wrong.append(f"{case['case_id']}: expected {want}, got {got}")
    assert not wrong, "\n".join(wrong)


def test_scoring_is_deterministic(cases, auditor):
    """Same case, same verdict, twice. The eval document is byte-diffed in CI."""
    case = next(c for c in cases if c["requires"] == "rules")
    context = MandateAuditContext.model_validate(case["context"])
    assert auditor.audit(context).model_dump() == auditor.audit(context).model_dump()


# --------------------------------------------------------------------------------
# The document.
# --------------------------------------------------------------------------------


def test_the_eval_document_is_regenerated_identically():
    """GATE 4: `docs/llm_eval.md` is CI-generated, which means a stranger's fork produces
    the identical file. Verified by regenerating it and comparing bytes."""
    before = EVAL_PATH.read_bytes()
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "make_llm_eval.py")],
        capture_output=True,
        text=True,
        cwd=ROOT,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert EVAL_PATH.read_bytes() == before, "docs/llm_eval.md drifted from its generator"


def test_the_document_reports_the_unscored_arm_rather_than_omitting_it():
    """The failure this guards against is a document that looks complete because the arm it
    could not measure was quietly dropped."""
    text = EVAL_PATH.read_text(encoding="utf-8")
    assert "not measured" in text
    assert "Cost per verdict" in text
    assert "p95 latency" in text
    assert "Abstain precision" in text


def test_the_document_states_what_its_own_headline_is_worth():
    """100% on a set written by the same reader who wrote the rules is not external
    validation, and the document has to say so where the number is, not in a footnote."""
    text = EVAL_PATH.read_text(encoding="utf-8")
    assert "The same reader wrote both sides" in text
    assert "not external validation" in text


def test_no_wall_clock_number_reaches_the_document():
    """A wall-clock measurement differs per machine, so writing one in would make the CI
    byte-diff fail on a fast laptop -- and rounding it until it stopped moving would be a
    number rounded until it stopped meaning anything."""
    text = EVAL_PATH.read_text(encoding="utf-8")
    assert " ms/case" not in text
    assert "wall-clock" not in text.split("## 5.")[0]


def test_the_natural_adversarial_split_is_declared_and_non_trivial(cases):
    """T4.8's gap needs two sets that are both real. A split that put three cases in the
    adversarial bucket would report a gap computed from noise."""
    families = {c["family"] for c in cases}
    assert families == {"natural", "adversarial"}
    adversarial = [c for c in cases if c["family"] == "adversarial"]
    assert len(adversarial) >= 40
    assert 0.3 < len(adversarial) / len(cases) < 0.7


def test_the_adversarial_set_contains_both_directions_of_failure(cases):
    """Boundary cases that should be findings, and near-misses that should not. An
    adversarial set of breaches alone cannot catch a system that is merely over-eager."""
    adversarial = [c for c in cases if c["family"] == "adversarial"]
    verdicts = {c["expected_verdict"] for c in adversarial}
    assert Verdict.COMPLIANT.value in verdicts
    assert Verdict.NON_COMPLIANT.value in verdicts


def test_the_document_carries_the_adversarial_gap_and_the_cut():
    """GATE 4: llm_eval.md contains the adversarial gap, or states that the red-team arm
    was cut. This build does both -- the generator is cut, the number is measured."""
    text = EVAL_PATH.read_text(encoding="utf-8")
    assert "Natural versus adversarial" in text
    assert "**Gap**" in text
    assert "adversarial generator, is cut" in text
    assert "floor on the true one" in text
