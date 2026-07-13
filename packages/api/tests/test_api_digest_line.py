"""R11-B2 digest-line sanitizer pins (`persona_api.digest.builder._line`).

The owner's screenshot (2026-07-13): a "Done overnight" row dumped a task's full
delivery message verbatim — ``{{#warm}}`` voice-markup, ``**markdown**``
emphasis, embedded newlines, and a trailing internal schedule id — where the
A6-R-1 contract promises ONE calm line. `_line` is the seam every DigestItem
title/detail passes through; these pins keep the dump from coming back. The
same lines feed C0's morning message (`render_digest_message`), so the fix is
made here, not in the web renderer.
"""

from __future__ import annotations

from persona_api.digest.builder import _LINE_BUDGET, _line


def test_strips_voice_markup_tags() -> None:
    assert _line("{{#warm}} Hi there {{/warm}} — ready.") == "Hi there — ready."


def test_strips_markdown_emphasis_keeps_words() -> None:
    assert _line("**Reminder: Stretch.** Use `neck rolls` __daily__.") == (
        "Reminder: Stretch. Use neck rolls daily."
    )


def test_collapses_newlines_and_runs_of_whitespace() -> None:
    assert _line("line one\n\nline   two\tend") == "line one line two end"


def test_truncates_at_a_word_boundary_with_ellipsis() -> None:
    text = "word " * 100
    out = _line(text)
    assert len(out) <= _LINE_BUDGET + 1  # budget + the ellipsis
    assert out.endswith("…")
    assert " wor…" not in out  # never mid-word


def test_short_clean_text_passes_through_unchanged() -> None:
    assert _line("Filed 8 receipts into the tax folder.") == (
        "Filed 8 receipts into the tax folder."
    )


def test_the_screenshot_dump_becomes_one_calm_line() -> None:
    dump = (
        "{{#warm}} Hi there — it's your scheduled check-in. **Reminder: Stretch "
        "for 5 minutes.** You've been at it for a while. Five minutes is all it "
        "takes. Here's a simple routine you can do right now, seated or "
        "standing: 1. **Neck rolls** — 30 seconds. Slow, both directions. 2. "
        "**Shoulder shrugs** — 30 seconds.\n\nReminder delivered: stretch for 5 "
        "minutes. No decision required; this is a standing nudge that fires on "
        "schedule sched-7d939ffda6b2a15229ace8b784a88ca1."
    )
    out = _line(dump)
    assert "{{" not in out
    assert "**" not in out
    assert "\n" not in out
    assert len(out) <= _LINE_BUDGET + 1
    assert "sched-7d939ffda" not in out  # the internal id rides the truncated tail
