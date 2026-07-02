"""Durable notification feed — reads, mark-read, and server-authored writes.

Spec P6 (P6-D-11). The bell's cross-device home: run-terminal + persona-ready
notifications are written server-side (D4-c/d) and read here through the
owner-scoped, RLS-protected ``notifications`` table. RLS scopes every query to
the caller's tenant exactly like ``calls`` / ``conversations`` — the ``rls_engine``
connection carries ``app.current_user_id`` (set per-request from the auth
contextvar; set explicitly by the run worker for a server-authored write).

Copy is stored locale-neutral (``message_key`` + ``params``); the web localises.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from persona_api.db.models import notifications as notifications_t

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine


def list_notifications(*, rls_engine: Engine, limit: int, offset: int) -> list[dict[str, object]]:
    """List the caller's notifications (RLS-scoped), newest-first, paginated.

    Read-only (CQS). ``created_at`` descending leads with the most recent; ``id``
    breaks ties deterministically.
    """
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(
                select(notifications_t)
                .order_by(notifications_t.c.created_at.desc(), notifications_t.c.id.desc())
                .limit(limit)
                .offset(offset)
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def mark_read(*, rls_engine: Engine, notification_id: str) -> int:
    """Mark one notification read; returns rows touched (0 if not owned / absent).

    RLS makes a non-owned row invisible → the UPDATE matches nothing → 0, so this
    never leaks whether another tenant's id exists.
    """
    with rls_engine.begin() as conn:
        result = conn.execute(
            update(notifications_t).where(notifications_t.c.id == notification_id).values(read=True)
        )
    return result.rowcount


def mark_all_read(*, rls_engine: Engine) -> int:
    """Mark all of the caller's unread notifications read; returns rows touched."""
    with rls_engine.begin() as conn:
        result = conn.execute(
            update(notifications_t).where(notifications_t.c.read.is_(False)).values(read=True)
        )
    return result.rowcount


def create_notification(
    *,
    conn: Connection,
    owner_id: str,
    kind: str,
    ref_id: str | None,
    level: str,
    message_key: str,
    params: dict[str, str] | None = None,
) -> None:
    """Idempotent server-authored write on the caller's RLS connection (P6-D-11).

    ``ON CONFLICT (owner_id, kind, ref_id) DO NOTHING`` makes a retried write —
    the run persist-final / persist-error / restart-sweep paths, or the two
    avatar paths — a no-op, so no duplicate bell entry. Takes a ``Connection`` (not
    an engine) so the write runs in the caller's owner-scoped transaction — the
    only owner-scoped DB access the job context exposes. Best-effort is the
    caller's concern: it owns the transaction and wraps this so a feed-write
    failure never fails the authoritative run/avatar persist (D-P6-12).
    """
    stmt = (
        pg_insert(notifications_t)
        .values(
            owner_id=owner_id,
            kind=kind,
            ref_id=ref_id,
            level=level,
            message_key=message_key,
            params=params or {},
        )
        .on_conflict_do_nothing(constraint="uq_notifications_owner_kind_ref")
    )
    conn.execute(stmt)
