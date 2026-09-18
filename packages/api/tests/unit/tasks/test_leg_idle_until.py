"""A leg that says "nothing to do until Thursday" is scheduled for Thursday (finding E).

``LegOutcome.resume_at`` had three consumers and no producer: every CONTINUE re-enqueued at
once, so a task that should have slept burned a full leg per fire to learn it was still too
early. This drives the whole chain: the distiller's answer names the time, the executor
carries it on the outcome, and the continuation enqueues the next leg AT that time, with the
task parked ``waiting(until_time)`` in between (the task store is the real one on the
community engine; the checkpoint sink is stubbed because its CAS append is Postgres-only).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.tasks import (
    Contract,
    SpendKind,
    Task,
    TaskState,
    UserReply,
    WaitKind,
    micros_from_cents,
)
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.store import TaskStore
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from persona_runtime.legs import LegDisposition, LegExecutor
from persona_runtime.legs.semantic_distiller import SemanticCheckpointWriter
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping
    from pathlib import Path

    from persona.tasks import TaskCheckpoint
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.agentic.run import CancelToken
    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 15, 9, 0, tzinfo=UTC)
_THURSDAY = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
_OWNER = "user_idle"
_PERSONA = "persona_idle"


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "idle.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="i@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


class _ScriptedBackend:
    def __init__(self, content: str) -> None:
        self._content = content

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-haiku-4-5-20251001"

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: object, **_kwargs: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        return ChatResponse(
            content=self._content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


class _BoxedRunner:
    """A leg the box stopped while it was still polling for the sale to open."""

    async def run(
        self,
        task: str,
        *,
        on_event: Callable[[RunEvent], Awaitable[None]],  # noqa: ARG002
        cancel_token: CancelToken,  # noqa: ARG002
    ) -> Run:
        return Run(
            persona_id=_PERSONA,
            task=task,
            status=RunStatus.MAX_STEPS_REACHED,
            steps=[Step(type=StepType.REASONING, content="the sale opens Thursday", tokens=10)],
            output=None,
            started_at=_NOW,
            finished_at=_NOW,
        )


class _RecordingQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, object]] = []

    def enqueue(self, **kwargs: object) -> None:
        self.enqueued.append(kwargs)


class _Sink:
    """The checkpoint store's append, minus the Postgres-only row id; keeps what landed."""

    def __init__(self) -> None:
        self.landed: list[TaskCheckpoint] = []

    def append(
        self,
        task: Task,
        checkpoint: TaskCheckpoint,
        *,
        spend: Mapping[SpendKind, int],
        now: datetime,
    ) -> Task:
        self.landed.append(checkpoint)
        advanced = task.advance_checkpoint(checkpoint.checkpoint_seq, now=now)
        for kind, micros in spend.items():
            advanced = advanced.record_spend(kind, micros, now=now)
        return advanced


def _meter(run: Run) -> Mapping[SpendKind, int]:
    return {SpendKind.MODEL: micros_from_cents(0.25 * len(run.steps))}


def _distillation(**overrides: object) -> str:
    payload: dict[str, object] = {
        "conclusions": ["the sale opens Thursday at 09:00 UTC; nothing is bookable before"],
        "lessons": [],
        "plan": ["book the fare once the sale is open"],
        "next_step": "Open the booking page and book the fare",
        "open_questions": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


async def _run_leg(engine: Engine, distillation: str) -> tuple[_RecordingQueue, TaskStore, _Sink]:
    tasks = TaskStore(engine)
    tasks.create(
        Task(
            id="t1",
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal="book the fare when the sale opens"),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    task = tasks.start(_OWNER, "t1", now=_NOW)
    sink = _Sink()
    executor = LegExecutor(
        runner=_BoxedRunner(),
        writer=SemanticCheckpointWriter(
            backend_provider=lambda: _ScriptedBackend(distillation),  # type: ignore[arg-type, return-value]
        ),
        sink=sink,
        meter=_meter,
    )
    outcome = await executor.run_leg(task=task, trigger=UserReply(reply="go ahead"), now=_NOW)
    assert outcome.disposition is LegDisposition.CONTINUE
    queue = _RecordingQueue()
    TaskContinuation(task_store=tasks, queue=queue).apply(  # type: ignore[arg-type]
        _OWNER, outcome, now=_NOW
    )
    return queue, tasks, sink


@pytest.mark.asyncio
async def test_the_next_leg_is_scheduled_for_the_time_the_leg_named(engine: Engine) -> None:
    queue, tasks, sink = await _run_leg(engine, _distillation(idle_until=_THURSDAY.isoformat()))

    assert len(queue.enqueued) == 1
    assert queue.enqueued[0]["scheduled_at"] == _THURSDAY  # not now
    parked = tasks.get(_OWNER, "t1")
    assert parked.state is TaskState.WAITING
    assert parked.wait_kind is WaitKind.UNTIL_TIME
    # The durable record of the wait: the checkpoint that landed names the instant.
    assert sink.landed[-1].idle_until == _THURSDAY


@pytest.mark.asyncio
async def test_a_leg_with_work_to_do_continues_at_once(engine: Engine) -> None:
    queue, tasks, _ = await _run_leg(engine, _distillation())

    assert len(queue.enqueued) == 1
    assert queue.enqueued[0]["scheduled_at"] == _NOW
    assert tasks.get(_OWNER, "t1").state is TaskState.ACTIVE


@pytest.mark.asyncio
async def test_an_idle_time_past_the_ceiling_continues_at_once(engine: Engine) -> None:
    """The ceiling is the difference between a task that defers itself and one that goes to
    sleep for good without the user agreeing to it."""
    far = (_NOW + timedelta(days=60)).isoformat()
    queue, _, _ = await _run_leg(engine, _distillation(idle_until=far))

    assert queue.enqueued[0]["scheduled_at"] == _NOW
