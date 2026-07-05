"""Spec K8 T1b — no episodic writer reads the store to mint an id (acceptance 1).

The bug class: ``index = len(store.get_all(persona_id, include_superseded=True))``
made every episodic write an O(N) read AND raced under concurrent writers. The
sweep found it in six writers (chat loop, voice recorder, agentic loop, task
milestone recorder, origination recorder, CLI); all now mint uuidv7 ids
(K8-D-6). These spies pin the runtime-owned writers; the voice/api siblings are
pinned in their own packages' suites.
"""

# ruff: noqa: ARG002 — the spy store ignores protocol args by design.
from __future__ import annotations

from types import SimpleNamespace

from persona.schema.chunks import PersonaChunk, WriteSource
from persona_runtime.legs.memory import MilestoneRecorder, TaskMilestone
from persona_runtime.loop import ConversationLoop


class _SpyStore:
    """Records method calls; write-only episodic double."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.chunks: list[PersonaChunk] = []

    def write(self, persona_id: str, chunks: list[PersonaChunk], **_: object) -> None:
        self.calls.append("write")
        self.chunks.extend(chunks)

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        self.calls.append("get_all")
        return list(self.chunks)


def test_chat_write_episodic_never_reads_the_store() -> None:
    store = _SpyStore()
    ConversationLoop._write_episodic(  # noqa: SLF001 — driving the real writer unbound
        SimpleNamespace(_stores={"episodic": store}),  # type: ignore[arg-type]
        "p1",
        "hello",
        "hi there",
    )
    assert store.calls == ["write"]  # no get_all — the O(N)-per-write read is gone
    assert len(store.chunks) == 1
    assert store.chunks[0].provenance is not None
    assert store.chunks[0].provenance.source == WriteSource.SYSTEM


def test_milestone_recorder_never_reads_the_store() -> None:
    store = _SpyStore()
    MilestoneRecorder(store).record(  # type: ignore[arg-type]
        "p1", TaskMilestone.COMPLETED, "did the thing", task_id="t1"
    )
    assert store.calls == ["write"]
    assert store.chunks[0].metadata["source"] == "task_milestone"


def test_two_writers_racing_produce_distinct_ids() -> None:
    # The count-race regression: two writers over the SAME store state used to
    # compute the same index and collide. Minted ids cannot collide.
    store = _SpyStore()
    ns = SimpleNamespace(_stores={"episodic": store})
    ConversationLoop._write_episodic(ns, "p1", "a", "b")  # type: ignore[arg-type]  # noqa: SLF001
    ConversationLoop._write_episodic(ns, "p1", "c", "d")  # type: ignore[arg-type]  # noqa: SLF001
    ids = [c.id for c in store.chunks]
    assert len(set(ids)) == 2  # noqa: PLR2004
