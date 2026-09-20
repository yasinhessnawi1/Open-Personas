"""Child process for the Postgres half of the T2 durability test (Spec K13).

The sibling of ``packages/core/tests/integration/_crash_child.py``, against the production
transport instead of the community one. Run as a script, never imported by the suite.

It takes a database URL, writes version 1 of a chain, arms a killer that sends this process a
real ``SIGKILL`` after the next transport call returns, and writes version 2. Under the old two
call write that kill lands with the supersede link committed and the new head missing, which is
the corruption; under the single call it lands after the one transaction committed.

Nothing in ``persona`` cooperates. The killer is an ordinary object satisfying the ``Backend``
protocol, injected through the constructor the store already takes.
"""

from __future__ import annotations

import hashlib
import os
import signal
import sys
from datetime import UTC, datetime
from typing import Any

from persona.audit import MemoryAuditLogger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.postgres import EMBEDDING_DIM, PostgresBackend
from persona.stores.self_facts import SelfFactsStore
from sqlalchemy import create_engine

#: Seeded by the parent before this runs (memory_chunks.persona_id is a foreign key).
PERSONA_ID = "p1"
LOGICAL_ID = "p1::self_facts::crash"
TEXT_V1 = "the first version, written before the kill"
TEXT_V2 = "the second version, written across the kill"

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class _Embedder:
    """Deterministic, finite, never the zero vector. Defined here so the child imports no
    test fixtures: pgvector rejects NaN and gives no meaning to a zero vector."""

    model_name: str = "crash-child-embedder"
    dimension: int = EMBEDDING_DIM

    def encode(self, texts: Any) -> list[list[float]]:  # noqa: ANN401 - Sequence[str]
        out: list[list[float]] = []
        for text in texts:
            byts: list[int] = []
            counter = 0
            while len(byts) < self.dimension:
                byts.extend(hashlib.sha256(f"{text}:{counter}".encode()).digest())
                counter += 1
            out.append([(b - 127.5) / 255.0 for b in byts[: self.dimension]])
        return out


class KillAfterNextUpsert:
    """A ``Backend`` that forwards everything and, once armed, dies after one upsert."""

    def __init__(self, inner: Any) -> None:  # noqa: ANN401 - any transport
        self._inner = inner
        self._armed = False

    def arm(self) -> None:
        self._armed = True

    def upsert(self, **kwargs: Any) -> None:  # noqa: ANN401 - forwards the protocol kwargs
        self._inner.upsert(**kwargs)
        if self._armed:
            # Not an exception. The process ends here, mid write, with no unwinding.
            os.kill(os.getpid(), signal.SIGKILL)

    def __getattr__(self, name: str) -> Any:  # noqa: ANN401 - transparent forwarding
        return getattr(self._inner, name)


def _chunk(text: str, *, chunk_id: str, logical_id: str | None) -> PersonaChunk:
    provenance = None
    if logical_id is not None:
        provenance = ChunkProvenance(
            source=WriteSource.USER, logical_id=logical_id, version=1, written_at=_NOW
        )
    return PersonaChunk(id=chunk_id, text=text, created_at=_NOW, provenance=provenance)


def main() -> int:
    engine = create_engine(sys.argv[1], pool_size=1, max_overflow=0)
    backend = KillAfterNextUpsert(PostgresBackend(engine=engine, embedder=_Embedder()))
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())

    store.write(
        PERSONA_ID,
        [_chunk(TEXT_V1, chunk_id=LOGICAL_ID, logical_id=None)],
        source=WriteSource.USER,
    )

    backend.arm()
    store.write(
        PERSONA_ID,
        [_chunk(TEXT_V2, chunk_id=LOGICAL_ID, logical_id=LOGICAL_ID)],
        source=WriteSource.USER,
        reason="the update that gets killed",
    )

    # Unreachable: the update always issues at least one upsert, and the first kills us.
    return 99


if __name__ == "__main__":
    raise SystemExit(main())
