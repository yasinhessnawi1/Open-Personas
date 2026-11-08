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

from typing import TYPE_CHECKING, Any, ClassVar

from persona.audit import AuditAction
from persona.errors import ChainConflictError, ForgottenEvidenceError
from persona.schema.chunks import PersonaChunk, WriteSource
from persona.stores.base import TypedStore
from persona.stores.policy import PolicyDecision, PolicyRule, PolicyTable
from persona.stores.versioning import plan_excision

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

__all__ = ["CORE_MEMORY_KIND", "CORE_SOURCE_IDS_KEY", "CoreMemoryStore", "core_logical_id"]

#: The chunk kind the core block lives under (migration ``0NN_core_memory_kind``).
CORE_MEMORY_KIND = "core_memory"

#: Metadata key under which the block cites its source chunk ids (provenance, §0).
CORE_SOURCE_IDS_KEY = "source_ids"

#: Where a cited source can be. The refresh selects raw episodic chunks today, and the first
#: place looked. The others are here because a block predating that choice could cite them, and
#: taking a row that exists for a forgotten one would delete a summary nobody asked to forget.
_EVIDENCE_KINDS: tuple[str, ...] = ("episodic", "identity", "self_facts", "worldview")

#: How many times a forget plans its rewrite before a busy chain reaches the caller. The same
#: bound as a versioned write, for the same reason: a refresh can commit between the read the
#: plan came from and the rewrite, and the head index then refuses the stale plan whole.
_EXCISE_ATTEMPTS = 3


def cited_sources(chunk: PersonaChunk) -> tuple[str, ...]:
    """The source ids a stored block cites."""
    return tuple(s for s in chunk.metadata.get(CORE_SOURCE_IDS_KEY, "").split(",") if s)


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
        # Through the store's own ``get_all`` rather than the transport's, so the K13 read
        # tolerance applies here too (D-K13-8). This is the one kind that really carries
        # version chains in production, and a chain broken by an interrupted refresh used to
        # make the block vanish: no unsuperseded row, so ``current`` was empty and the persona
        # silently lost its always-in-context memory until the next refresh.
        current = self.get_all(persona_id)
        if not current:
            return None
        # Deterministic: newest version wins if a backend ever returns siblings.
        return max(current, key=lambda c: (c.provenance.version if c.provenance else 1, c.id))

    # ----- derived text must not outlive its evidence (K8-D-14, R9-209) ----

    def _chain(self, persona_id: str) -> list[PersonaChunk]:
        return self._backend.get_by_logical_ids(
            persona_id=persona_id,
            store_kind=self.STORE_KIND,
            logical_ids=[core_logical_id(persona_id)],
        )

    def missing_sources(self, persona_id: str, source_ids: Iterable[str]) -> list[str]:
        """The cited ids no longer on disk anywhere, in the order given. Reads only."""
        wanted = list(dict.fromkeys(source_ids))
        unfound = set(wanted)
        for kind in _EVIDENCE_KINDS:
            if not unfound:
                break
            found = self._backend.get_by_ids(
                persona_id=persona_id, store_kind=kind, ids=sorted(unfound)
            )
            unfound -= {c.id for c in found}
        return [s for s in wanted if s in unfound]

    def forget_cited(
        self,
        persona_id: str,
        forgotten_ids: Sequence[str],
        *,
        written_by: str = "stores.forget",
    ) -> int:
        """Delete every version citing a forgotten id, and return how many went.

        The chain is closed up around them in the same operation, so it comes out valid and
        with one head. If the head went, the newest survivor is current until the next refresh,
        and if nothing survives there is no block. Moves pointers and deletes rows, so it reads
        no text beyond the citations and embeds nothing. Audited with ids and counts only.

        The plan comes from a read, and a refresh can commit between that read and the rewrite.
        The transport compares the chain with that read before it rewrites anything, and refuses
        a plan the chain has moved past, so this reads again and plans afresh, a bounded number
        of times. On Postgres the comparison is inside the transaction, so it is exact. On
        Chroma it is just before, so a writer landing inside the rewrite can still leave a hole
        (documented in ``backend.py``).
        """
        forgotten = set(forgotten_ids)
        return self._excise_bounded(
            persona_id,
            lambda chain: [c.id for c in chain if forgotten.intersection(cited_sources(c))],
            written_by=written_by,
            cited=len(forgotten),
        )

    def retire(self, persona_id: str, *, written_by: str = "stores.forget") -> int:
        """Delete the whole chain, because the evidence behind it is gone, and return the count.

        Bounded the same way as :meth:`forget_cited`: a refresh that commits between the read
        and the rewrite makes the plan stale, and a re-read takes its version too.
        """
        return self._excise_bounded(
            persona_id, lambda chain: [c.id for c in chain], written_by=written_by, cited=0
        )

    def _excise_bounded(
        self,
        persona_id: str,
        pick_doomed: Callable[[list[PersonaChunk]], list[str]],
        *,
        written_by: str,
        cited: int,
    ) -> int:
        """Plan from a fresh read and rewrite, re-reading if another writer got there first."""
        for attempt in range(1, _EXCISE_ATTEMPTS + 1):
            chain = self._chain(persona_id)
            doomed = pick_doomed(chain)
            if not doomed:
                return 0
            try:
                self._excise(persona_id, chain, doomed, written_by=written_by, cited=cited)
            except ChainConflictError:
                if attempt == _EXCISE_ATTEMPTS:
                    raise
                self._log.info(
                    "chain conflict during a forget, re-reading persona={persona} attempt={n}",
                    persona=persona_id,
                    n=attempt,
                )
                continue
            return len(doomed)
        return 0  # pragma: no cover - the loop returns or raises on its last attempt

    def _excise(
        self,
        persona_id: str,
        chain: list[PersonaChunk],
        doomed: list[str],
        *,
        written_by: str,
        cited: int,
    ) -> None:
        rewrite = plan_excision(chain, doomed)
        self._backend.excise_versions(
            persona_id=persona_id,
            store_kind=self.STORE_KIND,
            logical_id=core_logical_id(persona_id),
            expected={
                c.id: (c.provenance.version, c.provenance.superseded_by)
                for c in chain
                if c.provenance is not None
            },
            doomed_ids=doomed,
            links=rewrite.links,
            versions=rewrite.versions,
        )
        self._emit_audit(
            persona_id=persona_id,
            action=AuditAction.REMOVE_DOCUMENTS,
            source=WriteSource.USER,
            written_by=written_by,
            chunk_ids=doomed,
            logical_ids=[core_logical_id(persona_id)],
            metadata={
                "forgotten_cited": str(cited),
                "versions_left": str(len(chain) - len(doomed)),
            },
        )

    def orphaned_citations(self, persona_id: str) -> list[str]:
        """Every id some version cites that is no longer on disk, sorted. Reads only.

        What a sweep of restored rows looks for: blocks written before a forget reached them.
        """
        cited = sorted({s for c in self._chain(persona_id) for s in cited_sources(c)})
        return self.missing_sources(persona_id, cited)

    def _check_rollback_target(self, persona_id: str, target: PersonaChunk) -> None:
        """Refuse to bring back a summary of evidence that has been forgotten."""
        missing = self.missing_sources(persona_id, cited_sources(target))
        if missing:
            msg = (
                "This version of the memory summary was written from something that has since "
                "been forgotten, so it cannot be restored."
            )
            raise ForgottenEvidenceError(
                msg, context={"persona_id": persona_id, "missing": str(len(missing))}
            )
