"""R11-B2 pins — the A10 create door's ``intent`` field ("Schedule for later").

The Activity dialog schedules real WORK for later, not a nudge about it:
``intent="task"`` makes the subject the contract goal verbatim, while the
default stays the A10 reminder shape (backward-compatible — every existing
caller composes exactly what it did before).
"""

from __future__ import annotations

from persona_api.schemas.requests import ScheduleCreateRequest
from persona_api.services.schedule_create_service import _reminder_contract, _task_contract


def test_intent_defaults_to_reminder_backward_compatible() -> None:
    req = ScheduleCreateRequest(
        one_time_at="2027-01-01T10:00:00Z",  # type: ignore[arg-type] — pydantic parses
        timezone="Europe/Oslo",
        persona_id="p1",
        subject="stretch for 5 minutes",
        idempotency_key="k" * 12,
    )
    assert req.intent == "reminder"


def test_task_contract_keeps_the_goal_verbatim() -> None:
    contract = _task_contract("Draft the Q3 positioning rewrite")
    assert contract.goal == "Draft the Q3 positioning rewrite"
    assert "Remind" not in contract.goal


def test_reminder_contract_unchanged() -> None:
    contract = _reminder_contract("stretch for 5 minutes")
    assert contract.goal == "Remind and update the user about: stretch for 5 minutes"


def test_occurrence_subject_sheds_the_reminder_frame() -> None:
    """R11-B3: a stored reminder goal renders as its bare subject on the calendar."""
    from persona_api.services.occurrences_service import _subject
    from persona_api.services.schedule_create_service import REMINDER_GOAL_PREFIX

    class _S:  # duck-typed Schedule: only payload_template is read
        payload_template: dict = {}

    task = ("t1", "p1", f"{REMINDER_GOAL_PREFIX}stretch for 5 minutes")
    assert _subject(_S(), task) == "stretch for 5 minutes"

    plain = ("t1", "p1", "Draft the Q3 positioning rewrite")
    assert _subject(_S(), plain) == "Draft the Q3 positioning rewrite"
