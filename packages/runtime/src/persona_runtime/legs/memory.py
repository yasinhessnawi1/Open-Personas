"""Milestone-granularity episodic distillation for tasks (Spec A2, T10; D-A2-4).

A diligent overnight task runs many legs; writing each leg's output to the persona's episodic
memory would flood it (the spec §7 "memory spam" risk). The discipline (D-A2-4):

1. The leg composes the unmodified ``AgenticLoop`` with a **task-scoped episodic sink** in
   ``stores["episodic"]`` (pure DI — :class:`TaskEpisodicSink`), so the loop's per-run
   ``_write_episodic_summary`` lands in a throwaway buffer, NOT the persona's episodic. Zero
   loop edit; zero per-leg spam.
2. The **task layer** promotes only **milestones** (started / major-progress / waiting /
   completed / failed) into the persona's real episodic store — the restraint K2 applies to
   extraction: record what *matters*, not what *happened*. ``milestone_for`` is the pure gate
   (a CONTINUE leg that established no new conclusion is NOT a milestone).

This is the A2↔memory seam; graph writes (a task-learned user fact) ride **K2's gated path**
(the loop's K2 tools / post-leg synthesis), never a raw graph write — the leg has no raw graph
store, so that is structural.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id

if TYPE_CHECKING:
    from persona.stores.protocol import MemoryStore
    from persona.tasks import TaskCheckpoint

    from persona_runtime.legs.executor import LegDisposition

__all__ = [
    "MilestoneRecorder",
    "TaskEpisodicSink",
    "TaskMilestone",
    "milestone_for",
]


class TaskMilestone(StrEnum):
    """The episodic milestones a task records (NOT every leg — restraint, D-A2-4)."""

    TASK_STARTED = "task_started"
    MAJOR_PROGRESS = "major_progress"
    WAITING = "waiting"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskEpisodicSink:
    """A throwaway episodic store the leg injects so the loop's per-run write isn't promoted.

    Satisfies the slice of the ``MemoryStore`` protocol the loop's ``_write_episodic_summary``
    uses (``write`` + ``get_all``); everything else is empty. The buffered chunks are
    task-scoped and discarded — only :class:`MilestoneRecorder` writes to the persona's real
    episodic store.
    """

    def __init__(self) -> None:
        self._buffer: list[PersonaChunk] = []

    def write(  # noqa: PLR0913 — mirrors the MemoryStore.write protocol signature
        self,
        persona_id: str,  # noqa: ARG002 — protocol signature; the sink is persona-agnostic
        chunks: list[PersonaChunk],
        *,
        source: WriteSource = WriteSource.SYSTEM,  # noqa: ARG002 — protocol
        written_by: str | None = None,  # noqa: ARG002 — protocol
        reason: str | None = None,  # noqa: ARG002 — protocol
        force: bool = False,  # noqa: ARG002 — protocol
    ) -> None:
        self._buffer.extend(chunks)

    def get_all(
        self,
        persona_id: str,  # noqa: ARG002 — protocol signature
        *,
        include_superseded: bool = False,  # noqa: ARG002 — protocol signature
    ) -> list[PersonaChunk]:
        return list(self._buffer)

    def query(
        self,
        persona_id: str,  # noqa: ARG002 — protocol signature
        query_text: str,  # noqa: ARG002 — protocol signature
        top_k: int = 5,  # noqa: ARG002 — protocol signature
    ) -> list[PersonaChunk]:
        return []

    def history(
        self,
        persona_id: str,  # noqa: ARG002 — protocol signature
        logical_id: str,  # noqa: ARG002 — protocol signature
    ) -> list[PersonaChunk]:
        return []


def milestone_for(
    *,
    is_first_leg: bool,
    prior_checkpoint: TaskCheckpoint | None,
    new_checkpoint: TaskCheckpoint,
    disposition: LegDisposition,
) -> TaskMilestone | None:
    """The pure milestone gate — ``None`` when a leg is not worth an episodic entry.

    Restraint: only a terminal outcome, the first leg, or a leg that established a NEW
    conclusion is a milestone. A CONTINUE leg that merely advanced without a new conclusion
    records nothing (no per-leg spam). ``waiting`` milestones are emitted at the wait
    transition (by the continuation), not here.
    """
    from persona_runtime.legs.executor import LegDisposition as _Disp

    if disposition == _Disp.COMPLETED:
        return TaskMilestone.COMPLETED
    if disposition == _Disp.FAILED:
        return TaskMilestone.FAILED
    if is_first_leg:
        return TaskMilestone.TASK_STARTED
    prior_n = len(prior_checkpoint.progress_conclusions) if prior_checkpoint is not None else 0
    if len(new_checkpoint.progress_conclusions) > prior_n:
        return TaskMilestone.MAJOR_PROGRESS
    return None


class MilestoneRecorder:
    """Writes ONE episodic chunk per milestone into the persona's real episodic store."""

    def __init__(self, episodic_store: MemoryStore) -> None:
        self._store = episodic_store

    def record(
        self, persona_id: str, milestone: TaskMilestone, summary: str, *, task_id: str
    ) -> None:
        """Write a milestone episodic chunk (tagged ``source='task_milestone'``)."""
        now = datetime.now(UTC)
        # Minted uuidv7 id (K8-D-6) — the former store-count index was an
        # O(N) read per write and raced under concurrent writers.
        chunk_id = mint_chunk_id(persona_id, "episodic")
        self._store.write(
            persona_id,
            [
                PersonaChunk(
                    id=chunk_id,
                    text=summary,
                    metadata={
                        "source": "task_milestone",
                        "milestone": milestone.value,
                        "task_id": task_id,
                    },
                    created_at=now,
                    provenance=ChunkProvenance(
                        source=WriteSource.SYSTEM,
                        logical_id=chunk_id,
                        version=1,
                        written_at=now,
                        written_by="task.milestone",
                    ),
                )
            ],
            source=WriteSource.SYSTEM,
            written_by="task.milestone",
        )
