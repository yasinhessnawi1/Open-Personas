"""Sandbox error-UX tests: every violation carries a reason-keyed correction.

R9-007 (refining T10 / D-25-5 / spec §2.5, acceptance criteria 4 + 8). The
original ``SandboxViolationError`` messages stated only what was WRONG; a model
that hit one had no way to construct a path that WOULD work and often retried
the same escaping shape — burning steps and model spend. A first pass added a
single generic "use out/report.md" hint to every reason, but that same hint
fired for cases it did not fit (e.g. it never told the model to swap backslashes
for forward slashes, or to name a file rather than the directory). Each of the
7 path-validation raise sites (null_byte, too_long, mixed_separators, empty,
absolute, root_reference, escape) now appends an ACTIONABLE, imperative
correction keyed to *why* the path was rejected, so the model can recover.

These tests assert per reason:
1. the message carries the reason-specific corrective phrase(s), and
2. the message names the reason discriminator (``[reason=X]``),
while the structured ``context`` dict (reason + preview) is left intact.

The final tests exercise the pass-through: a ``file_write`` / ``file_read``
call with a bad path returns a ToolResult whose ``content`` includes the keyed
correction, and a "followable correction" test proves the hint leads somewhere
valid — absolute path (error + hint) then a relative in-sandbox path (success).
"""

# ruff: noqa: ANN401, ARG001, ARG002, ERA001
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.errors import SandboxViolationError
from persona.tools._sandbox import resolve_sandbox_path
from persona.tools.builtin.file_read import make_file_read_tool
from persona.tools.builtin.file_write import make_file_write_tool

if TYPE_CHECKING:
    from pathlib import Path

# One representative bad input per reason. Each MUST trip exactly the reason
# named in the second tuple element (verified via context["reason"]).
_REASON_CASES: list[tuple[str, str]] = [
    ("\x00", "null_byte"),
    ("a" * (4096 + 1), "too_long"),
    ("a\\b\\c", "mixed_separators"),
    ("   ", "empty"),
    ("/workspace/out/report.md", "absolute"),
    (".", "root_reference"),
    ("../escape.txt", "escape"),
]

# Reason -> substrings that MUST appear in the corrective message. These are the
# actionable phrases the model needs to recover from THAT specific failure.
_EXPECTED_PHRASES: dict[str, list[str]] = {
    "null_byte": ["Remove control", "out/report.md"],
    "too_long": ["Shorten the path", "4096"],
    "mixed_separators": ["forward slashes", "backslashes"],
    "empty": ["non-empty relative filename", "out/report.md"],
    "absolute": ["RELATIVE", "out/report.md", "absolute path"],
    "root_reference": ["Provide a filename", "not the directory itself"],
    "escape": ["RELATIVE", "out/report.md", ".."],
}


class TestReasonKeyedCorrection:
    """Every raise site enriches its message with a reason-specific correction."""

    @pytest.mark.parametrize(("bad", "reason"), _REASON_CASES)
    def test_message_contains_actionable_correction(
        self, tmp_path: Path, bad: str, reason: str
    ) -> None:
        with pytest.raises(SandboxViolationError) as exc_info:
            resolve_sandbox_path(tmp_path, bad)
        msg = str(exc_info.value)
        for phrase in _EXPECTED_PHRASES[reason]:
            assert phrase in msg, f"reason={reason} missing corrective phrase {phrase!r} in {msg!r}"

    @pytest.mark.parametrize(("bad", "reason"), _REASON_CASES)
    def test_message_names_the_reason(self, tmp_path: Path, bad: str, reason: str) -> None:
        with pytest.raises(SandboxViolationError) as exc_info:
            resolve_sandbox_path(tmp_path, bad)
        assert f"[reason={reason}]" in str(exc_info.value)

    @pytest.mark.parametrize(("bad", "reason"), _REASON_CASES)
    def test_context_dict_shape_preserved(self, tmp_path: Path, bad: str, reason: str) -> None:
        # The enrichment touches only the human-readable message string; the
        # structured context (reason + preview-style fields) is unchanged.
        with pytest.raises(SandboxViolationError) as exc_info:
            resolve_sandbox_path(tmp_path, bad)
        assert exc_info.value.context.get("reason") == reason

    def test_corrections_are_distinct_per_reason(self, tmp_path: Path) -> None:
        # The whole point of R9-007: the guidance is keyed to the reason, so
        # different failures yield different corrective text (not one generic
        # hint). We read ``args[0]`` (the raw message) and strip the shared
        # ``<summary> [reason=X]; `` prefix to compare only the correction half.
        corrections: dict[str, str] = {}
        for bad, reason in _REASON_CASES:
            with pytest.raises(SandboxViolationError) as exc_info:
                resolve_sandbox_path(tmp_path, bad)
            raw = exc_info.value.args[0]
            corrections[reason] = raw.split("]; ", 1)[1]
        # At minimum, the reasons with materially different fixes must differ.
        assert corrections["mixed_separators"] != corrections["absolute"]
        assert corrections["root_reference"] != corrections["absolute"]
        assert corrections["too_long"] != corrections["escape"]
        # And the distinct corrective phrasings really are distinct values.
        assert len(set(corrections.values())) >= 5


class TestToolsSurfaceCorrection:
    """The enriched message reaches the model via the ToolResult content."""

    @pytest.mark.asyncio
    async def test_file_write_absolute_path_error_includes_correction(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(
            path="/workspace/out/startup_launch_funnel.md", content="x"
        )
        assert result.is_error is True
        assert "out/report.md" in result.content
        assert "RELATIVE" in result.content
        assert "[reason=absolute]" in result.content

    @pytest.mark.asyncio
    async def test_file_read_mixed_separators_error_includes_correction(
        self, tmp_path: Path
    ) -> None:
        tool_inst = make_file_read_tool(sandbox_root=tmp_path)
        result = await tool_inst.execute(path="a\\b\\c")
        assert result.is_error is True
        # file_read shares the resolver, so it gets the same reason-keyed hint.
        assert "forward slashes" in result.content
        assert "[reason=mixed_separators]" in result.content


class TestFollowableCorrection:
    """Following the correction leads to a valid path (the hint goes somewhere)."""

    @pytest.mark.asyncio
    async def test_absolute_then_relative_in_sandbox_succeeds(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)

        # 1. The model tries an absolute path — rejected, but told the fix.
        rejected = await tool_inst.execute(path="/etc/report.md", content="hello")
        assert rejected.is_error is True
        assert "out/report.md" in rejected.content
        assert "[reason=absolute]" in rejected.content

        # 2. It follows the correction verbatim — a relative in-sandbox path.
        accepted = await tool_inst.execute(path="out/report.md", content="hello")
        assert accepted.is_error is False
        # The corrected path actually landed inside the sandbox root.
        assert (tmp_path / "out" / "report.md").read_text() == "hello"

    @pytest.mark.asyncio
    async def test_root_reference_then_named_file_succeeds(self, tmp_path: Path) -> None:
        tool_inst = make_file_write_tool(sandbox_root=tmp_path)

        rejected = await tool_inst.execute(path=".", content="data")
        assert rejected.is_error is True
        assert "Provide a filename" in rejected.content
        assert "[reason=root_reference]" in rejected.content

        # Following it: name a file instead of the directory.
        accepted = await tool_inst.execute(path="notes.md", content="data")
        assert accepted.is_error is False
        assert (tmp_path / "notes.md").read_text() == "data"
