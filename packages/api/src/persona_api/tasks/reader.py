"""The api implementation of the A4 task-state reader (Spec A4, T7; A4-D-5).

:class:`APITaskStateReader` satisfies the core :class:`~persona.tasks.TaskStateReader` protocol
over the RLS-scoped :class:`TaskStore` / :class:`CheckpointStore`. It **binds the owner** at
construction (the dispatch-time provider builds a fresh reader per request from the
``current_user_id`` contextvar), so every read is owner-scoped: ``TaskStore.get`` raises
``TaskNotFoundError`` for another tenant's task (RLS makes a cross-tenant read a not-found, never
a leak), and ``list_active`` only ever returns this owner's rows. Core ⊥ api: the protocol lives
in core, this impl in api.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.tasks import is_terminal

if TYPE_CHECKING:
    from persona.tasks import Task, TaskCheckpoint

    from persona_api.tasks.store import CheckpointStore, TaskStore

__all__ = ["APITaskStateReader"]


class APITaskStateReader:
    """Owner-scoped, read-only task state over the RLS stores (a ``TaskStateReader``)."""

    def __init__(self, tasks: TaskStore, checkpoints: CheckpointStore, owner_id: str) -> None:
        """Bind the owner; every read scopes to it (the provider builds one per dispatch)."""
        self._tasks = tasks
        self._checkpoints = checkpoints
        self._owner_id = owner_id

    def get_task(self, task_id: str) -> Task:
        """The owner's task; ``TaskNotFoundError`` for a miss or another tenant's task."""
        return self._tasks.get(self._owner_id, task_id)

    def get_latest_checkpoint(self, task_id: str) -> TaskCheckpoint | None:
        """The head checkpoint, or ``None`` before the first leg."""
        return self._checkpoints.get_latest(self._owner_id, task_id)

    def list_active(self) -> list[Task]:
        """The owner's non-terminal tasks (what the persona is actually working on)."""
        return [t for t in self._tasks.list_for_owner(self._owner_id) if not is_terminal(t.state)]
