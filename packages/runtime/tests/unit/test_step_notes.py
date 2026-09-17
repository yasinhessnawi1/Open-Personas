"""``Step.notes`` is a discriminated union, and the origination note is a member of it.

The record is what a reopened run is rebuilt from, so a note has to survive the JSON round
trip with its ``kind`` intact and never admit another kind's fields.
"""

from __future__ import annotations

import pytest
from persona_runtime.agentic.step import (
    CallSkippedNote,
    PersonaOriginatedNote,
    Step,
    StepType,
)
from pydantic import ValidationError


def test_persona_originated_note_round_trips_on_the_final_step() -> None:
    step = Step(
        type=StepType.FINAL,
        content="Done.",
        notes=[
            CallSkippedNote(tool="web_search", guard="cached_read"),
            PersonaOriginatedNote(conversation_id="conv_1"),
        ],
    )

    rebuilt = Step.model_validate(step.model_dump(mode="json"))

    assert rebuilt == step
    assert rebuilt.model_dump(mode="json")["notes"][-1] == {
        "kind": "persona_originated",
        "conversation_id": "conv_1",
    }


def test_persona_originated_note_needs_the_conversation_it_landed_in() -> None:
    with pytest.raises(ValidationError):
        Step.model_validate(
            {"type": "final", "content": "Done.", "notes": [{"kind": "persona_originated"}]}
        )


def test_a_note_cannot_borrow_another_kinds_fields() -> None:
    with pytest.raises(ValidationError):
        Step.model_validate(
            {
                "type": "final",
                "notes": [{"kind": "persona_originated", "tool": "web_search"}],
            }
        )
