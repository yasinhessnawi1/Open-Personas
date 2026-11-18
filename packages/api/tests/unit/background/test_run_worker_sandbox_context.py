"""Bug B: the run worker must bind a SandboxRequestContext around the loop run.

After the file-tool security scoping (file_read / file_write resolve their root
from the bound :class:`SandboxRequestContext`), a run with NO context bound fails
closed — every ``file_write`` / ``file_read`` errors. The run worker
(:class:`RunRegistry._run`) must bind the context to the run's owner with a
run-scoped sandbox session (mirroring ``chat_service.stream_chat``), and reset it
in a ``finally``.
"""

# ruff: noqa: ARG002 — test-double loop signatures mirror AgenticLoop.run intentionally.

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona_api.background.run_worker import RunRegistry
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import personas as personas_t
from persona_api.db.models import runs as runs_t
from persona_api.sandbox import SandboxRequestContext, get_sandbox_request_context
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.interaction_scope import InteractionRef, current_interaction
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from persona_runtime.agentic.events import RunEvent
    from sqlalchemy import Engine

_OWNER = "user_alice"
_PERSONA = "astrid"
_RUN_ID = "run_deadbeef"


class _RecordingLoop:
    """A fake AgenticLoop that records the sandbox context bound while it runs."""

    def __init__(self) -> None:
        self.seen_ctx: SandboxRequestContext | None = None
        self.seen_interaction: InteractionRef | None = None

    async def run(
        self,
        task: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        user_respond: Callable[[str], Awaitable[str]] | None = None,
        cancel_token: object | None = None,
    ) -> Run:
        # Capture the context bound at the moment the loop executes (this is when
        # the file tools resolve their scoped root).
        self.seen_ctx = get_sandbox_request_context()
        self.seen_interaction = current_interaction()
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


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = make_community_engine(tmp_path / "t.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="a@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: Astrid"))
        conn.execute(
            insert(runs_t).values(
                id=_RUN_ID, owner_id=_OWNER, persona_id=_PERSONA, task="t", status="running"
            )
        )
    return eng


@pytest.mark.asyncio
async def test_run_worker_binds_sandbox_context_to_owner_and_run_scope(engine: Engine) -> None:
    registry = RunRegistry(engine)
    loop = _RecordingLoop()
    handle = registry.start(run_id=_RUN_ID, owner_id=_OWNER, loop=loop, task_text="t")  # type: ignore[arg-type]
    assert handle.task is not None
    await handle.task

    ctx = loop.seen_ctx
    assert ctx is not None, "no SandboxRequestContext bound while the run executed"
    assert ctx.owner_id == _OWNER  # file-tool root scopes to the run's owner
    # The run gets its OWN sandbox session, distinct from any chat conversation.
    assert ctx.conversation_id == _RUN_ID
    assert ctx.session_id == f"{_OWNER}:{_RUN_ID}"


@pytest.mark.asyncio
async def test_run_worker_resets_sandbox_context_after_run(engine: Engine) -> None:
    registry = RunRegistry(engine)
    handle = registry.start(run_id=_RUN_ID, owner_id=_OWNER, loop=_RecordingLoop(), task_text="t")  # type: ignore[arg-type]
    assert handle.task is not None
    await handle.task
    # The contextvar is reset in the finally — nothing leaks into this scope.
    assert get_sandbox_request_context() is None


@pytest.mark.asyncio
async def test_run_worker_resets_context_even_when_loop_raises(engine: Engine) -> None:
    class _BoomLoop:
        async def run(self, *_a: object, **_k: object) -> Run:
            raise RuntimeError("boom")

    registry = RunRegistry(engine)
    handle = registry.start(run_id=_RUN_ID, owner_id=_OWNER, loop=_BoomLoop(), task_text="t")  # type: ignore[arg-type]
    assert handle.task is not None
    await handle.task  # the worker swallows the error (never crashes silently)
    assert get_sandbox_request_context() is None


@pytest.mark.asyncio
async def test_run_worker_binds_the_run_as_the_interaction(engine: Engine) -> None:
    """R9-253: a direct write in the run names the run, so a disclosure there gates it."""
    registry = RunRegistry(engine)
    loop = _RecordingLoop()
    handle = registry.start(run_id=_RUN_ID, owner_id=_OWNER, loop=loop, task_text="t")  # type: ignore[arg-type]
    assert handle.task is not None
    await handle.task
    assert loop.seen_interaction == InteractionRef(kind="agentic_run", id=_RUN_ID)
    assert current_interaction() is None


@pytest.mark.asyncio
async def test_a_bind_that_raises_still_frees_the_long_op_slot(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9-253 review: the interaction bind sits inside the try whose finally does cleanup."""
    import contextlib  # noqa: PLC0415

    from persona_api.background import run_worker  # noqa: PLC0415

    def broken(*_a: object) -> object:
        raise ValueError("an interaction needs a kind and an id")

    monkeypatch.setattr(run_worker, "bind_interaction", broken)
    registry = RunRegistry(engine)
    released: list[str] = []
    monkeypatch.setattr(registry, "_release_op_slot", lambda h: released.append(h.run_id))
    handle = registry.start(run_id=_RUN_ID, owner_id=_OWNER, loop=_RecordingLoop(), task_text="t")  # type: ignore[arg-type]
    assert handle.task is not None
    with contextlib.suppress(ValueError):
        await handle.task
    assert released == [_RUN_ID]
    assert get_sandbox_request_context() is None
