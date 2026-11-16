"""One graph maintenance lock per owner (R9-284, D-R9-284-graph-mutex).

Consolidation spans many transactions, and every review round found another way an erasure's
graph step could land between two of them. So the two never interleave for the same owner:
both take this Postgres advisory lock. Consolidation holds it for its whole run, on a session
connection of its own, and reads its candidates only once it holds it; the erasure's graph step
(and the sweep's, which is the same function) takes it in its own transaction, before any row
lock, so it is released at commit or rollback. A user's forget or delete of a node is the third
taker (R9-287, D-R9-287-forget-canonical): the node delete takes it the same way, first in its
transaction, waiting at most its lock timeout.

The key is the two-``int4`` form, ``(GRAPH_MAINTENANCE_CLASS, hashtext('graph-maint:' || owner))``.
Postgres keeps that key space apart from the single ``bigint`` keys the other advisory locks in
this codebase use, so no other lock can collide with it. Two owners whose hashes collide only
wait for each other; nothing is shared but the wait.

Why this adds no deadlock cycle. A waiter for this lock holds nothing when it starts waiting:
the graph step and the node delete take it as the first statement of their transactions (the
delete's ``SET LOCAL lock_timeout`` before it takes no lock), and a consolidation run takes it
before its first read or write. The holder (a consolidation run) may then wait on row
locks: a persona row ``FOR KEY SHARE`` for the write guard, node and edge rows for its writes.
The graph step and the node delete, once they hold it, wait on row locks the same way.
Those are held by other writers (synthesis, a direct write, the erasure's persona row delete),
and none of them ever waits for this lock: the persona row delete runs in its own transaction,
before the graph step and without this lock. Consolidation never deletes a node, so it never
calls the node delete while it holds the session lock. So no cycle passes through it; any
cycle among row locks alone existed before it and is retried (the job's backoff, the sweep's
next pass, the forget's 409).
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import text

from persona.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Connection, Engine

__all__ = [
    "GRAPH_MAINTENANCE_CLASS",
    "graph_maintenance_held",
    "take_graph_maintenance_lock",
    "try_graph_maintenance_lock",
]

_log = get_logger("graph.maintenance_lock")

#: The first key of the two-int4 form: this lock's class, fixed for every process.
GRAPH_MAINTENANCE_CLASS = 2_840_284
_KEY = f"{GRAPH_MAINTENANCE_CLASS}, hashtext('graph-maint:' || :owner)"
_SESSION_LOCK = text(f"SELECT pg_advisory_lock({_KEY})")
_SESSION_UNLOCK = text(f"SELECT pg_advisory_unlock({_KEY})")
_XACT_LOCK = text(f"SELECT pg_advisory_xact_lock({_KEY})")
_XACT_TRY = text(f"SELECT pg_try_advisory_xact_lock({_KEY})")


def take_graph_maintenance_lock(conn: Connection, owner_id: str) -> None:
    """Take the owner's lock for the rest of ``conn``'s transaction (the erasure's graph step).

    Call it first in the transaction, before any row lock; it waits for a running consolidation.
    A no-op on an engine that is not Postgres (no graph there).
    """
    if conn.dialect.name == "postgresql":
        conn.execute(_XACT_LOCK, {"owner": owner_id})


def try_graph_maintenance_lock(conn: Connection, owner_id: str) -> bool:
    """Take the owner's lock for the rest of the transaction if it is free; never wait.

    For a caller on an event loop (the sweep runs as a worker maintenance hook): waiting for a
    whole consolidation run there would stall every other task (round 6 M-3). ``True`` off
    Postgres, where there is no graph and nothing to wait for.
    """
    if conn.dialect.name != "postgresql":
        return True
    return bool(conn.execute(_XACT_TRY, {"owner": owner_id}).scalar())


@contextmanager
def graph_maintenance_held(engine: Engine, owner_id: str) -> Iterator[None]:
    """Hold the owner's lock on a session connection of its own for the ``with`` body.

    Consolidation's run. The connection is committed after the lock is taken, so it does not sit
    idle in a transaction while it holds the lock. The lock is released in a ``finally``; if the
    release itself fails, the connection is invalidated instead of going back to the pool, and
    closing its session releases the lock.
    """
    if engine.dialect.name != "postgresql":
        yield
        return
    conn = engine.connect()
    try:
        conn.execute(_SESSION_LOCK, {"owner": owner_id})
    except BaseException:
        conn.close()  # not taken: nothing to release
        raise
    try:
        conn.commit()
    except BaseException:
        # Taken, then the commit failed: the session may hold the lock, so it is closed, never
        # pooled (round 6 L-3).
        conn.invalidate()
        conn.close()
        raise
    try:
        yield
    finally:
        try:
            conn.execute(_SESSION_UNLOCK, {"owner": owner_id})
            conn.commit()
        except Exception as exc:  # noqa: BLE001 - the session's close releases the lock instead
            _log.error("graph maintenance unlock failed error={err}", err=type(exc).__name__)
            conn.invalidate()
        finally:
            conn.close()
