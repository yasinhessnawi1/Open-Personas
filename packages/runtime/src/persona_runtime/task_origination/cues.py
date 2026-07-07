"""The standing-intent cue net — the cheap, high-recall, non-deciding trigger (Spec A4, T4).

Step 1 of the A4-D-2 two-step. A pure lexical detector that flags whether a message
*plausibly* carries standing intent ("every morning…", "keep an eye on…", "over the next
week…", "remind me to…") across the user base's languages (Norwegian / Arabic / English,
A4-R-4). It is deliberately **high-recall and precision-light**: a cue only *admits* the
turn to the model's standing-vs-now judgment (step 2, :mod:`recognizer`); it never decides.

The discipline (per the crisis-gate lesson): the lexical layer must not be the precision
layer. A **missed** cue is a silent failure — a standing intent that is never recognised and
so never offered as a task. A **false-admit** is cheap — the model rejects it in step 2 and
the turn proceeds as ordinary chat. So the net errs toward firing: it matches the union of
all three languages' markers regardless of the persona's default language (a Norwegian
persona may well receive an English message).

This module decides nothing about whether a task is created — that is the model's judgment
(step 2) plus the explicit user confirmation (T6). It only makes the common, cue-free turn
cheap (no model call) while never letting a plausible standing intent slip past unconsidered.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

__all__ = ["CueSignal", "detect_standing_cue"]


class CueSignal(BaseModel):
    """A fired standing-intent cue — the matched marker + its category (for telemetry/tests)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: str
    marker: str


# Each entry: (category, pattern). Patterns are matched case-insensitively over the raw
# message (Unicode-aware). Latin markers use word-ish boundaries to avoid matching inside
# longer words; Arabic markers are substring-matched (Arabic word boundaries are unreliable
# under ``\b``, and recall is the goal). Coverage is intentionally broad — the model is the
# precision layer.
_CUE_PATTERNS: tuple[tuple[str, str], ...] = (
    # --- recurrence (English) ---
    (
        "recurrence",
        r"\bevery\s+(morning|day|week|month|hour|night|evening|monday|tuesday|"
        r"wednesday|thursday|friday|saturday|sunday|weekday|other\s+day)\b",
    ),
    ("recurrence", r"\beach\s+(morning|day|week|month|time)\b"),
    ("recurrence", r"\b(daily|weekly|monthly|hourly|nightly)\b"),
    ("recurrence", r"\b(recurring|recurrent|on\s+a\s+schedule|on\s+a\s+regular\s+basis)\b"),
    # Numeric intervals ("every 15 min", "every 2 hours") — found missing in the R4
    # operator pass: "schedule a task every 15 min" fell through to a model refusal.
    (
        "recurrence",
        r"\bevery\s+\d+\s*(minutes?|mins?|min|hours?|hrs?|hr|days?|weeks?|months?)\b",
    ),
    # The explicit schedule-verb ask ("schedule a task/reminder/check…"). Generous by
    # design — a false positive costs one small-tier judge call (the precision layer).
    (
        "recurrence",
        r"\bschedul(e|ing)\b.{0,40}\b(task|reminder|check|report|summary|message|call)\b",
    ),
    # --- ongoing / monitoring (English) ---
    (
        "ongoing",
        r"\bkeep\s+(watching|an\s+eye|me\s+posted|me\s+updated|tracking|monitoring|checking)\b",
    ),
    ("ongoing", r"\b(monitor|watch\s+for|keep\s+track|stay\s+on\s+top\s+of)\b"),
    ("ongoing", r"\b(from\s+now\s+on|going\s+forward|ongoing|continuously|in\s+the\s+future)\b"),
    # --- spanning (English) ---
    ("spanning", r"\bover\s+the\s+(next\s+)?(week|month|coming\s+(week|month|days))\b"),
    ("spanning", r"\b(throughout|across)\s+the\s+(week|month|day)\b"),
    # --- remind (English) ---
    ("remind", r"\bremind\s+me\b"),
    # --- recurrence (Norwegian) ---
    (
        "recurrence",
        r"\bhver\s+(morgen|dag|uke|måned|kveld|natt|mandag|tirsdag|onsdag|"
        r"torsdag|fredag|lørdag|søndag)\b",
    ),
    ("recurrence", r"\b(daglig|ukentlig|månedlig|hver\s+gang)\b"),
    # Norwegian numeric intervals ("hvert 15. minutt", "hver 2 timer", "hvert kvarter").
    ("recurrence", r"\bhver(t)?\s+\d+\.?\s*(minutt(er)?|min|time(r)?|dag(er)?|uke(r)?)\b"),
    ("recurrence", r"\bhvert\s+kvarter\b"),
    # --- ongoing / monitoring (Norwegian) ---
    ("ongoing", r"\b(følg\s+med|hold\s+øye\s+med|hold\s+meg\s+oppdatert|overvåk|følg\s+opp)\b"),
    ("ongoing", r"\b(fremover|fra\s+nå\s+av|løpende|kontinuerlig)\b"),
    # --- spanning / remind (Norwegian) ---
    ("spanning", r"\b(i\s+løpet\s+av|gjennom)\s+(uken|måneden|dagen)\b"),
    ("remind", r"\b(minn\s+meg|påminn\s+meg)\b"),
    # --- Arabic (substring; recurrence / ongoing / remind) ---
    ("recurrence", r"كل\s*(يوم|صباح|أسبوع|شهر|مساء|ليلة)"),
    ("recurrence", r"(يوميا|أسبوعيا|شهريا|كل\s*مرة)"),
    ("ongoing", r"(تابع|راقب|استمر\s*في|ابق\s*على\s*اطلاع)"),
    ("ongoing", r"(من\s*الآن|مستمر|باستمرار)"),
    ("remind", r"(ذكرني|ذكّرني)"),
)

_COMPILED: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (category, re.compile(pattern, re.IGNORECASE | re.UNICODE))
    for category, pattern in _CUE_PATTERNS
)


def detect_standing_cue(message: str) -> CueSignal | None:
    """Return the first standing-intent cue the message trips, or ``None`` (cheap path).

    Pure and side-effect-free. ``None`` means the turn carries no plausible standing cue and
    can skip the model judgment entirely; a :class:`CueSignal` admits it to step 2. High
    recall by design — a false-admit costs only the model's rejection, a miss costs an
    unrecognised standing intent.
    """
    for category, pattern in _COMPILED:
        match = pattern.search(message)
        if match is not None:
            return CueSignal(category=category, marker=match.group(0).strip())
    return None
