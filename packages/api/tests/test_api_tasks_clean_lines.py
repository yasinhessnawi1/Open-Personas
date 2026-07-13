"""R11-B2 rider pins — the tasks route flattens stored transcripts at read time.

Second surface of the same dump (owner screenshots, 2026-07-13): the task
detail's report card and checkpoint "progress so far"/"next" lines rendered a
reminder task's FULL delivery message verbatim — ``{{#warm}}`` voice-markup,
``**markdown**``, paragraphs, the internal schedule id — where A6-D-4 promises
progress + next-step SUMMARIES, never raw transcripts. `_clean*` in
`persona_api.routes.tasks` is the read seam every serialized line passes
through (stored rows stay verbose; surfaces read one calm line).
"""

from __future__ import annotations

from persona_api.routes.tasks import _clean, _clean_list, _clean_opt
from persona_api.textline import DETAIL_BUDGET, full_text

DUMP = (
    "{{#warm}} Hi there — it's your scheduled check-in. **Reminder: Stretch for "
    "5 minutes.** Here's a simple routine: 1. **Neck rolls** — 30 seconds. 2. "
    "**Shoulder shrugs** — 30 seconds. 3. **Seated spinal twist** — 30 seconds "
    "each side. 4. **Chest opener** — 30 seconds. 5. **Standing forward fold** "
    "— 60 seconds. 6. **Wrist and ankle circles** — 30 seconds each.\n\n"
    "Reminder delivered. No decision required; this is a standing nudge that "
    "fires on schedule sched-7d939ffda6b2a15229ace8b784a88ca1."
)


def test_clean_flattens_the_dump_to_one_detail_line() -> None:
    out = _clean(DUMP)
    assert "{{" not in out
    assert "**" not in out
    assert "\n" not in out
    assert len(out) <= DETAIL_BUDGET + 1
    assert out.endswith("…")


def test_clean_opt_passes_none_through() -> None:
    assert _clean_opt(None) is None
    assert _clean_opt("**bold** step") == "bold step"


def test_clean_list_flattens_every_conclusion() -> None:
    out = _clean_list(["{{#warm}} one", "two\nlines"])
    assert out == ["one", "two lines"]


# owner-ruled (2026-07-13): the terminal report keeps every word — clean, never clip.
def test_full_text_never_clips_the_report() -> None:
    out = full_text(DUMP)
    assert "{{" not in out
    assert "**" not in out
    assert not out.endswith("…")
    assert "sched-7d939ffda6b2a15229ace8b784a88ca1" in out  # the tail survives
    assert "Wrist and ankle circles" in out


def test_full_text_keeps_paragraph_breaks() -> None:
    assert full_text("para one.\n\n\n\npara **two**.") == "para one.\n\npara two."
