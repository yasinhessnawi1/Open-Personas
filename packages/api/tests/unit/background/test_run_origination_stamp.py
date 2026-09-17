"""A reopened run says its persona spoke first (part3 F11, the record half).

Within-runtime origination (Spec C0) fires AFTER ``persist_final``, and the
``persona_originated`` event it pushes onto the live stream never enters the event log,
so the durable record used to end in silence. The stamp is a :class:`PersonaOriginatedNote`
on the run's last step, written only once the message has actually been recorded and
delivered.

These tests drive the REAL chain on the community engine: the real ``RunRegistry`` runs
a scripted loop to completion, the real :class:`WithinRuntimeOriginator` records the
message into a started conversation and pushes it onto the run's own queue, and the
worker stamps the record from the receipt. Nothing forces the end state by hand.
"""

# ruff: noqa: ARG002 — the scripted loop's signature mirrors AgenticLoop.run.

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.schema.chunks import PersonaChunk
from persona_api.background.run_worker import RunRegistry
from persona_api.config import Edition
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import messages as messages_t
from persona_api.db.models import personas as personas_t
from persona_api.db.models import runs as runs_t
from persona_api.services.within_runtime_origination import (
    ORIGINATED_EVENT_TYPE,
    WithinRuntimeOriginator,
)
from persona_runtime.agentic.events import RunEvent
from persona_runtime.agentic.run import Run, RunStatus
from persona_runtime.agentic.step import CallSkippedNote, Step, StepType
from sqlalchemy import insert, select

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_OWNER = "user_alice"
_PERSONA = "astrid"
_RUN = "run_originated"
_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: |
    x
  language_default: en
  constraints: []
"""


class _InMemoryBackend:
    """The memory transport the episodic store writes through; enough for one chunk."""

    def __init__(self) -> None:
        self.store: dict[tuple[str, str], list[PersonaChunk]] = {}

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        key = (persona_id, store_kind)
        existing = self.store.setdefault(key, [])
        ids = {c.id for c in chunks}
        self.store[key] = [c for c in existing if c.id not in ids] + list(chunks)

    def query(
        self,
        *,
        persona_id: str,
        store_kind: str,
        text: str,
        top_k: int,
        where: dict[str, Any] | None = None,
    ) -> list[PersonaChunk]:
        return list(self.store.get((persona_id, store_kind), []))[:top_k]

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return list(self.store.get((persona_id, store_kind), []))

    def count(self, *, persona_id: str, store_kind: str, include_superseded: bool = False) -> int:
        return len(self.store.get((persona_id, store_kind), []))

    def recent(self, *, persona_id: str, store_kind: str, limit: int) -> list[PersonaChunk]:
        rows = sorted(
            self.store.get((persona_id, store_kind), []),
            key=lambda c: (c.created_at, c.id),
            reverse=True,
        )
        return rows[:limit]

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        wanted = set(logical_ids)
        return [
            c
            for c in self.store.get((persona_id, store_kind), [])
            if c.provenance is not None and c.provenance.logical_id in wanted
        ]

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.store.pop((persona_id, store_kind), None)

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        key = (persona_id, store_kind)
        self.store[key] = [c for c in self.store.get(key, []) if c.id not in set(ids)]


class _ScriptedLoop:
    """Runs to COMPLETED with a final step; ``output`` decides whether there is anything
    to originate (``None`` is the no-output run the originator declines)."""

    def __init__(self, output: str | None) -> None:
        self._output = output

    async def run(
        self,
        task: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        user_respond: Callable[[str], Awaitable[str]] | None = None,
        cancel_token: object | None = None,
    ) -> Run:
        assert on_event is not None
        await on_event(RunEvent.started(task))
        await on_event(RunEvent.completed(1, self._output or ""))
        now = datetime.now(UTC)
        return Run(
            persona_id=_PERSONA,
            task=task,
            status=RunStatus.COMPLETED,
            steps=[
                Step(type=StepType.REASONING, content="thinking"),
                Step(
                    type=StepType.FINAL,
                    content=self._output,
                    # A guard note already on the last step must survive the stamp.
                    notes=[CallSkippedNote(tool="web_search", guard="cached_read")],
                ),
            ],
            output=self._output,
            started_at=now,
            finished_at=now,
        )


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "t.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="a@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml=_YAML))
        conn.execute(
            insert(runs_t).values(
                id=_RUN, owner_id=_OWNER, persona_id=_PERSONA, task="t", status="running"
            )
        )
    yield eng
    eng.dispose()


@pytest.fixture
def originator(engine: Engine, tmp_path: Path) -> WithinRuntimeOriginator:
    return WithinRuntimeOriginator(
        rls_engine=engine,
        memory_backend=_InMemoryBackend(),  # type: ignore[arg-type]
        edition=Edition.community,
        audit_root=tmp_path / "audit",
    )


def _persisted_steps(engine: Engine) -> list[dict[str, Any]]:
    with engine.begin() as conn:
        steps = conn.execute(select(runs_t.c.steps).where(runs_t.c.id == _RUN)).scalar_one()
    if isinstance(steps, str):  # sqlite JSON round-trips as text
        steps = json.loads(steps)
    return list(steps) if steps else []


async def _run_to_end(registry: RunRegistry, output: str | None) -> list[RunEvent]:
    handle = registry.start(
        run_id=_RUN,
        owner_id=_OWNER,
        loop=_ScriptedLoop(output),  # type: ignore[arg-type]
        task_text="t",
    )
    assert handle.task is not None
    await handle.task
    events: list[RunEvent] = []
    while not handle.events.empty():
        ev = handle.events.get_nowait()
        if ev is None:
            break
        events.append(ev)
    return events


@pytest.mark.asyncio
async def test_the_reopened_run_says_the_persona_spoke_first(
    engine: Engine, originator: WithinRuntimeOriginator
) -> None:
    events = await _run_to_end(RunRegistry(engine, origination=originator), "All done.")

    # The live half still happens: the message rode the run's own open stream.
    live = [e for e in events if e.type == ORIGINATED_EVENT_TYPE]
    assert len(live) == 1
    conversation_id = live[0].data["conversation_id"]
    assert conversation_id

    # The record half is the fix: the last step carries the note, pointing at the SAME
    # conversation the live event named, and the guard note already there is kept.
    steps = _persisted_steps(engine)
    assert steps[0]["notes"] == []
    assert steps[-1]["type"] == "final"
    assert steps[-1]["notes"] == [
        {"kind": "call_skipped", "tool": "web_search", "guard": "cached_read"},
        {"kind": "persona_originated", "conversation_id": conversation_id},
    ]

    # And the conversation it points at really holds the originated message.
    with engine.begin() as conn:
        rows = (
            conn.execute(
                select(messages_t.c.content, messages_t.c.originated).where(
                    messages_t.c.conversation_id == conversation_id
                )
            )
            .mappings()
            .all()
        )
    assert [(r["content"], bool(r["originated"])) for r in rows] == [("All done.", True)]


@pytest.mark.asyncio
async def test_the_stamp_leaves_the_rest_of_the_terminal_record_alone(
    engine: Engine, originator: WithinRuntimeOriginator
) -> None:
    await _run_to_end(RunRegistry(engine, origination=originator), "All done.")

    with engine.begin() as conn:
        row = conn.execute(select(runs_t).where(runs_t.c.id == _RUN)).mappings().one()
    assert row["status"] == "completed"
    assert row["output"] == "All done."
    assert row["error"] is None
    assert row["finished_at"] is not None


@pytest.mark.asyncio
async def test_no_stamp_when_origination_is_not_wired(engine: Engine) -> None:
    events = await _run_to_end(RunRegistry(engine), "All done.")

    assert not [e for e in events if e.type == ORIGINATED_EVENT_TYPE]
    steps = _persisted_steps(engine)
    assert steps[-1]["notes"] == [
        {"kind": "call_skipped", "tool": "web_search", "guard": "cached_read"}
    ]


@pytest.mark.asyncio
async def test_no_stamp_when_the_run_had_nothing_to_say(
    engine: Engine, originator: WithinRuntimeOriginator
) -> None:
    # The originator declines a run with no output; the record must not claim otherwise.
    events = await _run_to_end(RunRegistry(engine, origination=originator), None)

    assert not [e for e in events if e.type == ORIGINATED_EVENT_TYPE]
    assert all(n["kind"] != "persona_originated" for n in _persisted_steps(engine)[-1]["notes"])
    with engine.begin() as conn:
        count = conn.execute(select(messages_t.c.id)).all()
    assert count == []
