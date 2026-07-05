"""A10 T4 — the schedule-claim lexicon probe suite (the precision gate, A10-D-4).

The detector is the SAFETY NET over the claimed∧¬created mismatch (grounding is the
primary gate, in the loop's structure); this suite pins its precision-first posture:
new-ACTION claims and promises fire; STATE descriptions (introspection truth), proposals,
offers, and questions never do. EN + NO + AR both directions.
"""

from __future__ import annotations

import pytest
from persona_runtime.schedule_claim import (
    SCHEDULE_CLAIM_LEXICON_VERSION,
    SCHEDULE_CORRECTION_VERSION,
    detect_schedule_claim,
    render_schedule_correction,
)

# --- positives: new-action claims the model cannot truthfully make in ordinary chat -----

_POSITIVES = [
    "I've scheduled it for you.",
    "Done! I have now scheduled the daily check.",
    "I've set that up. You're all good.",
    # The deterministic confirm voice is a claim BY DESIGN: in ordinary generation it
    # would be false (the real confirm branch early-returns and never reaches the seam).
    "Done — I've set that up. I'll keep you posted.",
    "I've added it to your calendar.",
    "I've added a reminder to your schedule.",
    "I've created a reminder for every morning.",
    "Consider it scheduled.",
    "Your reminder has been set.",
    "Great — I'll remind you every morning at 9.",
    "I'll send you a daily update on this.",
    "I'll check in every evening from now on.",
    # Norwegian
    "Jeg har lagt det inn i kalenderen din.",
    "Jeg har satt opp en daglig påminnelse.",
    "Jeg har planlagt den daglige oppdateringen.",
    "Jeg skal minne deg på det hver morgen.",
    # Arabic
    "لقد جدولت التذكير اليومي لك.",
    "قمت بجدولة تحديث يومي.",
    "سأذكرك كل صباح بهذا.",
]


@pytest.mark.parametrize("text", _POSITIVES)
def test_new_action_claims_fire(text: str) -> None:
    assert detect_schedule_claim(text) is not None, f"should fire: {text!r}"


# --- negatives: truth, offers, proposals, questions — never corrected -------------------

_NEGATIVES = [
    # State descriptions — the introspection tool truthfully describes EXISTING schedules.
    "Your reminder is already set up — it runs daily at 9 in your time.",
    "That schedule is active and will fire tomorrow morning.",
    "The task I set up runs weekly; the last check completed fine.",
    "It runs daily at 09:00, Europe/Oslo.",
    # Proposals / offers / questions — offering is allowed; claiming is not.
    "Want me to set that up as a daily reminder?",
    "If you confirm, I'll remind you every morning at 9.",
    "Shall I schedule a weekly summary for you?",
    "I can set that up — should I make it daily or weekly?",
    "Once you confirm, I'll add it to your calendar.",
    "Vil du at jeg skal sette opp en påminnelse?",
    "Hvis du bekrefter, skal jeg minne deg på det hver morgen.",
    "هل تريد أن أجدول تذكيرا يوميا؟",
    # Ordinary chat — nothing schedule-shaped.
    "Here's the summary you asked for.",
    "The weather in Oslo is mild today.",
    "I have scheduled maintenance knowledge: databases often reindex nightly.",
    "",
]


@pytest.mark.parametrize("text", _NEGATIVES)
def test_truth_offers_and_ordinary_text_never_fire(text: str) -> None:
    assert detect_schedule_claim(text) is None, f"should NOT fire: {text!r}"


def test_claim_in_one_sentence_not_masked_by_a_question_elsewhere() -> None:
    """The proposal exclusion is per-SENTENCE — a real claim beside a question still fires."""
    text = "I've scheduled the daily check. Anything else you'd like?"
    assert detect_schedule_claim(text) is not None


def test_correction_is_versioned_honest_and_actionable() -> None:
    """The correction names BOTH doors (calendar create + explicit confirm) — actionable."""
    correction = render_schedule_correction()
    assert "haven't actually created a schedule" in correction
    assert "New reminder" in correction  # door 1: the calendar's direct create surface
    assert "once you confirm" in correction  # door 2: the explicit confirm flow
    assert SCHEDULE_CLAIM_LEXICON_VERSION == "1.0"
    assert SCHEDULE_CORRECTION_VERSION == "1.0"
