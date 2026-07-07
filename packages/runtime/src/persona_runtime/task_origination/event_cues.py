"""The event-trigger cue net — the cheap, high-recall, non-deciding trigger for A7 (Spec A7, T8).

The A4 :func:`~persona_runtime.task_origination.cues.detect_standing_cue` analogue, for the OTHER
standing impulse: an *event* condition ("when an email from my landlord arrives, summarise it")
rather than a *clock* ("every morning…"). Step 1 of the same two-step (A4-D-2): a pure lexical
detector that flags whether a message *plausibly* asks the persona to watch for a platform event; it
only *admits* the turn to the model judge (:mod:`event_judge`), which decides and — conservatively —
extracts the typed filter. It NEVER decides, and it never guesses a filter.

Same discipline as the A4 cue net: high recall, precision-light. A **missed** cue is a silent
failure (a standing event-watch never offered); a **false-admit** is cheap (the judge returns
NOT_TRIGGER and the turn proceeds — possibly to the schedule cue, then to ordinary chat). So the net
errs toward firing, matching the union of the user base's languages (EN / NB / AR, A4-R-4).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

__all__ = ["EventCueSignal", "detect_event_cue"]


class EventCueSignal(BaseModel):
    """A fired event-trigger cue — the matched marker + its category (for telemetry/tests)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str
    marker: str


# Each entry: (category, pattern), matched case-insensitively (Unicode-aware). The markers are the
# event-condition phrasings — a temporal "when/whenever" bound to an inbound-message token, plus the
# strong standalone repeaters ("whenever", "every time"). Broad by design; the judge is precision.
_CUE_PATTERNS: tuple[tuple[str, str], ...] = (
    # --- strong standalone event repeaters (English) ---
    ("repeater", r"\b(whenever|any\s*time|every\s+time|each\s+time)\b"),
    # --- when + an inbound-message token (English) ---
    (
        "message",
        r"\bwhen\s+(an?\s+|a\s+new\s+)?(e-?mail|message|text|dm|sms|whatsapp|mail|"
        r"notification)\b",
    ),
    # --- when … arrives / is received / emails me (English) ---
    (
        "arrival",
        r"\bwhen\b.{0,50}\b(arrives?|comes?\s+in|lands?|shows?\s+up|is\s+received|"
        r"e-?mails?\s+me|texts?\s+me|messages?\s+me|writes?\s+to\s+me|contacts?\s+me)\b",
    ),
    # --- Norwegian ---
    ("repeater", r"\b(hver\s+gang|når\s+som\s+helst)\b"),
    ("message", r"\bnår\s+(en?\s+|et\s+)?(e-?post|melding|sms|tekstmelding|mail)\b"),
    ("arrival", r"\bnår\b.{0,50}\b(kommer|ankommer|mottas|sender\s+meg|mailer\s+meg)\b"),
    # --- Arabic (substring; when/whenever + message/arrival) ---
    ("repeater", r"(كلما|في\s*كل\s*مرة)"),
    ("message", r"عندما\s*.{0,30}(بريد|رسالة|إيميل)"),
    ("arrival", r"(عندما|حين)\s*.{0,30}(يصل|تصل|يرسل)"),
)

_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (category, re.compile(pattern, re.IGNORECASE | re.UNICODE))
    for category, pattern in _CUE_PATTERNS
)


def detect_event_cue(message: str) -> EventCueSignal | None:
    """Return the first event-trigger cue the message trips, or ``None`` (cheap path).

    Pure and side-effect-free. ``None`` means the turn carries no plausible event condition and can
    skip the model judgment; an :class:`EventCueSignal` admits it to the judge. High recall by
    design — a false-admit costs only the judge's rejection, a miss costs an unrecognised watch.
    """
    for category, pattern in _COMPILED:
        match = pattern.search(message)
        if match is not None:
            return EventCueSignal(category=category, marker=match.group(0).strip())
    return None
