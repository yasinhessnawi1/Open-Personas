"""One run of an erasure at a time (R9-284, review M1).

Two runs of the same erasure can overlap: the route's job and a resume's, a redelivered job and
its retry, an inline run and a sweep. Each step is idempotent, but two runs at once both add
their counts, both complete the erasure and both audit it, and the one that finishes second
fails on a completed row. So a run holds this lock for its whole length, and a run that cannot
take it does nothing: the holder is doing the work.

On Postgres it is a session advisory lock keyed on the erasure id, held on its own connection
of the platform engine, so it spans processes and is released if the holder dies. Elsewhere
(the legacy SQLite edition, one process) it is a lock in this process.
"""

from __future__ import annotations

import hashlib
import threading
from contextlib import contextmanager
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine

__all__ = ["erasure_lock", "lock_key"]

_LOCAL = threading.Lock()
_HELD: set[str] = set()


def lock_key(erasure_id: str) -> int:
    """A signed 64-bit advisory lock key for this erasure, the same in every process."""
    digest = hashlib.sha256(f"erasure:{erasure_id}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


@contextmanager
def _advisory(engine: Engine, erasure_id: str) -> Iterator[bool]:
    key = lock_key(erasure_id)
    with engine.connect() as conn:
        held = bool(conn.execute(text("SELECT pg_try_advisory_lock(:k)"), {"k": key}).scalar())
        conn.commit()
        try:
            yield held
        finally:
            if held:
                conn.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": key})
                conn.commit()


@contextmanager
def _in_process(erasure_id: str) -> Iterator[bool]:
    with _LOCAL:
        held = erasure_id not in _HELD
        if held:
            _HELD.add(erasure_id)
    try:
        yield held
    finally:
        if held:
            with _LOCAL:
                _HELD.discard(erasure_id)


@contextmanager
def erasure_lock(engine: Engine, erasure_id: str) -> Iterator[bool]:
    """Try to take the erasure's lock without waiting; yield whether this run holds it."""
    if engine.dialect.name == "postgresql":
        with _advisory(engine, erasure_id) as held:
            yield held
    else:
        with _in_process(erasure_id) as held:
            yield held
