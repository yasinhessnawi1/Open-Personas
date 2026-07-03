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

They live in the API package (not core) because they depend on the SQLAlchemy
``Engine`` + the ``persona_api.db.models`` table definitions — exactly where
``PostgresRateLimitStore`` lives. Both target NON-RLS platform tables written via
a plain ``engine.begin()`` (no owner GUC), like ``audit_log`` /
``rate_limit_buckets``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.audit import AuditEvent
from persona.errors import AuditWriteError
from sqlalchemy import insert, select
from sqlalchemy.exc import SQLAlchemyError

from persona_api.db.models import store_audit_events, tool_audit_events

if TYPE_CHECKING:
    from datetime import datetime

    from persona.audit import AuditAction, StoreKind
    from persona.schema.chunks import WriteSource
    from persona.tools.audit import ToolAuditEvent
    from sqlalchemy import Engine

__all__ = [
    "PostgresAuditLogger",
    "PostgresToolAuditLogger",
]


class PostgresAuditLogger:
    """Store-mutation audit logger backed by ``store_audit_events`` (R5-D-1).

    Drop-in for ``persona.audit.JSONLAuditLogger`` behind the ``AuditLogger``
    Protocol. ``emit`` is one atomic INSERT; ``read`` is a filtered SELECT that
    reconstructs :class:`AuditEvent` rows. Like the JSONL impl (and UNLIKE the
    best-effort ``audit_service.record``), a failed ``emit`` RAISES
    :class:`AuditWriteError` — failing to audit a store mutation is a correctness
    issue, not an operational one.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def emit(self, event: AuditEvent) -> None:
        """Append ``event`` as one atomic row. Raises on write failure."""
        try:
            with self._engine.begin() as conn:
                conn.execute(
                    insert(store_audit_events).values(
                        timestamp=event.timestamp,
                        persona_id=event.persona_id,
                        action=event.action.value,
                        store=event.store,
                        source=event.source.value,
                        written_by=event.written_by,
                        reason=event.reason,
                        chunk_ids=list(event.chunk_ids),
                        logical_ids=list(event.logical_ids),
                        metadata=dict(event.metadata),
                    )
                )
        except SQLAlchemyError as exc:
            raise AuditWriteError(
                "failed to insert store audit event",
                context={"persona_id": event.persona_id, "reason": str(exc)[:120]},
            ) from exc

    def read(
        self,
        persona_id: str,
        *,
        since: datetime | None = None,
        action: AuditAction | None = None,
        source: WriteSource | None = None,
        store: StoreKind | None = None,
    ) -> list[AuditEvent]:
        """Return this persona's events, filters ANDed, oldest-first.

        Mirrors ``JSONLAuditLogger.read`` semantics: a missing persona yields an
        empty list; ``since`` must be tz-aware (naive raises ``ValueError``, as in
        core's ``_filter_events``). Ordered by ``timestamp`` so per-persona
        interleaving is preserved.
        """
        if since is not None and since.tzinfo is None:
            msg = "since must be tz-aware"
            raise ValueError(msg)
        stmt = select(store_audit_events).where(store_audit_events.c.persona_id == persona_id)
        if since is not None:
            stmt = stmt.where(store_audit_events.c.timestamp >= since)
        if action is not None:
            stmt = stmt.where(store_audit_events.c.action == action.value)
        if source is not None:
            stmt = stmt.where(store_audit_events.c.source == source.value)
        if store is not None:
            stmt = stmt.where(store_audit_events.c.store == store)
        stmt = stmt.order_by(store_audit_events.c.timestamp, store_audit_events.c.created_at)
        with self._engine.begin() as conn:
            rows = conn.execute(stmt).mappings().all()
        return [
            AuditEvent(
                timestamp=row["timestamp"],
                persona_id=row["persona_id"],
                action=row["action"],
                store=row["store"],
                source=row["source"],
                written_by=row["written_by"],
                reason=row["reason"],
                chunk_ids=list(row["chunk_ids"] or []),
                logical_ids=list(row["logical_ids"] or []),
                metadata=dict(row["metadata"] or {}),
            )
            for row in rows
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
