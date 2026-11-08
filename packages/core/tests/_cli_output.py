"""Read what a CLI command said, without how the terminal was asked to draw it."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from typer.testing import Result

_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_PANEL = re.compile(r"[─-╿]")


def plain(result: Result) -> str:
    """The words of a command's output.

    A usage error goes through Typer's rich panel, which colours option names when the run looks
    like a terminal that wants colour (CI does: FORCE_COLOR, GITHUB_ACTIONS), puts box drawing
    around the text and wraps it at the console width. Any of the three splits ``--database-url``
    out of a plain substring check. These tests are about the words, so they read the words.
    """
    text = _PANEL.sub(" ", _ANSI.sub("", result.output))
    return " ".join(text.split())
