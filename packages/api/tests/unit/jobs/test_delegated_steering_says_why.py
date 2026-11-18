"""A spoken pause or resume that changes nothing says why (R9-241, D-ORCH-11-18-d (4)).

The voice path delegates a steering ask to a frontier turn and then applies the verb
through the steering service. It answered "Done. I've resumed that task." whatever the
control said, so a resume refused at the budget cap was reported as done. The service now
hands back the control's outcome, and a no-op is answered with the control's own note.
"""

from __future__ import annotations

from typing import Any

import pytest
from persona.schema.conversation import Conversation
from persona_api.jobs.handlers.delegated_turn import DelegatedTurnHandler, DelegatedTurnOutcome
from persona_runtime.task_origination import SteeringOutcome

_AT_THE_CAP = (
    "This task has used its budget, so resuming it would stop again at once. "
    "Extend the budget with Raise cap on the task page and it carries on by itself."
)


class _Steering:
    def __init__(self, outcome: SteeringOutcome | None) -> None:
        self._outcome = outcome
        self.applied: list[dict[str, Any]] = []

    async def steer(self, data: dict[str, Any]) -> SteeringOutcome | None:
        self.applied.append(dict(data))
        return self._outcome


async def _spoken(verb: str, outcome: SteeringOutcome | None) -> tuple[DelegatedTurnOutcome, str]:
    handler = DelegatedTurnHandler(
        runtime_factory=None,  # type: ignore[arg-type]
        origination_service=None,  # type: ignore[arg-type]
        steering_service=_Steering(outcome),  # type: ignore[arg-type]
        rls_engine=None,  # type: ignore[arg-type]
    )
    return await handler._resolve_outcome(
        conversation=Conversation(conversation_id="c1", persona_id="astrid", messages=[]),
        events=[{"type": "task_steering", "data": {"verb": verb, "task_id": "t1"}}],
        reply_text="",
        owner_id="user-1",
        conversation_id="c1",
        persona_id="astrid",
        delegation_key="d1",
    )


@pytest.mark.asyncio
async def test_a_spoken_resume_refused_at_the_cap_says_why() -> None:
    assert await _spoken("resume", SteeringOutcome(changed=False, note=_AT_THE_CAP)) == (
        DelegatedTurnOutcome.SUCCEEDED,
        _AT_THE_CAP,
    )


@pytest.mark.asyncio
async def test_a_spoken_pause_that_took_says_done() -> None:
    assert await _spoken("pause", SteeringOutcome(changed=True)) == (
        DelegatedTurnOutcome.SUCCEEDED,
        "Done. I've paused that task.",
    )
