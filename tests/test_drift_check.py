"""The gate that replaced the byte gate in CI, tested at its boundary.

`scripts/check_drift.py` exists because `repro --check` compares bytes and bytes turned out
to be a same-machine property (`docs/limitations.md` §9). It allows one unit in the last
printed digit, in named columns only. That allowance is the whole risk: too wide and it
waves through a result change, too narrow and CI is red forever on arithmetic nobody
controls.

So the boundary is pinned here rather than trusted. Every case below is one digit either
side of the line -- 413,470 -> 413,471 must pass and 413,470 -> 413,472 must fail -- because
an allowance tested only in the middle of its range is an allowance nobody has measured.

The reason this file exists at all: the thing that produced §9 was a workflow that had never
run. An untested gate is the same object.
"""

from __future__ import annotations

import importlib.util
import sys

import pytest

from mandateguard.data.paths import ROOT

_spec = importlib.util.spec_from_file_location("check_drift", ROOT / "scripts" / "check_drift.py")
assert _spec is not None and _spec.loader is not None
check_drift = importlib.util.module_from_spec(_spec)
sys.modules["check_drift"] = check_drift
_spec.loader.exec_module(check_drift)


class TestLastDigitTolerance:
    """One unit in the last digit printed, whatever that digit's place value is."""

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("413,470", 1.0),  # thousands separators do not change the last place
            ("81", 1.0),
            ("83.604", 0.001),  # the rate column, three decimals
            ("1,215.9", 0.1),
            ("0.00", 0.01),
        ],
    )
    def test_place_value(self, token: str, expected: float) -> None:
        assert check_drift.last_digit_tolerance(token) == pytest.approx(expected)


class TestTolerantColumns:
    """Where drift was measured, one digit passes and two do not."""

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("413,470", "413,471"),  # windows-latest produced exactly this
            ("413,470", "413,469"),  # ubuntu-latest produced exactly this
            ("83.604", "83.605"),
            ("+29,118", "+29,119"),  # §4's plane, signed
            ("(+111,091)", "(+111,092)"),  # §4's plane, parenthesised
            ("INR 413,432", "INR 413,433"),
        ],
    )
    def test_one_digit_passes(self, old: str, new: str) -> None:
        ok, why = check_drift.numbers_agree(old, new, tolerant=True)
        assert ok, why

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("413,470", "413,472"),  # two units: not the measured drift
            ("83.604", "83.606"),
            ("413,470", "413,500"),
            ("+29,118", "+29,120"),
        ],
    )
    def test_two_digits_fail(self, old: str, new: str) -> None:
        ok, why = check_drift.numbers_agree(old, new, tolerant=True)
        assert not ok
        assert "more than one digit" in why

    def test_shape_change_fails_even_within_tolerance(self) -> None:
        """A cell may drift. It may not stop being the same kind of cell."""
        ok, why = check_drift.numbers_agree("INR 413,432", "413,433", tolerant=True)
        assert not ok
        assert "shape changed" in why


class TestExactColumns:
    """Outside the named columns nothing moves at all, however small the move."""

    @pytest.mark.parametrize(
        ("old", "new"),
        [
            ("81", "82"),  # P4's net value -- the number the project's claim is
            ("**+7.42%**", "**+7.43%**"),  # the Adyen comparison
            ("1,215.9", "1,216.0"),
            ("16,236", "16,237"),
        ],
    )
    def test_any_change_fails(self, old: str, new: str) -> None:
        ok, why = check_drift.numbers_agree(old, new, tolerant=False)
        assert not ok
        assert "exact column changed" in why

    def test_identical_passes_either_way(self) -> None:
        for tolerant in (True, False):
            ok, _ = check_drift.numbers_agree("413,470", "413,470", tolerant=tolerant)
            assert ok


class TestSweepColumnsAreRecognised:
    """§4's plane heads its columns with numbers, and every cell under one is a rupee
    total. This is the case the first run of the real check missed."""

    @pytest.mark.parametrize("header", ["0.0005", "0.0060", "0.0250", "1.00"])
    def test_numeric_header_is_a_sweep_axis(self, header: str) -> None:
        assert check_drift.NUMBER.fullmatch(header) is not None

    @pytest.mark.parametrize("header", ["arm", "asks", "net value (inr)", "uplift \\ backfire"])
    def test_named_header_is_not(self, header: str) -> None:
        assert check_drift.NUMBER.fullmatch(header) is None


class TestPngDimensions:
    """Bytes are not compared; the figure's shape is."""

    def test_reads_committed_charts(self) -> None:
        for name in ("sweeps.png", "segments.png"):
            width, height = check_drift.png_size((ROOT / "docs" / "img" / name).read_bytes())
            assert width > 0 and height > 0

    def test_rejects_a_non_png(self) -> None:
        with pytest.raises(ValueError, match="not a PNG"):
            check_drift.png_size(b"not an image at all")


class TestTableParsing:
    def test_separator_is_not_a_data_row(self) -> None:
        assert check_drift.is_separator("|---|---:|---|")
        assert not check_drift.is_separator("| P4 | 1,215.9 |")

    def test_cells_are_stripped(self) -> None:
        assert check_drift.split_row("| P4 | 1,215.9 | 0.00 |") == ["P4", "1,215.9", "0.00"]

    def test_crlf_does_not_read_as_a_change(self) -> None:
        """The committed files were written on Windows. Line endings are not drift."""
        assert check_drift.read_lines("a\r\nb\r\n") == check_drift.read_lines("a\nb\n")


class TestArtifactScope:
    """Only what repro regenerates is examined. A source file edited in the working tree
    is not drift -- a distinction CI would never have surfaced, because CI's tree is
    clean."""

    def test_the_four_artifacts_are_the_scope(self) -> None:
        expected = {
            "docs/results.md",
            "docs/img/sweeps.png",
            "docs/img/segments.png",
            "docs/llm_eval.md",
        }
        assert expected == check_drift.ARTIFACTS

    def test_every_relaxed_file_is_an_artifact(self) -> None:
        """A file cannot be given an allowance without being in scope to be checked."""
        relaxed = check_drift.CELL_CHECKED | check_drift.DIMENSION_CHECKED
        assert relaxed <= check_drift.ARTIFACTS

    def test_llm_eval_has_no_allowance(self) -> None:
        assert "docs/llm_eval.md" not in check_drift.CELL_CHECKED
        assert "docs/llm_eval.md" not in check_drift.DIMENSION_CHECKED
