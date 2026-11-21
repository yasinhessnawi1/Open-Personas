"""API-action audit log (spec 08, T12, §8.2).

A row in ``audit_log`` for every state-changing operation (create/update/delete
persona, create conversation, start/cancel run): ``user_id``, ``action``,
``target``, and a metadata blob. This is API-action forensics — DISTINCT from the
store's per-mutation ``AuditEvent`` (spec 07 handoff: don't conflate them).

``audit_log`` is NOT under RLS (spec-07 rls.py) — it's append-only platform
forensics read by admins, and ``persona_app`` has INSERT-only on it. The write
records ``user_id`` explicitly (not via the RLS GUC).
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from persona.logging import get_logger
from sqlalchemy import insert

from persona_api.db.models import audit_log as audit_log_t

if TYPE_CHECKING:
    from sqlalchemy import Engine
    from sqlalchemy.engine import Connection

_log = get_logger("api.audit")

__all__ = ["record", "record_in"]


def record(
    *,
    engine: Engine,
    user_id: str,
    action: str,
    target: str,
    metadata: dict[str, str] | None = None,
) -> None:
    """Append an audit-log row. Best-effort: a logging failure never breaks the
    operation it audits (logged, swallowed)."""
    try:
        with engine.begin() as conn:
            record_in(conn, user_id=user_id, action=action, target=target, metadata=metadata)
    except Exception as exc:  # noqa: BLE001 — audit must not break the audited op
        _log.warning("audit_log write failed action={action}: {err}", action=action, err=str(exc))


def record_in(
    conn: Connection,
    *,
    user_id: str,
    action: str,
    target: str,
    metadata: dict[str, str] | None = None,
) -> None:
    """Append an audit-log row inside the caller's transaction; a failure fails it too.

    For an operation whose outcome and audit row must commit together (R9-293: a complete
    persona is an audited persona), unlike :func:`record`, which is best-effort.
    """
    conn.execute(
        insert(audit_log_t).values(
            id=f"audit_{uuid.uuid4().hex}",
            user_id=user_id,
            action=action,
            target=target,
            metadata=metadata or {},
        )
    )
