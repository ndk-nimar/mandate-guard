"""T5.8 -- the reproduction command, checked without spending three minutes.

The end-to-end run is real and it is slow: three scripts, about three minutes, and it
rebuilds files that are committed. Running that inside the ordinary suite would make every
`pytest` invocation rewrite `docs/results.md`, so it lives behind `RUN_REPRO=1` -- the same
shape as the two-process boot in `tests/spikes/test_two_process.py`.

What runs every time is the part that rots silently: the step table pointing at scripts
that still exist, at artifacts that are still committed, and the report still printing the
two artifacts a fresh clone cannot rebuild. That last one is the assertion worth having.
A `NOT_REPRODUCED` list that quietly stops being printed turns this command from an honest
report into a green tick.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from mandateguard.data.paths import ROOT
from mandateguard.eval import repro


def test_every_step_points_at_a_script_that_exists() -> None:
    for step in repro.STEPS:
        assert step.path.is_file(), f"{step.name} points at a missing {step.script}"


def test_every_produced_artifact_is_committed() -> None:
    """A step claiming a file that is not in git makes `--check` vacuously green.

    `git diff` has nothing to say about an untracked path, so an artifact that drifted out
    of version control would be rebuilt, compared against nothing, and reported as
    byte-identical.
    """
    tracked = subprocess.run(
        ["git", "ls-files", "--", *[p for step in repro.STEPS for p in step.produces]],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    for step in repro.STEPS:
        for produced in step.produces:
            assert produced in tracked, f"{produced} is not tracked, so --check cannot see it"


def test_nothing_is_both_reproduced_and_not_reproduced() -> None:
    produced = {path for step in repro.STEPS for path in step.produces}
    for path, _reason, _command in repro.NOT_REPRODUCED:
        assert path not in produced, f"{path} is claimed by a step and by NOT_REPRODUCED"


def test_the_things_we_cannot_reproduce_still_exist() -> None:
    """Otherwise the honesty note names files nobody can go and look at."""
    for path, _reason, _command in repro.NOT_REPRODUCED:
        assert (ROOT / path).exists(), path


def test_the_report_names_the_directory_it_writes_to(monkeypatch) -> None:
    """The 45x finding: same tree, same output, 81s or 3,668s depending on this path.

    Nothing failed in the slow case and nothing warned -- the only symptom was an hour of
    waiting, and `.env` is gitignored so a fresh clone never inherits the setting that
    avoids it. Naming the directory does not make it fast; it makes the hour diagnosable.
    """
    monkeypatch.delenv("MANDATEGUARD_DATA_DIR", raising=False)
    unset = repro.ReproReport().lines()[0:2]
    assert "derived frames ->" in unset[0]
    assert "MANDATEGUARD_DATA_DIR is unset" in unset[1]
    assert "cloud-synced" in unset[1]

    monkeypatch.setenv("MANDATEGUARD_DATA_DIR", str(ROOT))
    assert "is set" in repro.ReproReport().lines()[1]


def test_the_report_always_names_what_it_did_not_reproduce() -> None:
    """Even an empty report. This is the line the command exists to keep printing."""
    text = "\n".join(repro.ReproReport().lines())
    for path, _reason, _command in repro.NOT_REPRODUCED:
        assert path in text
    assert "Not reproduced from the sample" in text


def test_an_empty_report_is_not_ok() -> None:
    """ "Nothing ran" must not read as "everything passed"."""
    assert repro.ReproReport().ok is False


def _fake(name: str, script: str) -> repro.Step:
    return repro.Step(name=name, script=script, args=(), produces=(), why="fixture")


def test_a_failing_step_stops_the_run(tmp_path) -> None:
    """The second step must not run on top of a first one that did not finish."""
    boom = tmp_path / "boom.py"
    boom.write_text("import sys; sys.exit(3)\n", encoding="utf-8")
    never = tmp_path / "never.py"
    never.write_text("open('ran.txt', 'w').close()\n", encoding="utf-8")

    steps = (_fake("boom", boom.name), _fake("never", never.name))
    original = repro.SCRIPTS
    try:
        repro.SCRIPTS = tmp_path
        report = repro.run(steps=steps)
    finally:
        repro.SCRIPTS = original

    assert len(report.steps) == 1
    assert report.steps[0].returncode == 3
    assert report.ok is False
    assert not (ROOT / "ran.txt").exists()


def test_a_missing_script_is_an_error_rather_than_a_silent_skip(tmp_path) -> None:
    steps = (_fake("gone", "not_here.py"),)
    original = repro.SCRIPTS
    try:
        repro.SCRIPTS = tmp_path
        with pytest.raises(FileNotFoundError, match="repository checkout"):
            repro.run(steps=steps)
    finally:
        repro.SCRIPTS = original


def test_drift_reads_as_a_failure_not_a_note() -> None:
    report = repro.ReproReport(checked=True, drifted=["docs/results.md"])
    assert report.ok is False
    assert "DRIFTED" in "\n".join(report.lines())


def test_drifted_over_nothing_asks_git_nothing() -> None:
    assert repro.drifted([]) == []


@pytest.mark.skipif(
    os.environ.get("RUN_REPRO") != "1",
    reason="rebuilds committed artifacts and takes ~3 minutes; set RUN_REPRO=1 to run",
)
def test_the_whole_thing_reproduces_byte_for_byte() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "mandateguard.cli", "repro", "--check"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "byte-identical" in completed.stdout
