"""Postgres-backed store-mutation audit logger, and core's view of its table.

The audit port (:class:`persona.audit.AuditLogger`) has two homes for a row: the
per-persona JSONL file (:class:`persona.audit.JSONLAuditLogger`, the community /
single-node default) and the ``store_audit_events`` table (Spec R5, R5-D-1, the
multi-worker-safe one: Postgres MVCC + WAL make concurrent INSERTs from N
processes atomic and durable, where the JSONL logger's process-local
``threading.Lock`` protects only one worker).

**Why this lives in core.** The Postgres implementation was written inside
persona-api, which the MIT runtimes may not import (the licence boundary the
import-linter contract enforces). The voice service holds its own RLS-scoped
engine and audits every spoken turn, every typed-store write and every session
lifecycle event through this same port, so it needs the same row destination the
API uses (R9-188). The implementation therefore lives here, with core's OWN
minimal :class:`~sqlalchemy.Table` view of the api-owned table, exactly like
``persona.calls`` does for ``calls`` and ``persona.stores.postgres`` does for
``memory_chunks``. The api owns the DDL (migration 030) and re-exports this class
from ``persona_api.db.audit_loggers`` so there is ONE implementation, not two; a
contract test guards the two Table definitions against drift.

``store_audit_events`` is a NON-RLS platform table (like ``audit_log`` /
``rate_limit_buckets``): rows are written on whatever engine the caller holds,
with no owner GUC. The voice session engine may therefore write it directly.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    MetaData,
    Table,
    Text,
    func,
    insert,
    select,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import SQLAlchemyError

from persona.audit import AuditEvent
from persona.errors import AuditWriteError

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import Engine

    from persona.audit import AuditAction, StoreKind
    from persona.schema.chunks import WriteSource

__all__ = ["PostgresAuditLogger", "store_audit_events"]


def _json() -> JSON:
    """A dialect-neutral JSON column type: JSONB on Postgres, JSON elsewhere.

    Mirrors ``persona_api.db.models._json`` so this view emits the same DDL the
    api migration created (community SQLite gets the generic type).
    """
    return JSON().with_variant(JSONB(), "postgresql")


# A private MetaData so importing this module never collides with the api schema
# registry (mirrors ``persona.calls._md`` and ``persona.stores.postgres._md``).
_md = MetaData()

#: Core's minimal write/read view of the api ``store_audit_events`` table.
#: Column names/types mirror ``persona_api.db.models.store_audit_events``; the
#: api owns the DDL (migration 030) and a contract test guards the drift. ``id``
#: and ``created_at`` carry server-side defaults on the api side and are never
#: set here.
store_audit_events = Table(
    "store_audit_events",
    _md,
    Column("id", Text, primary_key=True, server_default=text("gen_random_uuid()::text")),
    Column("timestamp", DateTime(timezone=True), nullable=False),
    Column("persona_id", Text, nullable=False),
    Column("action", Text, nullable=False),
    Column("store", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("written_by", Text),
    Column("reason", Text),
    Column("chunk_ids", _json(), nullable=False, server_default=text("'[]'")),
    Column("logical_ids", _json(), nullable=False, server_default=text("'[]'")),
    Column("metadata", _json(), nullable=False, server_default=text("'{}'")),
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)


class PostgresAuditLogger:
    """Store-mutation audit logger backed by ``store_audit_events`` (R5-D-1).

    Drop-in for :class:`persona.audit.JSONLAuditLogger` behind the
    :class:`persona.audit.AuditLogger` Protocol. ``emit`` is one atomic INSERT;
    ``read`` is a filtered SELECT that reconstructs :class:`AuditEvent` rows.
    Like the JSONL implementation, a failed ``emit`` RAISES
    :class:`persona.errors.AuditWriteError`, because failing to audit a store
    mutation is a correctness issue, not an operational one. Callers for whom the audit is
    a side effect (the voice call path) swallow it at their own boundary.

    Args:
        engine: Any SQLAlchemy Engine that can reach the table. The table is
            non-RLS, so the API's platform engine and the voice service's
            session-scoped RLS engine are both valid writers.
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
