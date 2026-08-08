"""A task leg's agentic run IS a Spec-08 run — it lands in ``runs`` (D-08-5).

The regression these pin: the background task leg executed a real ``AgenticLoop``, spent
real money, and wrote NO ``runs`` row — invisible to the run viewer, with ``tasks.run_ids``
empty — while the interactive path recorded correctly. Two execution paths, two different
records; A0's own rule is that the worker is a different *place* to run, never a different
*thing* that runs.

So these drive the REAL :class:`TaskLegHandler` against a real database (the community
SQLite engine — real FK, CHECK and JSON enforcement) and assert the row that lands: owner,
persona, status from the run's own status vocabulary, non-empty ``steps``, and the run id
appended to ``tasks.run_ids``. Failed and gated legs are covered too: an unrecorded failure
is half of why the missing record mattered.

The checkpoint sink is stubbed (only ``task_checkpoints.id`` is a Postgres-only server
default, so the CAS append cannot run on SQLite); everything on the run-record path — the
handler, ``TaskStore``, and ``services.run_record`` — is the production code.
"""

# ruff: noqa: ARG002 — Protocol-conformance stubs keep the full production signatures.
from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends import AllModelsFailedError
from persona.errors import CreditsExhaustedError, GatedActionProposedError
from persona.tasks import Contract, ScheduledFire, SpendKind, Task
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import personas as personas_t
from persona_api.db.models import runs as runs_t
from persona_api.errors import RunPersonaOwnerMismatchError
from persona_api.services.user_facing_errors import CAPACITY_BUSY_FREE_MESSAGE
from persona_api.tasks import TaskLegHandler, TaskLegPayload, TaskStore
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import Step, StepType
from sqlalchemy import insert, select

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator, Mapping
    from pathlib import Path

    from persona.tasks import LegBox, TaskCheckpoint
    from persona_runtime.agentic.run import CancelToken, StepUsage
    from sqlalchemy import Engine

_NOW = datetime(2026, 7, 1, 9, 0, tzinfo=UTC)
_OWNER = "user_a"
_PERSONA = "persona_a"
_TASK = "t1"
_GOAL = "find the cheapest fare to Bergen"
_TRIGGER = ScheduledFire(schedule_id="sched-1", fire_time=_NOW)


# --- doubles -----------------------------------------------------------------


class _Runner:
    """A fake agentic run: emits events, then returns the scripted :class:`Run`."""

    def __init__(self, run: Run | None = None, *, raises: BaseException | None = None) -> None:
        self._run = run
        self._raises = raises
        self.calls = 0

    async def run(
        self,
        task: str,
        *,
        on_event: Callable[[RunEvent], Awaitable[None]],
        cancel_token: CancelToken,
        on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,
    ) -> Run:
        self.calls += 1
        if self._raises is not None:
            raise self._raises
        await on_event(RunEvent.thinking(0))
        await on_event(RunEvent.thinking(1))
        assert self._run is not None
        return self._run.model_copy(update={"task": task})


class _RunnerBuilder:
    def __init__(self, runner: _Runner) -> None:
        self._runner = runner

    def build(self, task_id: str, persona_id: str, box: LegBox) -> _Runner:
        return self._runner


class _Sink:
    """A checkpoint store stand-in: the CAS append is Postgres-only (see the module doc)."""

    def __init__(self, *, fails_with: BaseException | None = None) -> None:
        self._fails_with = fails_with

    def get_latest(self, owner_id: str, task_id: str) -> TaskCheckpoint | None:
        return None

    def append(
        self,
        task: Task,
        checkpoint: TaskCheckpoint,
        *,
        spend: Mapping[SpendKind, int] | None = None,
        now: datetime,
    ) -> Task:
        if self._fails_with is not None:
            raise self._fails_with
        advanced = task.advance_checkpoint(checkpoint.checkpoint_seq, now=now)
        for kind, micros in (spend or {}).items():
            advanced = advanced.record_spend(kind, micros, now=now)
        return advanced


class _Context:
    """A minimal ``JobContext``: the owner + a recording meter."""

    def __init__(self, owner_id: str = _OWNER) -> None:
        self._owner_id = owner_id
        self.meter_calls: list[int] = []

    @property
    def owner_id(self) -> str:
        return self._owner_id

    @property
    def job_id(self) -> str:
        return "job-1"

    def meter(self, *, amount_micros: int, kind: str, detail: object = None) -> None:
        self.meter_calls.append(amount_micros)


# --- fixtures + helpers ------------------------------------------------------


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "leg.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="a@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


@pytest.fixture
def tasks(engine: Engine) -> TaskStore:
    store = TaskStore(engine)
    store.create(
        Task(
            id=_TASK,
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal=_GOAL),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    store.start(_OWNER, _TASK, now=_NOW)
    return store


def _handler(
    engine: Engine,
    tasks: TaskStore,
    runner: _Runner,
    *,
    sink: _Sink | None = None,
    record: bool = True,
) -> TaskLegHandler:
    return TaskLegHandler(
        task_store=tasks,
        checkpoint_store=sink or _Sink(),  # type: ignore[arg-type]  # duck-typed CheckpointSink
        runner_builder=_RunnerBuilder(runner),
        rls_engine=engine if record else None,
    )


def _payload() -> TaskLegPayload:
    return TaskLegPayload(task_id=_TASK, predecessor_seq=None, trigger=_TRIGGER)


def _run_rows(engine: Engine) -> list[dict[str, Any]]:
    with engine.begin() as conn:
        return [dict(r) for r in conn.execute(select(runs_t)).mappings().all()]


def _steps(row: Mapping[str, Any]) -> list[dict[str, Any]]:
    steps = row["steps"]
    return json.loads(steps) if isinstance(steps, str) else list(steps or [])


def _completed_run() -> Run:
    return Run(
        persona_id=_PERSONA,
        task=_GOAL,
        status=RunStatus.COMPLETED,
        steps=[Step(type=StepType.FINAL, content="1620 kr, SAS, Tue", tokens=100)],
        output="1620 kr, SAS, Tue",
        started_at=_NOW,
        finished_at=_NOW,
    )


# --- the record lands --------------------------------------------------------


@pytest.mark.asyncio
async def test_completed_leg_lands_a_run_row_linked_to_the_task(
    engine: Engine, tasks: TaskStore
) -> None:
    runner = _Runner(_completed_run())
    await _handler(engine, tasks, runner).handle(_payload(), _Context())

    rows = _run_rows(engine)
    assert len(rows) == 1, "the leg's agentic run must be recorded in runs"
    row = rows[0]
    assert row["owner_id"] == _OWNER
    assert row["persona_id"] == _PERSONA
    assert row["task"] == _GOAL
    assert row["status"] == "completed"
    assert row["output"] == "1620 kr, SAS, Tue"
    assert row["error"] is None
    assert row["finished_at"] is not None
    # The authoritative terminal steps (the run's own), not an empty snapshot.
    assert [s["content"] for s in _steps(row)] == ["1620 kr, SAS, Tue"]
    # ...and the task drills into it.
    assert tasks.get(_OWNER, _TASK).run_ids == (row["id"],)


@pytest.mark.asyncio
async def test_failed_leg_is_recorded_with_its_error(engine: Engine, tasks: TaskStore) -> None:
    """An invisible failure is half of why the missing record mattered."""
    failed = Run(
        persona_id=_PERSONA,
        task=_GOAL,
        status=RunStatus.ERROR,
        steps=[Step(type=StepType.REASONING, content="checking sas.no", tokens=40)],
        output=None,
        error="the browser tool died",
        started_at=_NOW,
        finished_at=_NOW,
    )
    await _handler(engine, tasks, _Runner(failed)).handle(_payload(), _Context())

    rows = _run_rows(engine)
    assert len(rows) == 1
    assert rows[0]["status"] == "error"
    assert rows[0]["error"] == "the browser tool died"
    assert _steps(rows[0]), "a failed run still records the steps it got through"
    assert tasks.get(_OWNER, _TASK).run_ids == (rows[0]["id"],)


@pytest.mark.asyncio
async def test_max_steps_leg_records_the_runs_own_status(engine: Engine, tasks: TaskStore) -> None:
    """No invented vocabulary: the run's own status is written through verbatim."""
    capped = Run(
        persona_id=_PERSONA,
        task=_GOAL,
        status=RunStatus.MAX_STEPS_REACHED,
        steps=[Step(type=StepType.REASONING, content="still working", tokens=100)],
        output=None,
        started_at=_NOW,
        finished_at=_NOW,
    )
    await _handler(engine, tasks, _Runner(capped)).handle(_payload(), _Context())
    assert _run_rows(engine)[0]["status"] == "max_steps_reached"


# --- viewable-not-resumable (D-08-5) -----------------------------------------


@pytest.mark.asyncio
async def test_steps_are_snapshotted_while_the_leg_is_still_running(
    engine: Engine, tasks: TaskStore
) -> None:
    """Mid-run, the persisted row already carries everything emitted so far."""
    seen: dict[str, Any] = {}

    class _Peeking(_Runner):
        async def run(
            self,
            task: str,
            *,
            on_event: Callable[[RunEvent], Awaitable[None]],
            cancel_token: CancelToken,
            on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,
        ) -> Run:
            self.calls += 1
            await on_event(RunEvent.thinking(0))
            await on_event(RunEvent.thinking(1))
            # The crash moment: read the durable row from OUTSIDE this run.
            row = _run_rows(engine)[0]
            seen["status"] = row["status"]
            seen["steps"] = _steps(row)
            return _completed_run()

    await _handler(engine, tasks, _Peeking()).handle(_payload(), _Context())

    assert seen["status"] == "running"
    assert [e["type"] for e in seen["steps"]] == ["thinking", "thinking"]


# --- the honest edges --------------------------------------------------------


@pytest.mark.asyncio
async def test_approval_gate_leaves_a_terminal_record_not_a_dangling_running_row(
    engine: Engine, tasks: TaskStore
) -> None:
    """A gated leg produces no ``Run``; the record still terminates, with the reason."""
    gate = GatedActionProposedError(
        "gated action awaiting approval",
        context={"proposal_id": "prop-1", "tool": "send_email"},
    )
    await _handler(engine, tasks, _Runner(raises=gate)).handle(_payload(), _Context())

    row = _run_rows(engine)[0]
    assert row["status"] == "cancelled"
    assert "prop-1" in row["error"]
    assert row["finished_at"] is not None


@pytest.mark.asyncio
async def test_a_leg_that_raises_is_recorded_errored_and_still_re_raised(
    engine: Engine, tasks: TaskStore
) -> None:
    """A0 must still see the failure — recording it does not swallow it."""
    runner = _Runner(_completed_run())
    handler = _handler(engine, tasks, runner, sink=_Sink(fails_with=RuntimeError("db down")))

    with pytest.raises(RuntimeError, match="db down"):
        await handler.handle(_payload(), _Context())

    row = _run_rows(engine)[0]
    assert row["status"] == "error"
    assert row["error"] == "db down"


@pytest.mark.asyncio
async def test_a_persona_that_is_not_the_owners_fails_loudly_before_any_model_spend(
    engine: Engine, tasks: TaskStore
) -> None:
    """The composite FK ``(persona_id, owner_id)`` must break the leg, not be skipped."""
    ensure_owner(engine, owner_id="user_b", email="b@example.com")
    runner = _Runner(_completed_run())
    handler = _handler(engine, tasks, runner)

    # A leg claiming to run as user_b against user_a's persona: the run row cannot exist.
    with pytest.raises(RunPersonaOwnerMismatchError):
        await handler.handle(_payload(), _Context("user_b"))

    assert runner.calls == 0, "the record is opened BEFORE the model spend"
    assert _run_rows(engine) == []


@pytest.mark.asyncio
async def test_no_engine_wired_keeps_the_bare_handler_shape(
    engine: Engine, tasks: TaskStore
) -> None:
    """A handler with no engine (unit / plain-A2 composition) records nothing, as before."""
    await _handler(engine, tasks, _Runner(_completed_run()), record=False).handle(
        _payload(), _Context()
    )
    assert _run_rows(engine) == []
    assert tasks.get(_OWNER, _TASK).run_ids == ()


class _ExhaustedPolicy:
    """A CreditsPolicy whose pre-flight gate refuses: the owner is at zero."""

    def __init__(self) -> None:
        self.checked = False

    def require_credits(self, *, rls_engine: Engine, user_id: str) -> int:  # noqa: ARG002
        self.checked = True
        raise CreditsExhaustedError("no credits", context={"user_id": user_id})


class _FundedPolicy:
    """A CreditsPolicy with balance; capture is a no-op so the leg is unbilled but allowed."""

    def __init__(self) -> None:
        self.checked = False

    def require_credits(self, *, rls_engine: Engine, user_id: str) -> int:  # noqa: ARG002
        self.checked = True
        return 500

    def capture_up_to_idempotent(self, **_kwargs: object) -> object:
        return None


@pytest.mark.asyncio
async def test_a_leg_does_not_run_when_the_owner_is_out_of_credits(
    engine: Engine, tasks: TaskStore
) -> None:
    """THE regression (R9-108): the leg path had no pre-flight credit gate.

    It only CAPTURED after the fact, so an owner at zero kept running work that
    billed nothing -- production showed 61 legs in 24h against balance=0, each
    capturing 0 and consuming provider quota anyway. The chat path has gated on
    require_credits all along.
    """
    runner = _Runner(_completed_run())
    policy = _ExhaustedPolicy()
    handler = TaskLegHandler(
        task_store=tasks,
        checkpoint_store=_Sink(),  # type: ignore[arg-type]
        runner_builder=_RunnerBuilder(runner),
        rls_engine=engine,
        credits_policy=policy,  # type: ignore[arg-type]
    )
    await handler.handle(_payload(), _Context())
    assert policy.checked is True, "the leg must consult the credit gate before running"
    assert runner.calls == 0, "an owner at zero must not have work executed on their behalf"
    assert _run_rows(engine) == [], "no run record either -- the leg never started"


@pytest.mark.asyncio
async def test_a_funded_owner_still_runs(engine: Engine, tasks: TaskStore) -> None:
    """The gate must not block a paying owner -- otherwise it trades one bug for a worse one."""
    runner = _Runner(_completed_run())
    policy = _FundedPolicy()
    handler = TaskLegHandler(
        task_store=tasks,
        checkpoint_store=_Sink(),  # type: ignore[arg-type]
        runner_builder=_RunnerBuilder(runner),
        rls_engine=engine,
        credits_policy=policy,  # type: ignore[arg-type]
    )
    await handler.handle(_payload(), _Context())
    assert policy.checked is True
    assert runner.calls == 1, "a funded owner's leg must run normally"


@pytest.mark.asyncio
async def test_an_unmetered_deployment_is_unaffected(engine: Engine, tasks: TaskStore) -> None:
    """No credits_policy wired (community / plain A2) => no gate, byte-identical behaviour."""
    runner = _Runner(_completed_run())
    await _handler(engine, tasks, runner).handle(_payload(), _Context())
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_a_tier_exhaustion_is_sanitised_before_it_reaches_the_run_row(
    engine: Engine, tasks: TaskStore
) -> None:
    """R9-097 (remainder): a leg's ``runs.error`` is shown to the user too.

    The chat path stopped printing our provider names, model ids and routing
    strategy on a busy moment; the leg path still wrote ``str(exc)`` into a column
    the task's run list renders. Same leak, different door.

    A0 still sees the real failure: the exception re-raises unchanged, which is
    what its retry and dead-letter accounting reads.
    """
    exhausted = AllModelsFailedError(
        "every backend in MultiModelChatBackend exhausted",
        context={
            "tier": "frontier",
            "attempts_json": '[{"provider": "openrouter", "model": "openai/gpt-oss-20b:free"}]',
            "final_error_class": "RateLimitError",
        },
    )
    handler = _handler(engine, tasks, _Runner(_completed_run()), sink=_Sink(fails_with=exhausted))

    with pytest.raises(AllModelsFailedError):
        await handler.handle(_payload(), _Context())

    stored = str(_run_rows(engine)[0]["error"])
    assert stored == CAPACITY_BUSY_FREE_MESSAGE  # no subscription row IS the free plan
    for leaked in ("openrouter", "gpt-oss-20b", "MultiModelChatBackend"):
        assert leaked not in stored, f"{leaked!r} reached the user through a leg's runs.error"
