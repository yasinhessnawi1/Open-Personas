"""Postgres-backed audit loggers — multi-worker-safe (Spec R5, R5-D-1).

The default ``persona.audit.JSONLAuditLogger`` and
``persona.tools.audit.JSONLToolAuditLogger`` serialize writes behind a
*process-local* ``threading.Lock`` — safe within one uvicorn worker (S08-4), but
the moment the API runs on N workers / N Fly Machines each process appends to its
own file (or, worse, interleaves partial lines into a shared one) and audit
entries are lost or corrupted.

These two loggers write to Postgres instead. PostgreSQL MVCC + WAL make
concurrent INSERTs from N processes/connections non-blocking, per-statement
atomic, and durable — no lost / duplicated / interleaved-corrupted rows and no
explicit locking for an append-only pattern. This is the D-19-5 "in-process
state → shared store" pattern (mirrors ``PostgresRateLimitStore``), reused, not
reinvented. They satisfy the core ``AuditLogger`` / ``ToolAuditLogger`` Protocols
structurally, and are selected by ``PERSONA_API_AUDIT_BACKEND=postgres`` at
``app.py::_build_audit_logger`` (default stays JSONL → community / single-node
byte-unchanged).

``PostgresToolAuditLogger`` lives in the API package because it depends on the
``persona_api.db.models`` table definition, exactly where ``PostgresRateLimitStore``
lives. ``PostgresAuditLogger`` used to live here too, and moved to
``persona.audit_postgres`` (R9-188) so the MIT voice runtime can select the same
row destination without importing persona-api, which the licence boundary
forbids; it is re-exported here so every existing import keeps working and there
is ONE implementation. Both target NON-RLS platform tables written via a plain
``engine.begin()`` (no owner GUC), like ``audit_log`` / ``rate_limit_buckets``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.audit_postgres import PostgresAuditLogger
from persona.errors import AuditWriteError
from sqlalchemy import insert
from sqlalchemy.exc import SQLAlchemyError

from persona_api.db.models import tool_audit_events

if TYPE_CHECKING:
    from persona.tools.audit import ToolAuditEvent
    from sqlalchemy import Engine

__all__ = [
    "PostgresAuditLogger",
    "PostgresToolAuditLogger",
]


class PostgresToolAuditLogger:
    """Tool audit logger backed by ``tool_audit_events`` (R5-D-1).

    Drop-in for ``persona.tools.audit.JSONLToolAuditLogger`` behind the
    ``ToolAuditLogger`` Protocol (``emit`` only — the Protocol has no ``read``).
    One atomic INSERT per event; ``persona_id`` is nullable (CLI sessions carry
    no persona). Like the JSONL impl, a failed ``emit`` raises
    :class:`AuditWriteError`.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def emit(self, event: ToolAuditEvent) -> None:
        """Append ``event`` as one atomic row. Raises on write failure."""
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    insert(tool_audit_events).values(
                        timestamp=event.timestamp,
                        persona_id=event.persona_id,
                        tool_name=event.tool_name,
                        action=event.action,
                        resource=event.resource,
                        is_error=event.is_error,
                        metadata=dict(event.metadata),
                    )
                )
        except SQLAlchemyError as exc:
            raise AuditWriteError(
                "failed to insert tool audit event",
                context={"persona_id": event.persona_id or "-", "reason": str(exc)[:120]},
            ) from exc
