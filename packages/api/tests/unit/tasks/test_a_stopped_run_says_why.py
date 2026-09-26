"""A run that was stopped early records why (R9-158, migration 057).

A task leg stopped before it ended on its own was written as a bare ``cancelled``,
whichever thing stopped it, so a task the user had only paused showed a "cancelled" run.
``runs.stop_reason`` now carries the reason: the control (``paused`` / ``cancelled``), a
box bound, the deploy drain, or the approval gate.

These pin, on the community engine with the real handler, stores and run writer:

- the vocabulary: ``RunStopReason`` equals the CHECK on the model and in migration 057,
  and every reason the leg box or the drain can trip with is in it;
- the writer: the reason lands only on a cancelled run, and the approval gate records its
  own;
- the producer: the control watcher trips with ``paused`` or ``cancelled`` and the run row
  says so, and a run that ended on its own records none;
- the reader: the task detail's run summary carries it.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.errors import GatedActionProposedError
from persona.tasks import Contract, LegBoxLimit, ScheduledFire, Task
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.db.models import runs as runs_t
from persona_api.services import run_record, run_service, task_control_service
from persona_api.services.run_record import RunStopReason
from persona_api.tasks import TaskLegHandler, TaskLegPayload, TaskStore
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from sqlalchemy import CheckConstraint, insert, select

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping

    from persona.tasks import LegBox, SpendKind, TaskCheckpoint
    from persona_runtime.agentic.run import CancelToken, StepUsage
    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)
_OWNER = "user_stop_reason"
_PERSONA = "persona_stop_reason"
_MIGRATION = (
    Path(__file__).resolve().parents[3] / "alembic" / "versions" / "057_runs_stop_reason.py"
)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "stop_reason.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="r@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


@pytest.fixture
def store(engine: Engine) -> TaskStore:
    s = TaskStore(engine)
    s.create(
        Task(
            id="t1",
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal="find three sources"),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    s.start(_OWNER, "t1", now=_NOW)
    return s


def _quoted(sql: str) -> set[str]:
    return set(re.findall(r"'([a-z_]+)'", sql))


# --- the vocabulary -----------------------------------------------------------------------


def test_the_stop_reason_vocabulary_is_one_list_in_the_enum_the_model_and_the_migration() -> None:
    check = next(
        c
        for c in runs_t.constraints
        if isinstance(c, CheckConstraint) and c.name == "runs_stop_reason_check"
    )
    enum_values = {r.value for r in RunStopReason}
    migration = _MIGRATION.read_text(encoding="utf-8")
    upgrade = migration[migration.index("def upgrade") : migration.index("def downgrade")]
    assert len(enum_values) == 7
    assert (_quoted(str(check.sqltext)), _quoted(upgrade)) == (enum_values, enum_values)


def test_every_reason_the_box_or_the_drain_can_trip_with_is_recordable() -> None:
    assert {limit.value for limit in LegBoxLimit} <= {r.value for r in RunStopReason}


# --- the writer ---------------------------------------------------------------------------


def _open_run(engine: Engine, run_id: str) -> None:
    run_record.insert_run(
        engine,
        run_id=run_id,
        owner_id=_OWNER,
        persona_id=_PERSONA,
        task="find three sources",
        task_id="t1",
        started_at=_NOW,
    )


def _row(engine: Engine, run_id: str) -> tuple[str, str | None]:
    with engine.begin() as conn:
        row = conn.execute(
            select(runs_t.c.status, runs_t.c.stop_reason).where(runs_t.c.id == run_id)
        ).one()
    return (str(row.status), row.stop_reason)


def _finished(status: RunStatus) -> Run:
    return Run(
        persona_id=_PERSONA,
        task="find three sources",
        status=status,
        steps=[Step(type=StepType.REASONING, content="read one source", tokens=3)],
        output="one source read",
        started_at=_NOW,
        finished_at=_NOW,
    )


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (RunStatus.CANCELLED, ("cancelled", "paused")),
        (RunStatus.COMPLETED, ("completed", None)),
        (RunStatus.MAX_STEPS_REACHED, ("max_steps_reached", None)),
    ],
)
def test_the_reason_is_recorded_only_on_a_cancelled_run(
    engine: Engine, store: TaskStore, status: RunStatus, expected: tuple[str, str | None]
) -> None:
    del store  # the task row the run names
    _open_run(engine, "run-1")
    run_record.persist_final(
        engine,
        run_id="run-1",
        run=_finished(status),
        owner_id=_OWNER,
        stop_reason=RunStopReason.PAUSED,
    )
    assert _row(engine, "run-1") == expected


def test_a_run_the_approval_gate_stopped_records_approval(engine: Engine, store: TaskStore) -> None:
    del store
    _open_run(engine, "run-1")
    run_record.persist_terminal(
        engine,
        run_id="run-1",
        status="cancelled",
        error="stopped for approval (proposal p1)",
        finished_at=_NOW,
        owner_id=_OWNER,
        stop_reason=RunStopReason.APPROVAL,
    )
    assert _row(engine, "run-1") == ("cancelled", "approval")


# --- the producer, through the real handler -----------------------------------------------


class _StoppableLeg:
    """Two boundaries; the user presses ``control`` between them; the loop's own rule then
    decides the status (cancelled if its token was tripped, else ``ends``)."""

    def __init__(
        self,
        engine: Engine,
        store: TaskStore,
        *,
        control: str | None,
        ends: RunStatus = RunStatus.MAX_STEPS_REACHED,
        gate: bool = False,
    ) -> None:
        self._engine = engine
        self._store = store
        self._control = control
        self._ends = ends
        self._gate = gate

    async def run(
        self,
        task: str,
        *,
        on_event: Callable[[RunEvent], Awaitable[None]],
        cancel_token: CancelToken,
        on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,  # noqa: ARG002
    ) -> Run:
        await on_event(RunEvent.thinking(0))
        current = self._store.get(_OWNER, "t1")
        if self._control == "pause":
            task_control_service.pause_task(self._engine, _OWNER, current, now=_NOW)
        elif self._control == "cancel":
            task_control_service.cancel_task(self._engine, _OWNER, current, now=_NOW)
        await on_event(RunEvent.thinking(1))
        if self._gate:
            raise GatedActionProposedError(
                "gated", context={"proposal_id": "p1", "description": "send the email"}
            )
        status = RunStatus.CANCELLED if cancel_token.is_cancelled else self._ends
        return Run(
            persona_id=_PERSONA,
            task=task,
            status=status,
            steps=[Step(type=StepType.REASONING, content="read one source", tokens=3)],
            output="one source read",
            started_at=_NOW,
            finished_at=_NOW,
        )


class _Builder:
    def __init__(self, runner: object) -> None:
        self._runner = runner

    def build(self, task_id: str, persona_id: str, box: LegBox, *, task: object = None) -> object:  # noqa: ARG002
        return self._runner


class _Sink:
    def get_latest(self, owner_id: str, task_id: str) -> TaskCheckpoint | None:  # noqa: ARG002
        return None

    def append(
        self,
        task: Task,
        checkpoint: TaskCheckpoint,
        *,
        spend: Mapping[SpendKind, int] | None = None,  # noqa: ARG002
        now: datetime,
    ) -> Task:
        return task.advance_checkpoint(checkpoint.checkpoint_seq, now=now)


class _Context:
    owner_id = _OWNER
    job_id = "job-1"

    def meter(self, *, amount_micros: int, kind: str, detail: object = None) -> None:  # noqa: ARG002
        return None


async def _run_the_leg(engine: Engine, store: TaskStore, leg: _StoppableLeg) -> None:
    handler = TaskLegHandler(
        task_store=store,
        checkpoint_store=_Sink(),  # type: ignore[arg-type]
        runner_builder=_Builder(leg),  # type: ignore[arg-type]
        rls_engine=engine,  # opens and settles the leg's real runs row
    )
    payload = TaskLegPayload(
        task_id="t1", predecessor_seq=None, trigger=ScheduledFire(schedule_id="s", fire_time=_NOW)
    )
    await handler.handle(payload, _Context())  # type: ignore[arg-type]


def _the_runs(engine: Engine) -> list[tuple[str, str | None]]:
    rows = run_service.list_runs_for_task(rls_engine=engine, task_id="t1")
    return [(s.status, s.stop_reason) for s in map(run_service.summarise_run, rows)]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("control", "expected"),
    [("pause", ("cancelled", "paused")), ("cancel", ("cancelled", "cancelled"))],
)
async def test_a_run_a_control_stopped_says_which_control(
    engine: Engine, store: TaskStore, control: str, expected: tuple[str, str]
) -> None:
    await _run_the_leg(engine, store, _StoppableLeg(engine, store, control=control))
    assert _the_runs(engine) == [expected]


@pytest.mark.asyncio
async def test_a_run_that_ended_on_its_own_records_no_reason(
    engine: Engine, store: TaskStore
) -> None:
    await _run_the_leg(engine, store, _StoppableLeg(engine, store, control=None))
    assert _the_runs(engine) == [("max_steps_reached", None)]


@pytest.mark.asyncio
async def test_a_run_the_approval_gate_stopped_says_it_waits_for_approval(
    engine: Engine, store: TaskStore
) -> None:
    await _run_the_leg(engine, store, _StoppableLeg(engine, store, control=None, gate=True))
    assert _the_runs(engine) == [("cancelled", "approval")]
