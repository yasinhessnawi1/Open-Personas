"""The recovery hints a refused path carries, word for word (Spec WIN T5).

The model reads these hints in the tool result and the user may see them, so they
follow the product's writing rule: no em or en dashes. The three that had one are
pinned exactly; every hint is checked for dashes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.errors import SandboxViolationError
from persona.tools import _sandbox
from persona.tools._sandbox import resolve_sandbox_path

if TYPE_CHECKING:
    from pathlib import Path

ABSOLUTE = (
    "Paths must be RELATIVE to the working directory and stay inside it, "
    "e.g. 'out/report.md'. Do not use an absolute path (a leading '/') or '..'."
)
ESCAPE = (
    "Paths must be RELATIVE to the working directory and stay inside it, "
    "e.g. 'out/report.md'. Do not use '..' or any path that climbs out of "
    "the working directory."
)
NO_SCOPE = (
    "No workspace is bound for this request, so file access is unavailable "
    "right now. Changing the path will not help; do not retry."
)


@pytest.mark.parametrize(
    ("reason", "expected"),
    [("absolute", ABSOLUTE), ("escape", ESCAPE), ("no_scope", NO_SCOPE)],
)
def test_the_hint_reads_exactly(reason: str, expected: str) -> None:
    assert _sandbox._correction_for(reason) == expected


def test_no_hint_has_an_em_or_en_dash() -> None:
    hints = [*_sandbox._CORRECTION_BY_REASON.values(), _sandbox._DEFAULT_CORRECTION]
    offenders = [hint for hint in hints if "\u2014" in hint or "\u2013" in hint]
    assert offenders == []


def test_a_climbing_path_carries_the_escape_hint(tmp_path: Path) -> None:
    with pytest.raises(SandboxViolationError) as caught:
        resolve_sandbox_path(tmp_path, "../outside.md")
    assert f"; {ESCAPE}" in str(caught.value)
