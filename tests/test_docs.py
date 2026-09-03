"""T5.7 -- the documents cross-link, and the README's numbers are not typed from memory.

Two failure modes this file exists for, both of which have already happened here.

**A link to a document that does not exist reads exactly like a citation that resolves.**
`calibration.md` §5 and `problem.md` sent every prior-art figure to `prior_art.md` for "the
exact claim and page reference" for the whole of Phases 1-4. The file had never been
written. Nothing failed, nothing warned, and six load-bearing numbers had a citation chain
that terminated in a 404 -- until somebody clicked. This test is the thing that clicks.

**A number typed into prose drifts from the run that produced it.** Every generated
document in this repository is rebuilt by CI and byte-diffed, but the README is written by
hand, and a README quoting a headline that `results.md` no longer contains is worse than a
README with no headline at all. So each figure the README quotes has to still be findable
in the document it came from.

The journals (`worklog.md`, `seekha.md`, `tasks-samjhao.md`) are gitignored and are not
scanned: they are personal working notes, they do not exist in a fresh clone, and a link
*to* them from a submitted document would be a defect this test should catch.
"""

from __future__ import annotations

import re

import pytest

from mandateguard.data.paths import ROOT

DOCS = ROOT / "docs"

JOURNALS = {"worklog.md", "seekha.md", "tasks-samjhao.md", "interview_prep.md"}
"""Gitignored, so they do not exist in a fresh clone. The first three are the Hinglish
journals; `interview_prep.md` is T5.10, which `tasks.md` marks "not submitted"."""

PROCESS = {"tasks.md", "video_script.md"}
"""Committed, but about *building* the project rather than arguing for it. They are not in
the README's document table because that table is the reader's map of the argument."""

LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _scanned() -> list:
    docs = [p for p in sorted(DOCS.rglob("*.md")) if p.name not in JOURNALS]
    return [ROOT / "README.md", ROOT / "CLAUDE.md", *docs]


def _is_journal(target: str) -> bool:
    return any(target.endswith(journal) for journal in JOURNALS)


@pytest.mark.parametrize("source", _scanned(), ids=lambda p: str(p.relative_to(ROOT)))
def test_every_relative_link_resolves(source) -> None:
    text = source.read_text(encoding="utf-8")
    broken = []
    for target in LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")) or _is_journal(target):
            continue
        resolved = (source.parent / target.split("#", 1)[0]).resolve()
        if not resolved.exists():
            broken.append(target)
    assert not broken, f"{source.relative_to(ROOT)} links to nothing: {broken}"


def test_no_submitted_document_links_to_a_journal() -> None:
    """The journals are gitignored. A link to one is a dead link in every fresh clone.

    `CLAUDE.md` is exempt and is the only exemption: it is the file that *instructs* the
    journals to be written, its links point at files the reader is told to create, and it
    is not part of the submission. Everything else in `_scanned()` is.
    """
    offenders = []
    for source in _scanned():
        if source.name == "CLAUDE.md":
            continue
        for target in LINK.findall(source.read_text(encoding="utf-8")):
            if _is_journal(target):
                offenders.append(f"{source.relative_to(ROOT)} -> {target}")
    assert not offenders, offenders


README_FIGURES = [
    ("1,215.9", "docs/results.md"),
    ("1,131.9", "docs/results.md"),
    ("+7.42%", "docs/results.md"),
    ("90.6", "docs/results.md"),
    ("16,236", "docs/results.md"),
    ("+0.05%", "docs/results.md"),
    ("5,079", "docs/results.md"),
]


@pytest.mark.parametrize(("figure", "source"), README_FIGURES)
def test_the_readme_quotes_a_number_the_generated_document_still_contains(
    figure: str, source: str
) -> None:
    """Both halves matter: the README says it, and the run still produces it."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    generated = (ROOT / source).read_text(encoding="utf-8")
    assert figure in readme, f"README no longer quotes {figure}; drop it from README_FIGURES"
    assert figure in generated, (
        f"README quotes {figure} but {source} does not contain it any more. "
        "The run moved and the prose did not."
    )


def test_the_readme_lists_every_document_it_ships() -> None:
    """A document that exists but is unreachable from the README is a document nobody reads."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for doc in sorted(DOCS.glob("*.md")):
        if doc.name in JOURNALS or doc.name in PROCESS:
            continue
        assert doc.name in readme, f"docs/{doc.name} is not reachable from the README"
