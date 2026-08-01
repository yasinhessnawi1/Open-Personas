"""The durable task stores — RLS-scoped, audited (Spec A2, T5).

:class:`TaskStore` owns the task entity (CRUD + lifecycle transitions);
:class:`CheckpointStore` owns the append-only checkpoint sequence + the atomic CAS write
that advances the head and accrues the ledger. Both run **owner-scoped through RLS** (every
operation re-binds ``app.current_user_id`` via :func:`~persona_api.db.engine.rls_connection`),
so a cross-tenant reach hits zero rows — the standing adversarial guarantee.

Discipline held here (mirrors A1's ``ScheduleStore``):

* **CQS** — reads only read; mutators write and return the post-mutation :class:`Task` as the
  *confirmation* of the new state, never a query result.
* **One ``AuditEvent`` per mutation** — exactly one ``audit_log`` row per create/transition/
  append (best-effort, never breaks the op — the project's auditability posture).
* **The entity is the source of truth for legality** — transitions call the frozen
  :class:`Task` methods (which raise :class:`~persona.errors.TaskStateError`); the store only
  persists what the entity produced.

**The CAS append (the durable half of A2-R-4).** :meth:`CheckpointStore.append` does the
load-bearing atomic write in ONE transaction: ``INSERT ... ON CONFLICT (task_id,
checkpoint_seq) DO NOTHING`` + an ``UPDATE tasks SET head_checkpoint_seq, ledger_* WHERE
head_checkpoint_seq IS NOT DISTINCT FROM :predecessor``. A re-delivered leg's checkpoint
no-ops the INSERT and the head CAS matches no row → a clean no-op (no double checkpoint, no
double-counted spend). The pure ``Task.advance_checkpoint`` guards the happy path
(strict-successor / non-terminal); the durable CAS handles concurrent re-delivery. The audit
fires only on a real append. (The audit row is a separate best-effort write per the
established ``audit_service`` posture; the load-bearing atomicity is checkpoint + ledger.)
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from persona.errors import TaskNotFoundError
from persona.logging import get_logger
from persona.tasks import (
    DEFAULT_CHECKPOINT_TOKEN_BUDGET,
    Task,
    enforce_checkpoint_budget,
)
from sqlalchemy import insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from persona_api.db.engine import rls_connection
from persona_api.db.models import task_checkpoints as checkpoints_t
from persona_api.db.models import tasks as tasks_t
from persona_api.services import audit_service
from persona_api.tasks.serde import (
    checkpoint_values,
    row_to_checkpoint,
    row_to_task,
    task_values,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from datetime import datetime

    from persona.tasks import SpendKind, TaskCheckpoint, WaitKind
    from sqlalchemy import Engine

__all__ = ["CheckpointStore", "TaskStore"]

_log = get_logger("api.tasks.store")


def _as_id_list(value: object) -> list[str]:
    """Coerce a stored JSON id array to ``list[str]`` (community SQLite may hand back text)."""
    if isinstance(value, str):
        loaded = json.loads(value)
        return [str(v) for v in loaded] if isinstance(loaded, list) else []
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


class TaskStore:
    """Owner-scoped, audited CRUD + lifecycle over the ``tasks`` table.

    Construct with the ``persona_app`` RLS engine — every operation re-binds the owner's GUC,
    so the store can never reach another tenant's rows.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- reads (CQS: no writes) --------------------------------------------

    def get(self, owner_id: str, task_id: str) -> Task:
        """Fetch one task. Raises :class:`TaskNotFoundError` on a miss (no oracle)."""
        with rls_connection(self._engine, owner_id) as conn:
            row = conn.execute(select(tasks_t).where(tasks_t.c.id == task_id)).mappings().first()
        if row is None:
            raise TaskNotFoundError("task not found", context={"task_id": task_id})
        return row_to_task(row)

    def list_for_owner(self, owner_id: str) -> list[Task]:
        """All of the owner's tasks, newest first (RLS-scoped read)."""
        with rls_connection(self._engine, owner_id) as conn:
            rows = (
                conn.execute(select(tasks_t).order_by(tasks_t.c.created_at.desc())).mappings().all()
            )
        return [row_to_task(r) for r in rows]

    def get_by_schedule_id(self, owner_id: str, schedule_id: str) -> Task | None:
        """The task backed by ``schedule_id``, or ``None`` (R9-037's delete-side linkage read).

        Rides the existing partial ``idx_tasks_schedule`` index. Used to capture the
        schedule→task linkage BEFORE a schedule delete — ``tasks.schedule_id`` has an
        ``ON DELETE SET NULL`` FK, so the linkage is unrecoverable once the delete commits.
        """
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(select(tasks_t).where(tasks_t.c.schedule_id == schedule_id))
                .mappings()
                .first()
            )
        return None if row is None else row_to_task(row)

    # --- mutations (CQS: return the post-mutation Task as confirmation) -----

    def create(self, task: Task) -> Task:
        """Persist a new task. Audits ``task.create``."""
        with rls_connection(self._engine, task.owner_id) as conn:
            conn.execute(insert(tasks_t).values(**task_values(task)))
        self._audit(task.owner_id, "task.create", task)
        return task

    def start(self, owner_id: str, task_id: str, *, now: datetime) -> Task:
        """``DEFINED → ACTIVE``."""
        return self._transition(owner_id, task_id, "task.start", lambda t: t.start(now=now))

    def begin_wait(self, owner_id: str, task_id: str, kind: WaitKind, *, now: datetime) -> Task:
        """``ACTIVE → WAITING(kind)``."""
        return self._transition(
            owner_id, task_id, "task.begin_wait", lambda t: t.begin_wait(kind, now=now)
        )

    def resume(self, owner_id: str, task_id: str, *, now: datetime) -> Task:
        """``WAITING → ACTIVE``."""
        return self._transition(owner_id, task_id, "task.resume", lambda t: t.resume(now=now))

    def complete(self, owner_id: str, task_id: str, *, now: datetime) -> Task:
        """``ACTIVE → COMPLETED``."""
        return self._transition(owner_id, task_id, "task.complete", lambda t: t.complete(now=now))

    def fail(self, owner_id: str, task_id: str, *, now: datetime) -> Task:
        """``ACTIVE | WAITING → FAILED``."""
        return self._transition(owner_id, task_id, "task.fail", lambda t: t.fail(now=now))

    def cancel(self, owner_id: str, task_id: str, *, now: datetime) -> Task:
        """``→ CANCELLED`` from any non-terminal state."""
        return self._transition(owner_id, task_id, "task.cancel", lambda t: t.cancel(now=now))

    def pause(self, owner_id: str, task_id: str, *, now: datetime) -> Task:
        """Set the ``paused`` overlay."""
        return self._transition(owner_id, task_id, "task.pause", lambda t: t.pause(now=now))

    def unpause(self, owner_id: str, task_id: str, *, now: datetime) -> Task:
        """Clear the ``paused`` overlay."""
        return self._transition(owner_id, task_id, "task.unpause", lambda t: t.unpause(now=now))

    def cas_unpause(self, owner_id: str, task_id: str, *, now: datetime) -> bool:
        """Atomically clear the ``paused`` overlay iff it is set (the A3 budget-extend gate).

        ``UPDATE ... SET paused=false WHERE id=:id AND paused=true`` — exactly one of two
        concurrent un-pauses wins (the DB serialises the row); the loser sees rowcount 0. This
        is the at-most-once gate the A3 budget extension keys on, so a duplicated extension
        reply cannot double-extend (only the un-pause winner applies the budget bump). Returns
        whether this call cleared the overlay.
        """
        with rls_connection(self._engine, owner_id) as conn:
            result = conn.execute(
                update(tasks_t)
                .where(tasks_t.c.id == task_id, tasks_t.c.paused.is_(True))
                .values(paused=False, updated_at=now)
            )
        if result.rowcount != 1:
            return False
        self._audit(owner_id, "task.unpause", self.get(owner_id, task_id))
        return True

    def record_run(self, owner_id: str, task_id: str, run_id: str) -> None:
        """Link a leg's agentic run to the task by appending to ``run_ids``.

        A task leg executes a real agentic run; ``run_ids`` is the linkage the Tasks
        surface drills through to the Spec-08 run viewer. Read-modify-write inside ONE
        row-locked transaction (``SELECT ... FOR UPDATE``) — the column is JSON, so an
        append is not a single atomic statement on both dialects. Appending the SAME id
        twice is a no-op, so a caller may safely retry.

        Args:
            owner_id: The RLS scope (the task's owner).
            task_id: The task to link.
            run_id: The ``runs`` row id to append.

        Raises:
            TaskNotFoundError: If the task is not visible to ``owner_id``.
        """
        with rls_connection(self._engine, owner_id) as conn:
            row = conn.execute(
                select(tasks_t.c.run_ids).where(tasks_t.c.id == task_id).with_for_update()
            ).first()
            if row is None:
                raise TaskNotFoundError("task not found", context={"task_id": task_id})
            existing = _as_id_list(row[0])
            if run_id in existing:
                return
            conn.execute(
                update(tasks_t).where(tasks_t.c.id == task_id).values(run_ids=[*existing, run_id])
            )
        audit_service.record(
            engine=self._engine,
            user_id=owner_id,
            action="task.record_run",
            target=task_id,
            metadata={"run_id": run_id},
        )

    # --- internals ----------------------------------------------------------

    def _transition(
        self, owner_id: str, task_id: str, action: str, apply: Callable[[Task], Task]
    ) -> Task:
        """Fetch → apply the entity transition (may raise) → persist state → audit."""
        current = self.get(owner_id, task_id)
        updated = apply(current)
        self._persist_state(owner_id, task_id, updated)
        self._audit(owner_id, action, updated)
        return updated

    def _persist_state(self, owner_id: str, task_id: str, task: Task) -> None:
        """RLS-scoped UPDATE of the transition columns; raise NotFound if no row matched."""
        with rls_connection(self._engine, owner_id) as conn:
            result = conn.execute(
                update(tasks_t)
                .where(tasks_t.c.id == task_id)
                .values(
                    state=task.state.value,
                    paused=task.paused,
                    wait_kind=task.wait_kind.value if task.wait_kind is not None else None,
                    updated_at=task.updated_at,
                )
            )
            if result.rowcount != 1:
                raise TaskNotFoundError("task not found", context={"task_id": task_id})

    def _audit(self, owner_id: str, action: str, task: Task) -> None:
        audit_service.record(
            engine=self._engine,
            user_id=owner_id,
            action=action,
            target=task.id,
            metadata={"state": task.state.value, "persona_id": task.persona_id},
        )


class CheckpointStore:
    """Owner-scoped append-only checkpoint sequence + the atomic CAS write (A2-R-4)."""

    def __init__(
        self, engine: Engine, *, token_budget: int = DEFAULT_CHECKPOINT_TOKEN_BUDGET
    ) -> None:
        self._engine = engine
        self._budget = token_budget

    # --- reads (CQS) --------------------------------------------------------

    def get_latest(self, owner_id: str, task_id: str) -> TaskCheckpoint | None:
        """The head checkpoint for a task, or ``None`` before the first leg."""
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(
                    select(checkpoints_t)
                    .where(checkpoints_t.c.task_id == task_id)
                    .order_by(checkpoints_t.c.checkpoint_seq.desc())
                    .limit(1)
                )
                .mappings()
                .first()
            )
        return row_to_checkpoint(row) if row is not None else None

    def list_recent(self, owner_id: str, task_id: str, *, limit: int) -> list[TaskCheckpoint]:
        """The most-recent ``limit`` checkpoints, newest first (the last-N window source)."""
        with rls_connection(self._engine, owner_id) as conn:
            rows = (
                conn.execute(
                    select(checkpoints_t)
                    .where(checkpoints_t.c.task_id == task_id)
                    .order_by(checkpoints_t.c.checkpoint_seq.desc())
                    .limit(limit)
                )
                .mappings()
                .all()
            )
        return [row_to_checkpoint(r) for r in rows]

    # --- the atomic CAS append (mutation) -----------------------------------

    def append(
        self,
        task: Task,
        checkpoint: TaskCheckpoint,
        *,
        spend: Mapping[SpendKind, int] | None = None,
        now: datetime,
    ) -> Task:
        """Append ``checkpoint`` and advance the head + ledger atomically (A2-R-4).

        **The CAS predecessor is the checkpoint's OWN predecessor (``seq - 1``)** — the
        job-creation anchor, NOT the live ``task.head``. A re-delivered leg carries the same
        ``checkpoint_seq`` (the job payload fixes it), so once the first delivery advanced the
        head the CAS (``head IS NOT DISTINCT FROM seq-1``) matches no row → a clean no-op. The
        durable CAS is the **single** idempotency mechanism (no second job-layer check). The
        budget gate runs first (so an oversized checkpoint writes nothing); ``advance_checkpoint``
        is the entity guard on the applied path (the durable head was ``seq-1`` → it is valid).

        Args:
            task: The current durable task (freshly fetched; head = ``seq-1`` on a fresh leg).
            checkpoint: The checkpoint to append; ``checkpoint_seq`` is the job-fixed anchor.
            spend: Per-kind leg spend to accrue into the ledger this append.
            now: The write time.

        Returns:
            The post-append task (head advanced + ledger accrued), or — on a re-delivery
            no-op — the current durable task.

        Raises:
            CheckpointTooLargeError: If the checkpoint's accumulating core exceeds the budget.
        """
        enforce_checkpoint_budget(checkpoint, token_budget=self._budget)
        seq = checkpoint.checkpoint_seq
        predecessor = seq - 1 if seq > 0 else None
        new_ledger = task.ledger
        for kind, micros in (spend or {}).items():
            new_ledger = new_ledger.record(kind, micros)

        with rls_connection(self._engine, task.owner_id) as conn:
            conn.execute(
                pg_insert(checkpoints_t)
                .values(**checkpoint_values(checkpoint, task.owner_id))
                .on_conflict_do_nothing(constraint="uq_task_checkpoints_task_seq")
            )
            result = conn.execute(
                update(tasks_t)
                .where(
                    tasks_t.c.id == task.id,
                    tasks_t.c.head_checkpoint_seq.is_not_distinct_from(predecessor),
                )
                .values(
                    head_checkpoint_seq=seq,
                    ledger_model_micros=new_ledger.model_micros,
                    ledger_sandbox_micros=new_ledger.sandbox_micros,
                    ledger_external_micros=new_ledger.external_micros,
                    updated_at=now,
                )
            )
            applied = result.rowcount == 1

        if applied:
            # The durable head was seq-1 → the entity's strict-successor guard is satisfied.
            advanced = task.advance_checkpoint(seq, now=now)
            for kind, micros in (spend or {}).items():
                advanced = advanced.record_spend(kind, micros, now=now)
            self._audit_append(task.owner_id, advanced, checkpoint)
            return advanced
        # Re-delivery: the head already advanced past seq-1 → a clean no-op (the CAS truth).
        _log.info(
            "checkpoint append no-op (re-delivery)",
            task_id=task.id,
            checkpoint_seq=seq,
        )
        return self._read_task(task.owner_id, task.id)

    # --- internals ----------------------------------------------------------

    def _read_task(self, owner_id: str, task_id: str) -> Task:
        with rls_connection(self._engine, owner_id) as conn:
            row = conn.execute(select(tasks_t).where(tasks_t.c.id == task_id)).mappings().first()
        if row is None:
            raise TaskNotFoundError("task not found", context={"task_id": task_id})
        return row_to_task(row)

    def _audit_append(self, owner_id: str, task: Task, checkpoint: TaskCheckpoint) -> None:
        metadata: dict[str, Any] = {
            "checkpoint_seq": str(checkpoint.checkpoint_seq),
            "content_hash": checkpoint.content_hash,
            "ledger_total_micros": str(task.ledger.total_micros),
        }
        audit_service.record(
            engine=self._engine,
            user_id=owner_id,
            action="checkpoint.append",
            target=task.id,
            metadata=metadata,
        )
