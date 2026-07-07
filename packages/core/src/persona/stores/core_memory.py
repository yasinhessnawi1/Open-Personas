"""Core-memory store — the always-in-context block's persistence (Spec K9, T7; K9-D-10).

The K9 core-memory block is a compact user+persona summary held as an ordinary
``memory_chunks`` row with ``kind='core_memory'`` (the approved DDL-preview). One
**append-only versioned** logical chain per persona: a background refresh writes a new
version that supersedes the prior head (never destroys it — §0 provenance-preserving),
and the turn reads the current head. It reuses the whole chunk transport (persona-scoped
RLS, the D-07-4 versioning chain, the injected embedder) and is **excluded from recall by
construction** — K9's fusion legs query only ``'episodic'`` / ``'episodic_gist'``, never
this kind.
"""

from __future__ import annotations

from typing import Any, ClassVar

from persona.schema.chunks import PersonaChunk, WriteSource
from persona.stores.base import TypedStore
from persona.stores.policy import PolicyDecision, PolicyRule, PolicyTable

__all__ = ["CORE_MEMORY_KIND", "CoreMemoryStore", "core_logical_id"]

#: The chunk kind the core block lives under (migration ``0NN_core_memory_kind``).
CORE_MEMORY_KIND = "core_memory"


def core_logical_id(persona_id: str) -> str:
    """The stable logical-chain id for a persona's core block (one chain per persona).

    Every refresh writes a new version under this id, so :meth:`TypedStore.write`
    supersedes the prior head — append-only, current-head-readable.
    """
    return f"{persona_id}::core"


class CoreMemoryStore(TypedStore):
    """The persona's core-memory block — background-written, always read (K9-D-10).

    A thin :class:`TypedStore` over ``kind='core_memory'``: writes are **SYSTEM-only**
    (the background engine authors the block; no user/persona-self write path), versioning
    is on (the append-only refresh), and :meth:`current` reads the single head.
    """

    STORE_KIND: ClassVar[str] = CORE_MEMORY_KIND
    _POLICY: ClassVar[PolicyTable] = {
        WriteSource.SYSTEM: PolicyRule(decision=PolicyDecision.ACCEPT),
    }

    def __init__(self, *, backend: Any, audit_logger: Any) -> None:  # noqa: ANN401 — composes via base
        super().__init__(backend=backend, audit_logger=audit_logger)

    def current(self, persona_id: str) -> PersonaChunk | None:
        """The persona's current core block, or ``None`` before the first refresh.

        There is one logical chain per persona, so the current heads
        (``get_all`` returns ``superseded_by IS NULL`` rows) are at most one row.
        """
        heads = self._backend.get_all(persona_id=persona_id, store_kind=self.STORE_KIND)
        current = [c for c in heads if c.provenance is None or c.provenance.superseded_by is None]
        if not current:
            return None
        # Deterministic: newest version wins if a backend ever returns siblings.
        return max(current, key=lambda c: (c.provenance.version if c.provenance else 1, c.id))
