"""The confabulation safety net — a schedule-claim mismatch detector (Spec A10, A10-D-4).

**Role: safety net, NOT the primary gate.** The primary honesty mechanism is structural
grounding, already in the loop: every legitimate schedule voice is emission-coupled — the
confirm branch speaks ``_CONTRACT_CONFIRMED_TEXT`` in the same act that emits
``task_originated`` (and A4-D-X guarantees the async create either succeeds or surfaces an
un-suppressible failure account). The ordinary-generation path, by construction, cannot
create a schedule (no chat tool can; every creating branch early-returns before
generation) — so when this seam runs, ``created == False`` is already structurally known.
This module never decides whether a schedule was created; it only detects that the model's
free text **voiced a new-schedule success claim in the ¬created state** (the
``claimed ∧ ¬created`` mismatch) so the loop can append a deterministic, honest,
actionable correction.

**Posture (precision-first — the deliberate inverse of the A4 cue net):**

* A **false positive** appends a truthful-but-awkward disclaimer to a reply that didn't
  really claim a new schedule — user-visible noise, and *wrong* if the persona was
  truthfully describing an EXISTING schedule (introspection). So the lexicon targets
  new-ACTION claims and promises ("I've scheduled…", "I'll remind you every…") and
  excludes STATE descriptions ("is already set up", "runs daily at 9"); a match inside a
  clearly *proposal-shaped* sentence ("if you confirm, I'll remind you every morning…")
  is rejected. The probe suite (positives + negatives incl. the introspection-truth and
  proposal cases) is the unit gate.
* A **false negative** (a novel phrasing slips) costs one uncorrected turn — the pre-A10
  status quo, mitigated at the source by frontier chat routing and by the direct calendar
  door existing. The lexicon is versioned data, tunable without a code change.

The detector is the third post-generation lexical instance (after Spec 25's refusal
detector and Spec 26/27's tool/MCP-gap detectors) — pure functions, no I/O, one loop seam.
The correction text is a versioned deterministic artifact (the ``_CONTRACT_CONFIRMED_TEXT``
voice class): honest, and actionable — it names BOTH real doors (the calendar's New
reminder, and the explicit confirm flow).
"""

from __future__ import annotations

import re

from pydantic import BaseModel, ConfigDict

__all__ = [
    "SCHEDULE_CLAIM_LEXICON_VERSION",
    "SCHEDULE_CORRECTION_VERSION",
    "ScheduleClaimSignal",
    "detect_schedule_claim",
    "render_schedule_correction",
]

#: Bump when the claim lexicon changes (telemetry/probe-suite anchoring).
SCHEDULE_CLAIM_LEXICON_VERSION = "1.0"
#: Bump when the correction wording changes (Spec 10 prompt-artifact discipline).
SCHEDULE_CORRECTION_VERSION = "1.0"


class ScheduleClaimSignal(BaseModel):
    """A detected new-schedule success claim (the matched phrase, for telemetry/tests)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    matched: str


# New-ACTION claims and promises only — never state descriptions. Latin patterns use
# word-ish boundaries; Arabic is substring-matched (the cue-net convention). Precision
# over recall: each pattern should be very hard to say WITHOUT claiming a new schedule.
_CLAIM_PATTERNS: tuple[str, ...] = (
    # --- English: perfect-tense creation claims. The object must be determiner/pronoun/
    # cadence-shaped ("scheduled it/the check/daily …") so noun-phrase collisions like
    # "I have scheduled maintenance knowledge" stay out (precision-first).
    r"\bi(?:'ve| have)\s+(?:now\s+)?scheduled\s+"
    r"(?:it|that|this|one|a|an|the|your|daily|weekly|monthly|hourly)\b",
    r"\bi(?:'ve| have)\s+(?:now\s+)?set\s+(?:that|this|it)\s+up\b",
    r"\bi(?:'ve| have)\s+(?:added|put)\s+(?:that|this|it|one|a\s+reminder)\s+"
    r"(?:to|on|in)\s+your\s+(?:schedule|calendar)\b",
    r"\bi(?:'ve| have)\s+(?:created|set)\s+(?:a|the|your)\s+"
    r"(?:reminder|schedule|recurring\s+\w+)\b",
    r"\bconsider\s+it\s+scheduled\b",
    r"\byour\s+(?:reminder|schedule)\s+has\s+been\s+(?:set|created|added)\b",
    # --- English: recurring-promise claims (a cadence promised as a done deal) ---
    r"\bi(?:'ll| will)\s+remind\s+you\s+every\b",
    r"\bi(?:'ll| will)\s+(?:send|give)\s+you\s+(?:a\s+)?"
    r"(?:daily|weekly|monthly|hourly)\s+\w+",
    r"\bi(?:'ll| will)\s+(?:check\s+in|update\s+you)\s+every\b",
    # --- Norwegian ---
    r"\bjeg\s+har\s+(?:lagt|satt)\s+(?:det|den|dette|opp|inn)\b",
    r"\bjeg\s+har\s+(?:planlagt|opprettet)\b",
    r"\bjeg\s+(?:skal|kommer\s+til\s+å)\s+minne\s+deg\b",
    # --- Arabic (substring; the cue-net convention) ---
    r"لقد\s*(?:جدولت|أضفت|أنشأت)",
    r"قمت\s*بجدولة",
    r"سأذكرك\s*كل",
)

_COMPILED: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p, re.IGNORECASE | re.UNICODE) for p in _CLAIM_PATTERNS
)

# A match inside a proposal-shaped sentence is an OFFER, not a claim — reject it
# (precision-first; the confirm flow is exactly where such sentences legitimately live).
_PROPOSAL_MARKERS: tuple[str, ...] = (
    "if you",
    "once you",
    "when you confirm",
    "want me to",
    "would you like",
    "shall i",
    "should i",
    "can i",
    "hvis du",
    "vil du",
    "skal jeg",
    "ønsker du",
    "هل تريد",
    "إذا أكدت",
    "?",
)

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?؟\n])\s+")


def detect_schedule_claim(model_output: str) -> ScheduleClaimSignal | None:
    """Detect a new-schedule success claim in the model's final text (pure, no I/O).

    Returns the first matched claim, or ``None``. A claim whose containing sentence is
    proposal-shaped (conditional/offering) is NOT a claim — the model is allowed to
    *offer* to schedule; it is not allowed to say it *did*.
    """
    if not model_output:
        return None
    for sentence in _SENTENCE_SPLIT.split(model_output):
        lowered = sentence.lower()
        if any(marker in lowered for marker in _PROPOSAL_MARKERS):
            continue
        for pattern in _COMPILED:
            match = pattern.search(sentence)
            if match is not None:
                return ScheduleClaimSignal(matched=match.group(0).strip())
    return None


def render_schedule_correction() -> str:
    """The deterministic, honest, ACTIONABLE correction (version 1.0).

    Appended by the LOOP (never requested from the model) when a claim fired in the
    ¬created state. Names both real doors: the calendar's direct create surface and the
    explicit confirm flow — honest about what did NOT happen, concrete about what to do.
    """
    return (
        "To be clear — I haven't actually created a schedule just now. "
        "You can set one up yourself from your calendar (Schedule → New reminder), "
        "or tell me exactly what to schedule and when, and I'll set it up once you confirm."
    )
