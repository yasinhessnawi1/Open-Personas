"""A delegated voice turn and a task leg bind their interaction for direct writes (R9-253).

``record_user_fact`` names the interaction its turn is bound to, and a disclosure there
records the durable fact under that id, so initiative's wellbeing gate holds it. Both of
these run inside the job executor's own context, so the binding must also be reset when the
work ends, or the next job in the same context would inherit it.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from persona.jobs.delegation import DelegatedTurnPayload
from persona.schema.conversation import Conversation
from persona_api.jobs.handlers.delegated_turn import DelegatedTurnHandler, DelegatedTurnOutcome
from persona_api.tasks.leg_runner import RuntimeFactoryLegRunnerBuilder
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.interaction_scope import InteractionRef, current_interaction

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


class _ConversationLoop:
    def __init__(self, *, fail: bool = False) -> None:
        self.seen: InteractionRef | None = None
        self._fail = fail

    async def turn(self, *_a: object, **_k: object) -> AsyncIterator[object]:
        self.seen = current_interaction()
        if self._fail:
            raise RuntimeError("the turn broke")
        yield SimpleNamespace(delta="ok")


class _AgenticLoop:
    def __init__(self, *, fail: bool = False) -> None:
        self.seen: InteractionRef | None = None
        self._fail = fail

    async def run(self, task: str, **_k: object) -> Run:
        self.seen = current_interaction()
        if self._fail:
            raise RuntimeError("the leg broke")
        now = datetime.now(UTC)
        return Run(
            persona_id="astrid",
            task=task,
            status=RunStatus.COMPLETED,
            steps=[],
            output="done",
            error=None,
            started_at=now,
            finished_at=now,
        )


class _Factory:
    def __init__(self, loop: object) -> None:
        self._loop = loop

    async def build_conversation_loop(self, _persona_id: str) -> object:
        return self._loop

    async def build_agentic_loop(self, _persona_id: str, **_k: object) -> object:
        return self._loop


def _delegated(loop: _ConversationLoop) -> DelegatedTurnHandler:
    handler = DelegatedTurnHandler(
        runtime_factory=_Factory(loop),  # type: ignore[arg-type]
        origination_service=None,  # type: ignore[arg-type]
        steering_service=None,  # type: ignore[arg-type]
        rls_engine=None,  # type: ignore[arg-type]
    )
    handler._load_conversation = lambda cid, pid: Conversation(  # type: ignore[method-assign]  # noqa: SLF001
        conversation_id=cid, persona_id=pid, messages=[]
    )

    async def resolved(**_k: Any) -> tuple[DelegatedTurnOutcome, str]:  # noqa: ANN401
        return DelegatedTurnOutcome.SUCCEEDED, "Done."

    handler._resolve_outcome = resolved  # type: ignore[method-assign]  # noqa: SLF001
    handler._record_outcome = lambda **_k: None  # type: ignore[method-assign]  # noqa: SLF001
    return handler


def _payload() -> DelegatedTurnPayload:
    return DelegatedTurnPayload(
        conversation_id="conv-call", verbatim_ask="remind me", persona_id="astrid"
    )


@pytest.mark.parametrize("fail", [False, True], ids=["clean", "the-turn-fails"])
@pytest.mark.asyncio
async def test_a_delegated_turn_binds_its_conversation_and_resets_it(fail: bool) -> None:
    loop = _ConversationLoop(fail=fail)
    await _delegated(loop).handle(_payload(), SimpleNamespace(owner_id="user-1"))  # type: ignore[arg-type]
    assert loop.seen == InteractionRef(kind="conversation", id="conv-call")
    assert current_interaction() is None, "the next job must not inherit it"


@pytest.mark.asyncio
async def test_a_task_leg_binds_its_task_and_resets_it() -> None:
    loop = _AgenticLoop()
    runner = RuntimeFactoryLegRunnerBuilder(_Factory(loop)).build("task-9", "astrid", box=None)  # type: ignore[arg-type]
    await runner.run("carry on", on_event=None, cancel_token=None)  # type: ignore[arg-type]
    assert loop.seen == InteractionRef(kind="task", id="task-9")
    assert current_interaction() is None


@pytest.mark.asyncio
async def test_a_failing_task_leg_still_resets_its_binding() -> None:
    loop = _AgenticLoop(fail=True)
    runner = RuntimeFactoryLegRunnerBuilder(_Factory(loop)).build("task-9", "astrid", box=None)  # type: ignore[arg-type]
    with pytest.raises(RuntimeError, match="the leg broke"):
        await runner.run("carry on", on_event=None, cancel_token=None)  # type: ignore[arg-type]
    assert loop.seen == InteractionRef(kind="task", id="task-9")
    assert current_interaction() is None
