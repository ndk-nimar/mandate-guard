"""T5.11 -- what CI gates on, now that byte-identity turned out to be machine-local.

    uv run mandateguard repro --check   # regenerates; may report drift
    uv run python scripts/check_drift.py

### Why this exists

[ADR 0003](../docs/adr/0003-determinism-of-derived-data.md) asks that every derived file be
byte-identical, and `repro --check` enforces exactly that. On 2026-09-04 -- the first day
this repository had a remote, and therefore the first day CI had ever run -- that gate
failed on `ubuntu-latest`, and then failed again on `windows-latest`. Three machines, three
different `results.md`:

| cell | this laptop | GitHub windows-latest | GitHub ubuntu-latest |
|---|---|---|---|
| `P1` ARR retained | 384,906 | 384,90**7** | 384,90**5** |
| `P4` ARR retained | 413,470 | 413,47**1** | 413,46**9** |

Floating-point addition is not associative, so a sum over 1,354 mandates x 12 weeks lands
on a different last digit depending on the order the hardware and its math library
accumulate in. That is not a property this code can fix by being more careful, and it is
not a property any CI runner can be configured out of. **Byte-identity is a same-machine
guarantee.** `docs/limitations.md` 9 carries the measurement.

### What is gated instead, and why this is still a real gate

Every figure this project quotes was identical on all three machines. The drift is confined
to the last digit of the largest sums. So the rule here is not "close enough" -- it is
*named*:

* **Prose lines must be byte-identical.** Every number quoted in a sentence, including the
  +7.42% Adyen comparison that `limitations.md` 1 turns on, is a prose or headline-table
  figure. None of them moved on any machine, and if one moves, that is a result changing.
* **Table cells must be exact**, except in the columns listed in `TOLERANT_COLUMNS` -- the
  large rupee sums and the three-decimal rates where drift was actually measured. There a
  cell may differ by at most **one unit in its last printed digit**, which is the largest
  difference any of the three machines produced.
* **PNGs are compared on dimensions, not bytes.** matplotlib resolves different fonts per
  platform, so the same chart rasterises different bytes; `segments.png` was byte-identical
  across two Windows machines and 7 KB apart on Linux. Byte-comparing a rasterised chart in
  CI tests the font cache, not the figure.
* **Everything else must be byte-identical.** A file not named above -- `llm_eval.md`, for
  instance -- has no measured drift and gets no allowance.

The exact-byte check is not weakened, only relocated: `repro --check` still fails on one
byte and is still the gate that runs before a commit, on the machine the artifacts are
committed from. That is the gate that caught the mandate book returning 1,053 and 1,054 on
identical input, and it still would.

### It refuses to pass on a rebuild that did not happen

The failure mode this file must not have: `repro` crashes, regenerates nothing, `git diff`
is empty, and an empty diff reads as success. So the report `repro` printed is required as
input, and every step in it has to say `ok`. An unrun rebuild fails here rather than passing
quietly -- the same reasoning as ADR 0003's own point that a wrong answer gets found and an
absent one does not.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REPORT = ROOT / "repro-report.txt"

# Columns where cross-machine drift was measured (limitations.md 9.2). Everything not named
# here is compared exactly, including every column of the Adyen table in results.md 5, which
# is the comparison the project's headline rests on.
TOLERANT_COLUMNS = {
    "rate",
    "arr retained (inr)",
    "profit at optimum",
    # The budget grid in 3 is one column per arm, all of them rupee totals.
    "p0",
    "p1",
    "p2",
    "p3",
    "p4",
    "p5",
}

# Everything repro regenerates. Only these are examined: a source file edited in the working
# tree is not drift. In CI the tree is clean, so scoping this to the artifacts has to be
# deliberate rather than emergent -- checking "everything git reports as changed" passes in
# CI and fails on a developer's machine for reasons that have nothing to do with artifacts.
ARTIFACTS = {
    "docs/results.md",
    "docs/img/sweeps.png",
    "docs/img/segments.png",
    "docs/llm_eval.md",
}

# Of those, the ones whose bytes may differ, and the rule applied to each instead. An
# artifact in neither set -- llm_eval.md -- has no measured drift and gets no allowance.
CELL_CHECKED = {"docs/results.md"}
DIMENSION_CHECKED = {"docs/img/sweeps.png", "docs/img/segments.png"}

NUMBER = re.compile(r"-?\d[\d,]*(?:\.\d+)?")

# The steps repro reports. All three have to have run for a diff to mean anything.
REPRO_STEPS = ("results", "llm-eval", "segments")


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout


def committed(path: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"HEAD:{path}"], cwd=ROOT, capture_output=True, check=True
    ).stdout


def last_digit_tolerance(token: str) -> float:
    """One unit in the last printed digit: 413,470 -> 1.0, 83.604 -> 0.001."""
    return 10.0 ** -(len(token.split(".")[1]) if "." in token else 0)


def numbers_agree(old: str, new: str, tolerant: bool) -> tuple[bool, str]:
    """Compare two table cells. Returns (ok, why-not)."""
    if old == new:
        return True, ""
    if not tolerant:
        return False, f"exact column changed: {old!r} -> {new!r}"

    # The non-numeric skeleton must survive -- "INR 1,234" may not become "1,234".
    if NUMBER.sub("#", old) != NUMBER.sub("#", new):
        return False, f"cell shape changed: {old!r} -> {new!r}"

    for a, b in zip(NUMBER.findall(old), NUMBER.findall(new), strict=True):
        if a == b:
            continue
        try:
            fa, fb = float(a.replace(",", "")), float(b.replace(",", ""))
        except ValueError:
            return False, f"unparseable number: {a!r} -> {b!r}"
        tol = max(last_digit_tolerance(a), last_digit_tolerance(b))
        if abs(fa - fb) > tol + 1e-9:
            return False, f"moved by more than one digit: {a} -> {b} (limit {tol:g})"
    return True, ""


def split_row(line: str) -> list[str]:
    return [c.strip() for c in line.strip().strip("|").split("|")]


def is_table_row(line: str) -> bool:
    stripped = line.strip()
    return stripped.startswith("|") and stripped.endswith("|")


def is_separator(line: str) -> bool:
    return is_table_row(line) and set(line.strip()) <= set("|-: \t")


def read_lines(text: str) -> list[str]:
    return text.replace("\r\n", "\n").split("\n")


def check_cells(path: str, failures: list[str]) -> None:
    old_lines = read_lines(committed(path).decode("utf-8"))
    new_lines = read_lines((ROOT / path).read_text(encoding="utf-8"))

    if len(old_lines) != len(new_lines):
        failures.append(f"{path}: line count changed, {len(old_lines)} -> {len(new_lines)}")
        return

    header: list[str] = []
    for n, (old, new) in enumerate(zip(old_lines, new_lines, strict=True), start=1):
        # A header is the row immediately before a |---|---| separator.
        if is_table_row(old) and n < len(old_lines) and is_separator(old_lines[n]):
            header = [c.lower() for c in split_row(old)]

        if old == new:
            continue

        if not is_table_row(old) or not is_table_row(new):
            failures.append(f"{path}:{n}: prose line changed\n    - {old}\n    + {new}")
            continue

        old_cells, new_cells = split_row(old), split_row(new)
        if len(old_cells) != len(new_cells):
            failures.append(f"{path}:{n}: column count changed")
            continue

        for i, (a, b) in enumerate(zip(old_cells, new_cells, strict=True)):
            name = header[i] if i < len(header) else f"column {i}"
            # A column whose heading is itself a number is a sweep axis -- 4's uplift x
            # backfire plane heads its columns 0.0005, 0.0010 and so on -- and every cell
            # under one is a rupee total, drifting for the same reason the named columns
            # do. Found by running this check against the artifacts both CI runners
            # actually produced, which is the only reason it is here: 4's plane was not on
            # the list until three of its cells failed a test that was expected to pass.
            tolerant = name in TOLERANT_COLUMNS or NUMBER.fullmatch(name) is not None
            ok, why = numbers_agree(a, b, tolerant=tolerant)
            if not ok:
                failures.append(f"{path}:{n}: column {name!r}: {why}")


def png_size(data: bytes) -> tuple[int, int]:
    """Width and height from the IHDR chunk, which is always the first one."""
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("not a PNG")
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def check_dimensions(path: str, failures: list[str]) -> None:
    try:
        old = png_size(committed(path))
        new = png_size((ROOT / path).read_bytes())
    except ValueError as exc:
        failures.append(f"{path}: {exc}")
        return
    if old != new:
        failures.append(f"{path}: image size changed, {old[0]}x{old[1]} -> {new[0]}x{new[1]}")
    else:
        print(f"  ok    {path:24} bytes differ, {new[0]}x{new[1]} unchanged")


def check_report(failures: list[str]) -> None:
    """The rebuild has to have happened. An empty diff after a crash is not a pass."""
    if not REPORT.exists():
        failures.append(
            f"{REPORT.name} is missing. Run `uv run mandateguard repro --check` and tee its "
            "output there first -- without it, an empty diff cannot be told apart from a "
            "rebuild that never ran."
        )
        return
    text = REPORT.read_text(encoding="utf-8", errors="replace")
    missing = [s for s in REPRO_STEPS if not re.search(rf"^\s*ok\s+{re.escape(s)}\s", text, re.M)]
    if missing:
        failures.append(
            f"repro did not report 'ok' for: {', '.join(missing)}. The rebuild did not "
            "complete, so nothing below is evidence of anything."
        )
    else:
        print(f"  ok    repro rebuilt all {len(REPRO_STEPS)} steps")


def main() -> int:
    print("Drift check -- what CI gates on (docs/limitations.md 9)\n")
    failures: list[str] = []
    check_report(failures)

    touched = {p for p in git("diff", "--name-only").split("\n") if p.strip()}
    changed = sorted(touched & ARTIFACTS)
    if not changed:
        print("  ok    every derived file is byte-identical on this machine")
    for path in sorted(touched - ARTIFACTS):
        print(f"  --    {path:24} not a repro artifact, ignored")

    for path in changed:
        if path in CELL_CHECKED:
            before = len(failures)
            check_cells(path, failures)
            if len(failures) == before:
                print(f"  ok    {path:24} drift within one digit, quoted figures unchanged")
        elif path in DIMENSION_CHECKED:
            check_dimensions(path, failures)
        else:
            failures.append(
                f"{path}: changed, and it is an artifact with no measured drift allowance. "
                "This file is supposed to be byte-identical."
            )

    if failures:
        print("\nFAILED. This is not the known cross-machine drift:\n")
        for line in failures:
            print(f"  {line}")
        print(
            "\nThe allowance is defined in scripts/check_drift.py and justified in\n"
            "docs/limitations.md 9. If this drift is legitimate, the committed artifacts\n"
            "need regenerating and the change needs arguing for -- not widening the rule."
        )
        return 1

    print("\nPASSED. Every quoted figure is unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
