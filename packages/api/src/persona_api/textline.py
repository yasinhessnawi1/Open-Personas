"""One-line text sanitation for human-readable surfaces (R11-B2 rider).

Executors and personas write rich text — ``{{#warm}}`` voice-markup,
``**markdown**`` emphasis, multi-paragraph delivery messages, trailing internal
ids. Surfaces that promise a calm summary line (the morning digest's items,
A6-R-1; a task's progress/next-step projections, A6-D-4 "never raw
transcripts") flatten through THIS seam at read time, so already-stored rows
are covered and every writer stays free to be verbose in its durable record.

Strip markup markers (keep the words), collapse all whitespace, truncate at a
word boundary with an honest ellipsis.
"""

from __future__ import annotations

import re

__all__ = ["DETAIL_BUDGET", "LINE_BUDGET", "full_text", "one_line"]

#: Digest items — the under-a-minute read means ~a sentence per item.
LINE_BUDGET = 160
#: Task-detail projections (progress / next-step / causes) — a fuller summary
#: line, still never a dumped transcript.
DETAIL_BUDGET = 280

_VOICE_MARKUP_RE = re.compile(r"\{\{[^{}]*\}\}")
_MD_EMPHASIS_RE = re.compile(r"\*\*|__|`")
_WS_RE = re.compile(r"\s+")


def one_line(text: str, budget: int = LINE_BUDGET) -> str:
    """Collapse arbitrary persona/report text into one calm line."""
    cleaned = _WS_RE.sub(" ", _MD_EMPHASIS_RE.sub("", _VOICE_MARKUP_RE.sub(" ", text))).strip()
    if len(cleaned) <= budget:
        return cleaned
    cut = cleaned[:budget].rsplit(" ", 1)[0].rstrip(" ,;:—–-")
    return f"{cut}…"


_HWS_RE = re.compile(r"[ \t]+")
_PARA_RE = re.compile(r"\n{3,}")


def full_text(text: str) -> str:
    """Clean markup out of a FULL record without cutting a word (owner-ruled,
    R11-B2): the task's terminal report is the place the complete outcome is
    read, so it keeps every sentence — only the voice-markup/markdown markers
    go, horizontal whitespace collapses, and runaway blank lines settle to
    paragraph breaks. Summary projections (digest lines, checkpoints, causes)
    keep using :func:`one_line`."""
    cleaned = _MD_EMPHASIS_RE.sub("", _VOICE_MARKUP_RE.sub(" ", text))
    cleaned = _HWS_RE.sub(" ", cleaned)
    cleaned = _PARA_RE.sub("\n\n", cleaned)
    return cleaned.strip()
