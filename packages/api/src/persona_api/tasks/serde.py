"""Entity ↔ row serialization for the task model (Spec A2, T5).

The single place that maps the frozen ``persona.tasks`` entities to/from their durable
rows (``tasks`` / ``task_checkpoints``). Kept apart from the store so both stores share one
mapping and the column set lives in one spot.
"""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Any

from persona.tasks import Contract, CostLedger, Task, TaskCheckpoint, TaskState, WaitKind

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import RowMapping

__all__ = [
    "checkpoint_values",
    "row_to_checkpoint",
    "row_to_task",
    "task_values",
]


def _utc(value: datetime) -> datetime:
    """Re-attach UTC to a stored instant from a NOT NULL column.

    The non-optional twin of :func:`~persona_api.db.engine.aware_utc`: storage convention
    IS UTC, but the community SQLite engine keeps no tzinfo and hands instants back naive,
    which the ``Task`` validator rejects outright (the R4-C1 finding family). Postgres
    values are already aware and pass through untouched.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def task_values(task: Task) -> dict[str, Any]:
    """The full ``tasks`` column map for an INSERT from a :class:`Task`."""
    return {
        "id": task.id,
        "owner_id": task.owner_id,
        "persona_id": task.persona_id,
        "contract_json": task.contract.model_dump(mode="json"),
        "state": task.state.value,
        "paused": task.paused,
        "wait_kind": task.wait_kind.value if task.wait_kind is not None else None,
        "ledger_model_micros": task.ledger.model_micros,
        "ledger_sandbox_micros": task.ledger.sandbox_micros,
        "ledger_external_micros": task.ledger.external_micros,
        "head_checkpoint_seq": task.head_checkpoint_seq,
        "conversation_id": task.conversation_id,
        "run_ids": list(task.run_ids),
        "workspace_id": task.workspace_id,
        "schedule_id": task.schedule_id,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "schema_version": task.schema_version,
    }


def row_to_task(row: RowMapping) -> Task:
    """Build a :class:`Task` from a ``tasks`` row."""
    wait_kind_raw = row["wait_kind"]
    return Task(
        id=row["id"],
        owner_id=row["owner_id"],
        persona_id=row["persona_id"],
        contract=Contract.model_validate(row["contract_json"]),
        state=TaskState(row["state"]),
        paused=row["paused"],
        wait_kind=WaitKind(wait_kind_raw) if wait_kind_raw is not None else None,
        ledger=CostLedger(
            model_micros=row["ledger_model_micros"],
            sandbox_micros=row["ledger_sandbox_micros"],
            external_micros=row["ledger_external_micros"],
        ),
        head_checkpoint_seq=row["head_checkpoint_seq"],
        conversation_id=row["conversation_id"],
        run_ids=tuple(row["run_ids"]),
        workspace_id=row["workspace_id"],
        schedule_id=row["schedule_id"],
        created_at=_utc(row["created_at"]),
        updated_at=_utc(row["updated_at"]),
        schema_version=row["schema_version"],
    )


def checkpoint_values(checkpoint: TaskCheckpoint, owner_id: str) -> dict[str, Any]:
    """The ``task_checkpoints`` column map for an INSERT (``id`` is server-defaulted)."""
    return {
        "task_id": checkpoint.task_id,
        "owner_id": owner_id,
        "checkpoint_seq": checkpoint.checkpoint_seq,
        "checkpoint_json": checkpoint.model_dump(mode="json"),
        "content_hash": checkpoint.content_hash,
        "schema_version": checkpoint.schema_version,
        "created_at": checkpoint.updated_at,
    }


def row_to_checkpoint(row: RowMapping) -> TaskCheckpoint:
    """Rebuild a :class:`TaskCheckpoint` from a ``task_checkpoints`` row.

    The whole frozen checkpoint lives in ``checkpoint_json``; ``model_validate``
    round-trips it (and re-verifies the stored ``content_hash``).
    """
    return TaskCheckpoint.model_validate(row["checkpoint_json"])
