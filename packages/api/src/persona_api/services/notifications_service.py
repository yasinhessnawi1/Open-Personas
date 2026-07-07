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

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from persona_api.db.models import notifications as notifications_t
from persona_api.realtime.events import NotificationCreatedEvent, TaskUpdatedEvent

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine

    from persona_api.realtime.channel import UserEventChannel


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


def upsert_coalesced_notification(
    *,
    conn: Connection,
    owner_id: str,
    kind: str,
    ref_id: str | None,
    level: str,
    message_key: str,
    params: dict[str, str] | None = None,
) -> None:
    """Coalescing server-authored write — ONE re-alerting row per ``(owner, kind, ref_id)``.

    The DO-UPDATE sibling of :func:`create_notification` (which DO-NOTHINGs). Used for the
    ``schedule_fired`` bell: a schedule that fires every hour must show ONE moving entry, not
    24 rows. On a re-fire the conflicting row is UPDATED in place — ``created_at`` bumped to
    now (it floats back to the top of the newest-first feed / relative-time re-reads "just
    now"), ``read`` reset to ``False`` so the bell RE-ALERTS, and the copy (``level`` /
    ``message_key`` / ``params``) refreshed. The uniqueness/coalescing key is the existing
    ``uq_notifications_owner_kind_ref`` constraint. Takes a ``Connection`` so the write runs
    in the caller's owner-scoped transaction (RLS); the caller wraps it best-effort so a feed
    write never fails the authoritative fire.
    """
    values = {
        "owner_id": owner_id,
        "kind": kind,
        "ref_id": ref_id,
        "level": level,
        "message_key": message_key,
        "params": params or {},
    }
    stmt = (
        pg_insert(notifications_t)
        .values(**values)
        .on_conflict_do_update(
            constraint="uq_notifications_owner_kind_ref",
            set_={
                "created_at": func.now(),  # bump: re-sort to the top + relative-time "just now"
                "read": False,  # re-alert: the bell shows the fresh fire as unread
                "level": level,
                "message_key": message_key,
                "params": params or {},
            },
        )
    )
    conn.execute(stmt)


def publish_notification_created(
    channel: UserEventChannel | None,
    *,
    owner_id: str,
    kind: str,
    ref_id: str | None,
) -> None:
    """Ping the owner's open tabs that their bell changed (Spec A11, notification.created).

    Call this **after** the durable :func:`create_notification` write has COMMITTED — the
    event is a data-only signal (``kind`` + ``ref_id``) and the client refetches
    ``/v1/me/notifications`` (unioned with P6, read-state honoured) to reconcile by id
    (A11-D-2). Emitting pre-commit would race the refetch to an empty read (the
    surface-lags-truth rule). Best-effort: no channel or no open tab → a durable-only
    no-op (present on next connect); a publish never fails the authoritative write (the
    caller wraps it, and the in-process bus does not raise for a normal publish).
    """
    if channel is None:
        return
    channel.publish(owner_id, NotificationCreatedEvent(kind=kind, ref_id=ref_id))


def publish_task_updated(
    channel: UserEventChannel | None,
    *,
    owner_id: str,
    task_id: str,
    state: str,
) -> None:
    """Ping the owner's open tabs that a task transitioned (Spec A11/A6, task.updated).

    Data-only (``task_id`` + ``state``): A6's Review/Tasks/Approvals surfaces refetch their
    durable data via the W8 ``useTaskSignal`` seam — never trusting the pushed state. Call it
    AFTER the durable state write has committed (surface-lags-truth: a pre-commit ping would race
    the refetch to a stale read). Best-effort: no channel / no open tab → a no-op (the surface
    catches up on its next navigation/poll); a publish never fails the authoritative transition.
    """
    if channel is None:
        return
    channel.publish(owner_id, TaskUpdatedEvent(task_id=task_id, state=state))
