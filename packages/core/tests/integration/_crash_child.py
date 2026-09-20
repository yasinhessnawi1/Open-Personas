"""Child process for the T2 durability test: it writes a versioned update and really dies.

Run as a script, never imported by the suite (the leading underscore keeps pytest off it, the
same way ``_mcp_spawn.py`` stays uncollected). ``test_stores_crash_durability.py`` spawns it,
waits for it to be killed, and then reads the store back in a fresh process.

The kill is a real ``SIGKILL`` that this process sends to itself, so there is no exception to
catch, no unwinding, no flush on the way out, and what the parent reads afterwards is whatever
the transport had actually committed. What the test chooses is only the INSTANT: the killer is
disarmed while the chain is created and armed for the update, so the process dies at the point
a versioned write used to be torn in half.

Nothing in ``persona`` cooperates with this. The killer is an ordinary object satisfying the
``Backend`` protocol, handed to the store through the constructor the store already takes, so
the code under test is byte for byte the production path.
"""

from __future__ import annotations

import os
import signal
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# The child runs outside pytest, so ``packages/core`` is not on the path yet and
# ``tests._embedder`` (the same fake the parent uses, so both ends agree on the vector
# dimension) cannot be imported without this.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from persona.audit import MemoryAuditLogger  # noqa: E402
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource  # noqa: E402
from persona.stores.chroma import ChromaBackend  # noqa: E402
from persona.stores.self_facts import SelfFactsStore  # noqa: E402

from tests._embedder import HashEmbedder  # noqa: E402

#: The chain the parent reads back. Version 1 first, then the update that gets killed.
PERSONA_ID = "crash-persona"
LOGICAL_ID = "crash-persona::self_facts::0000"
TEXT_V1 = "the first version, written before the kill"
TEXT_V2 = "the second version, written across the kill"

_NOW = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)


class KillAfterNextUpsert:
    """A ``Backend`` that forwards everything and, once armed, dies after one upsert.

    The kill lands AFTER the wrapped transport returns, which is precisely the window the
    old two call write left open: the supersede link is committed and the new head is not.
    """

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
    persist_path = Path(sys.argv[1])
    backend = KillAfterNextUpsert(ChromaBackend(persist_path=persist_path, embedder=HashEmbedder()))
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

    # Unreachable: the update always issues at least one upsert, and the first one kills us.
    # If this ever returns, the parent's returncode assertion is the thing that says so.
    return 99


if __name__ == "__main__":
    raise SystemExit(main())
