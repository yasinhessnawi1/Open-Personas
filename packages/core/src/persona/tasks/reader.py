"""Read-only task state for grounded introspection (Spec A4, T7; A4-D-5).

The persona answers "how's it going?" *only* from here — and "here" exposes nothing but typed
conclusions. :class:`TaskStateReader` returns :class:`Task` + :class:`TaskCheckpoint`, and the
checkpoint is **conclusions-not-transcripts by construction** (D-A2-1): its content is distilled
findings, decisions, plan, and waits — never raw events. There is **no method and no field** on
this surface that yields transcript text, so a confabulated progress report is walled off *at the
data layer*, not by a prompt instruction (the steer-③ structural answer to criterion 7).

The projection (:func:`project_task_state`) maps the entity + head checkpoint onto the honest
state matrix — just-created / progressing / waiting-on-user / scheduled / completed / failed /
cancelled / paused — so the persona can be accurate including the unflattering cases. Owner
scoping lives in the *implementation* (it binds the owner and enforces RLS); this protocol takes
no owner_id, so a tool holding a reader can only ever see its own caller's tasks.
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic needs runtime access (TaskStateView field)
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from persona.tasks.state import TaskState, WaitKind

if TYPE_CHECKING:
    from persona.tasks.checkpoint import TaskCheckpoint
    from persona.tasks.entity import Task

__all__ = [
    "IntrospectionStatus",
    "TaskStateReader",
    "TaskStateView",
    "TaskSummary",
    "project_task_state",
    "summarise_task",
]


class IntrospectionStatus(StrEnum):
    """The human-facing status the persona narrates — the honest state matrix (A4-D-5)."""

    JUST_CREATED = "just_created"
    PROGRESSING = "progressing"
    WAITING_ON_USER = "waiting_on_user"
    SCHEDULED = "scheduled"  # waiting until a scheduled time
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED = "paused"


@runtime_checkable
class TaskStateReader(Protocol):
    """Owner-scoped, read-only access to task state (the introspection tool's only source).

    Implementations bind the owner and read through RLS; this surface exposes only typed
    entities (no transcripts). All three methods are reads (CQS).
    """

    def get_task(self, task_id: str) -> Task:
        """The task; raises ``TaskNotFoundError`` on a miss (cross-tenant reads as not-found)."""
        ...

    def get_latest_checkpoint(self, task_id: str) -> TaskCheckpoint | None:
        """The head checkpoint, or ``None`` before the first leg (a young task is honestly thin)."""
        ...

    def list_active(self) -> list[Task]:
        """The caller's own non-terminal-by-default tasks (for list-then-introspect)."""
        ...

    def list_recent_terminal(self, *, limit: int) -> list[Task]:
        """The caller's most-recently-finished tasks, newest first (Spec A5, A5-D-X-reads).

        Additive read for the initiative scan's task-history lens: completed
        missions whose results suggest follow-ups (spec A5 §2). Terminal tasks
        only (``completed | failed | cancelled``), ordered by ``updated_at``
        descending, bounded by ``limit``. Transcript-free by construction like
        every read here (conclusions live on checkpoints/reports, never events).
        """
        ...


class TaskSummary(BaseModel):
    """A one-line task summary for the list view (id + goal + status)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    goal: str
    status: IntrospectionStatus


class TaskStateView(BaseModel):
    """The full grounded state the persona may narrate — typed conclusions only, no transcripts."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    goal: str
    status: IntrospectionStatus
    wait_reason: str | None  # the checkpoint's ``blocked_on`` — the authoritative why-waiting
    progress: tuple[str, ...]  # distilled progress_conclusions (never events)
    next_step: str
    open_questions: tuple[str, ...]
    spent_micros: int
    updated_at: datetime


def _derive_status(task: Task, *, has_checkpoint: bool) -> IntrospectionStatus:
    """Map a task's lifecycle (+ pause overlay + wait kind) onto the narratable status."""
    if task.paused:
        return IntrospectionStatus.PAUSED
    if task.state is TaskState.COMPLETED:
        return IntrospectionStatus.COMPLETED
    if task.state is TaskState.FAILED:
        return IntrospectionStatus.FAILED
    if task.state is TaskState.CANCELLED:
        return IntrospectionStatus.CANCELLED
    if task.state is TaskState.WAITING:
        if task.wait_kind is WaitKind.UNTIL_TIME:
            return IntrospectionStatus.SCHEDULED
        return IntrospectionStatus.WAITING_ON_USER  # ON_USER (and the reserved ON_EVENT)
    if task.state is TaskState.DEFINED:
        return IntrospectionStatus.JUST_CREATED
    # ACTIVE: progressing once a leg has written a checkpoint, else still just-created.
    return IntrospectionStatus.PROGRESSING if has_checkpoint else IntrospectionStatus.JUST_CREATED


def summarise_task(task: Task) -> TaskSummary:
    """One-line summary (status uses the head-checkpoint pointer; no checkpoint fetch needed)."""
    return TaskSummary(
        task_id=task.id,
        goal=task.contract.goal,
        status=_derive_status(task, has_checkpoint=task.head_checkpoint_seq is not None),
    )


def project_task_state(task: Task, checkpoint: TaskCheckpoint | None) -> TaskStateView:
    """Project the task + its head checkpoint onto the grounded view (pure; transcript-free).

    Reads only typed, distilled fields; a young task with no checkpoint yields an honestly thin
    view (empty progress, no next step) rather than invented detail.
    """
    return TaskStateView(
        task_id=task.id,
        goal=task.contract.goal,
        status=_derive_status(task, has_checkpoint=checkpoint is not None),
        wait_reason=checkpoint.blocked_on if checkpoint is not None else None,
        progress=checkpoint.progress_conclusions if checkpoint is not None else (),
        next_step=checkpoint.next_step if checkpoint is not None else "",
        open_questions=checkpoint.open_questions if checkpoint is not None else (),
        spent_micros=task.ledger.total_micros,
        updated_at=task.updated_at,
    )
