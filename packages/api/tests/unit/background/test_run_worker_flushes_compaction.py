"""The run worker charges a compaction the run never got past (R9-299).

The billing watcher holds a compaction's usage until the next step or summary, so the
compaction shares that call's floor. A run that ends straight after a compaction (an
error, a salvage that failed) has no next call, so the worker flushes the watcher in its
terminal ``finally``: the held provider cost is charged once, under one floor.

Driven through the real :class:`RunRegistry` on a community (SQLite) database, with a
loop double that reports usage the way the real loop does.
"""

# ruff: noqa: ARG002 - the loop double's signature mirrors AgenticLoop.run.

from __future__ import annotations

import asyncio
import contextlib
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
from persona_api.sandbox import get_sandbox_request_context
from persona_runtime.agentic.run import Run, StepUsage
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from sqlalchemy import Engine

_OWNER = "user_alice"
_PERSONA = "astrid"
_RUN_ID = "run_deadbeef"


def _usage(prompt: int, completion: int, call: str) -> StepUsage:
    return StepUsage(
        step=0,
        provider="anthropic",
        model="claude-sonnet-4-6",
        prompt_tokens=prompt,
        completion_tokens=completion,
        cost_usd=None,
        call=call,  # type: ignore[arg-type]
    )


class _Policy:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def capture_up_to(self, **kw: object) -> tuple[int, int]:
        self.calls.append(kw)
        amount = kw["amount"]
        assert isinstance(amount, int)
        return amount, 100


class _CompactsThenBreaks:
    """A step, then a compaction, then the run fails before anything else is billed."""

    async def run(
        self,
        task: str,
        on_event: object | None = None,
        user_respond: object | None = None,
        cancel_token: object | None = None,
        on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,
    ) -> Run:
        assert on_step_usage is not None
        await on_step_usage(_usage(100, 100, "step"))
        await on_step_usage(_usage(2000, 100, "compaction"))
        raise RuntimeError("the provider went away")


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
async def test_a_run_that_ends_after_a_compaction_still_pays_for_it(engine: Engine) -> None:
    policy = _Policy()
    registry = RunRegistry(engine, credits_policy=policy)  # type: ignore[arg-type]

    handle = registry.start(
        run_id=_RUN_ID,
        owner_id=_OWNER,
        loop=_CompactsThenBreaks(),  # type: ignore[arg-type]
        task_text="t",
    )
    assert handle.task is not None
    await handle.task

    step, held = policy.calls
    assert step["cost_cents"] == pytest.approx(0.18)
    # The held compaction (2000 in, 100 out = 0.75 cents), charged once at the end.
    assert held["cost_cents"] == pytest.approx(0.75)
    assert held["amount"] == 1
    assert held["user_id"] == _OWNER


class _CancelledOnTheFlush(_Policy):
    """The step captures; the teardown flush is cancelled mid-capture."""

    def capture_up_to(self, **kw: object) -> tuple[int, int]:
        if self.calls:
            raise asyncio.CancelledError
        return super().capture_up_to(**kw)


@pytest.mark.asyncio
async def test_a_cancelled_flush_still_runs_the_whole_teardown(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second cancellation during the flush's capture is a ``BaseException``: the flush
    must not be what skips the slot release, the context resets and the end-of-stream
    sentinel the SSE reader waits on."""
    registry = RunRegistry(engine, credits_policy=_CancelledOnTheFlush())  # type: ignore[arg-type]
    released: list[str] = []
    monkeypatch.setattr(registry, "_release_op_slot", lambda h: released.append(h.run_id))

    handle = registry.start(
        run_id=_RUN_ID,
        owner_id=_OWNER,
        loop=_CompactsThenBreaks(),  # type: ignore[arg-type]
        task_text="t",
    )
    assert handle.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await handle.task

    assert released == [_RUN_ID]
    assert get_sandbox_request_context() is None
    events = [handle.events.get_nowait() for _ in range(handle.events.qsize())]
    assert events[-1] is None  # the sentinel was sent
