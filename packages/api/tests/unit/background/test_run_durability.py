"""T3 (spec P1) — runs durability VERIFY-AND-HARDEN: ``runs.steps`` is the floor.

The P1 ruling (D-P1-runs-no-migration): runs already reattach durably via the
existing ``runs.steps`` column — ``RunRegistry`` snapshots the FULL event-log to
it on every event (``_persist_progress``), so a reattach-after-gap reads
everything that happened while away from the persisted row. **No ``run_events``
table is needed.** This test pins that invariant: mid-run, before the task
finishes, the persisted ``runs.steps`` already contains every event emitted so
far; on completion it holds the authoritative final steps.
"""

# ruff: noqa: ARG002 — the scripted loop's signature mirrors AgenticLoop.run.

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.schema.tools import ToolCall, ToolResult
from persona_api.background.run_worker import RunRegistry
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import personas as personas_t
from persona_api.db.models import runs as runs_t
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import (
    CallSkippedNote,
    ContextPrunedNote,
    Step,
    StepType,
)
from sqlalchemy import insert, select

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from sqlalchemy import Engine

_OWNER = "user_alice"
_PERSONA = "astrid"
_RUN = "run_durable"


@pytest.fixture
def engine(tmp_path: object) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "t.db")  # type: ignore[operator]
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="a@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: Astrid"))
        conn.execute(
            insert(runs_t).values(
                id=_RUN, owner_id=_OWNER, persona_id=_PERSONA, task="t", status="running"
            )
        )
    return eng


def _persisted_steps(engine: Engine) -> list[dict[str, object]]:
    with engine.begin() as conn:
        steps = conn.execute(select(runs_t.c.steps).where(runs_t.c.id == _RUN)).scalar_one()
    if isinstance(steps, str):  # sqlite JSON round-trips as text
        steps = json.loads(steps)
    return list(steps) if steps else []


class _GatedLoop:
    """Emits two events, then blocks so the test can inspect the persisted floor
    MID-RUN (the reattach-after-gap moment), then completes."""

    def __init__(self, emitted: asyncio.Event, release: asyncio.Event) -> None:
        self._emitted = emitted
        self._release = release

    async def run(
        self,
        task: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        user_respond: Callable[[str], Awaitable[str]] | None = None,
        cancel_token: object | None = None,
    ) -> Run:
        assert on_event is not None
        await on_event(RunEvent.started(task))
        await on_event(
            RunEvent.tool_calling(0, [ToolCall(name="web_search", args={}, call_id="c1")])
        )
        self._emitted.set()  # two events emitted + persisted
        await self._release.wait()  # hold the run open so the test inspects mid-run
        now = datetime.now(UTC)
        return Run(
            persona_id=_PERSONA,
            task=task,
            status=RunStatus.COMPLETED,
            steps=[],
            output="done",
            error=None,
            started_at=now,
            finished_at=now,
        )


class _ActivityLoop:
    """Emits the P2 activity contract interleaved with tool events, gating AFTER
    ``activity_start`` (before ``activity_end``) so the test can inspect the persisted
    floor while the activity is in-flight — the reattach-mid-"using X…" moment."""

    def __init__(self, emitted: asyncio.Event, release: asyncio.Event) -> None:
        self._emitted = emitted
        self._release = release

    async def run(
        self,
        task: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        user_respond: Callable[[str], Awaitable[str]] | None = None,
        cancel_token: object | None = None,
    ) -> Run:
        assert on_event is not None
        await on_event(RunEvent.started(task))
        await on_event(
            RunEvent.tool_calling(0, [ToolCall(name="web_search", args={}, call_id="c1")])
        )
        await on_event(
            RunEvent.activity_start(
                0,
                activity_id="a1",
                kind="web",
                name="web_search",
                label="Searching the web",
                args_summary={"q": "rent"},
            )
        )
        self._emitted.set()  # in-flight: start persisted, end NOT yet
        await self._release.wait()
        await on_event(
            RunEvent.activity_end(0, activity_id="a1", status="ok", duration_ms=7.0, is_error=False)
        )
        await on_event(
            RunEvent.tool_result(
                0,
                "web_search",
                ToolResult(tool_name="web_search", content="results", call_id="c1"),
            )
        )
        now = datetime.now(UTC)
        return Run(
            persona_id=_PERSONA,
            task=task,
            status=RunStatus.COMPLETED,
            steps=[],
            output="done",
            error=None,
            started_at=now,
            finished_at=now,
        )


@pytest.mark.asyncio
async def test_activity_trail_in_running_snapshot_survives_mid_activity_reattach(
    engine: Engine,
) -> None:
    # P2 T4 (the trail-survives-reattach hold, RUN surface): while a run is RUNNING, the
    # event-log snapshot in runs.steps carries activity_* in order (no migration), so a
    # reattach-after-gap reads the in-flight "using X…" — activity_start persisted, its end
    # not yet. NOTE the two-shape behaviour (run_worker.py:139-141): on COMPLETION
    # _persist_final overwrites runs.steps with the authoritative Step list (tool_calls /
    # results — the benchmark's metric source), so the completed RUN form is Steps, not the
    # event-log. The verbatim activity trail through completion is the CHAT surface
    # (messages.stream_events) — see test_chat_turn_worker.
    emitted, release = asyncio.Event(), asyncio.Event()
    registry = RunRegistry(engine)
    handle = registry.start(
        run_id=_RUN,
        owner_id=_OWNER,
        loop=_ActivityLoop(emitted, release),
        task_text="t",  # type: ignore[arg-type]
    )

    await emitted.wait()
    # Mid-activity: the persisted running snapshot carries the trail IN ORDER — a reattach
    # reads the in-flight activity (start present, end not yet; ordered after its tool_calling).
    mid = [e.get("type") for e in _persisted_steps(engine)]
    assert "activity_start" in mid
    assert "activity_end" not in mid
    assert mid.index("tool_calling") < mid.index("activity_start")

    release.set()
    assert handle.task is not None
    await handle.task

    # On completion the authoritative form is the Step list (pre-existing P1 behaviour).
    with engine.begin() as conn:
        status = conn.execute(select(runs_t.c.status).where(runs_t.c.id == _RUN)).scalar_one()
    assert status == str(RunStatus.COMPLETED)


@pytest.mark.asyncio
async def test_runs_steps_is_the_durable_floor_mid_run(engine: Engine) -> None:
    emitted, release = asyncio.Event(), asyncio.Event()
    registry = RunRegistry(engine)
    handle = registry.start(
        run_id=_RUN,
        owner_id=_OWNER,
        loop=_GatedLoop(emitted, release),
        task_text="t",  # type: ignore[arg-type]
    )

    await emitted.wait()
    # MID-RUN, before the task finishes: the persisted row already reflects
    # EVERYTHING emitted so far — a reattach-after-gap reads it all from the DB,
    # not only from the in-memory bus. This is the durable floor (no run_events).
    mid = _persisted_steps(engine)
    types = [e.get("type") for e in mid]
    assert "started" in types
    assert "tool_calling" in types

    release.set()
    assert handle.task is not None
    await handle.task

    with engine.begin() as conn:
        status = conn.execute(select(runs_t.c.status).where(runs_t.c.id == _RUN)).scalar_one()
    assert status == str(RunStatus.COMPLETED)  # authoritative final persisted on completion


class _GuardedLoop:
    """Completes with a step whose deterministic guards fired (Spec W1; R9-157).

    The ledger answered one of the step's calls and the pruner trimmed at its boundary,
    which is what a long leg really looks like. Nothing is emitted live here on purpose:
    the question is what the DURABLE record says, because that is all a run opened after
    the fact has.
    """

    async def run(
        self,
        task: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        user_respond: Callable[[str], Awaitable[str]] | None = None,
        cancel_token: object | None = None,
    ) -> Run:
        now = datetime.now(UTC)
        return Run(
            persona_id=_PERSONA,
            task=task,
            status=RunStatus.COMPLETED,
            steps=[
                Step(
                    type=StepType.TOOL_CALL,
                    tool_calls=[ToolCall(name="web_search", args={"q": "rent"}, call_id="c1")],
                    results=[ToolResult(tool_name="web_search", content="results", call_id="c1")],
                    notes=[
                        CallSkippedNote(tool="web_search", guard="cached_read"),
                        ContextPrunedNote(before_tokens=9_000, after_tokens=4_000),
                    ],
                    tier_used="small",
                ),
                Step(type=StepType.FINAL, content="done", tier_used="mid"),
            ],
            output="done",
            error=None,
            started_at=now,
            finished_at=now,
        )


@pytest.mark.asyncio
async def test_what_the_guards_did_is_in_the_persisted_run(engine: Engine) -> None:
    """R9-157: the guard disclosure used to live only on the live SSE stream, so every run
    opened afterwards showed nothing and a correct guard read as a missing feature. The
    terminal write is the fix's load-bearing half: ``runs.steps`` itself has to say which
    guard fired, and for a skipped call, on which tool."""
    registry = RunRegistry(engine)
    handle = registry.start(
        run_id=_RUN,
        owner_id=_OWNER,
        loop=_GuardedLoop(),
        task_text="t",  # type: ignore[arg-type]
    )
    assert handle.task is not None
    await handle.task

    steps = _persisted_steps(engine)

    assert steps[0]["notes"] == [
        {"kind": "call_skipped", "tool": "web_search", "guard": "cached_read"},
        {"kind": "context_pruned", "before_tokens": 9_000, "after_tokens": 4_000},
    ]
    assert steps[1]["notes"] == []  # the step no guard touched claims nothing
