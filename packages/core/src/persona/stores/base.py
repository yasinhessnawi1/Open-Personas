"""Abstract :class:`TypedStore` — policy + versioning + audit, plus a transport.

Each concrete store subclass (identity / self_facts / worldview / episodic)
sets ``STORE_KIND`` and ``_POLICY``. All the orchestration lives here:

- Per-source policy enforcement at the boundary (delegates to
  :mod:`persona.stores.policy`).
- Versioning: a write to an existing ``logical_id`` becomes version N+1
  and supersedes the previous head (delegates to
  :mod:`persona.stores.versioning`).
- Audit-event emission on every successful mutation.
- A backend handle (the Chroma transport, or a mock in unit tests) — the
  base never touches Chroma directly.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, ClassVar

from persona.audit import AuditAction, AuditEvent
from persona.logging import get_logger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.policy import PolicyTable, evaluate_write_policy
from persona.stores.versioning import (
    compute_next_version,
    current_version,
    link_supersedes,
    validate_chain,
)

if TYPE_CHECKING:
    from persona.audit import AuditLogger, StoreKind
    from persona.stores.backend import Backend

__all__ = ["TypedStore"]


class TypedStore:
    """Shared implementation for the four typed stores.

    Subclasses set :attr:`STORE_KIND` and :attr:`_POLICY`. The base composes
    the Chroma backend, the audit logger, and the version-chain helpers.

    Identity-store subclasses set :attr:`SUPPORTS_VERSIONING = False`,
    which makes ``history`` and ``rollback`` raise — identity is immutable
    at runtime.
    """

    STORE_KIND: ClassVar[str] = ""  # overridden by subclasses
    SUPPORTS_VERSIONING: ClassVar[bool] = True
    _POLICY: ClassVar[PolicyTable] = {}

    def __init__(
        self,
        *,
        backend: Backend,
        audit_logger: AuditLogger,
    ) -> None:
        self._backend = backend
        self._audit = audit_logger
        self._log = get_logger(f"stores.{self.STORE_KIND}")

    # ----- write -----------------------------------------------------------

    def write(
        self,
        persona_id: str,
        chunks: list[PersonaChunk],
        *,
        source: WriteSource = WriteSource.SYSTEM,
        written_by: str | None = None,
        reason: str | None = None,
        force: bool = False,
    ) -> None:
        if not chunks:
            return

        evaluate_write_policy(
            policy=self._POLICY,
            source=source,
            force=force,
            chunks=chunks,
            reason=reason,
            store_kind=self.STORE_KIND,
            persona_id=persona_id,
        )

        # Scoped versioning fetch (Spec K8, K8-D-12): version computation only
        # reads a chunk's own logical chain, so fetch exactly the batch's
        # chains — never the whole store (the former ``get_all`` here was an
        # O(N) read on EVERY write). Non-versioning stores fetch nothing.
        existing: list[PersonaChunk] = []
        if self.SUPPORTS_VERSIONING:
            batch_logical_ids = [
                c.provenance.logical_id if c.provenance is not None else c.id for c in chunks
            ]
            existing = self._backend.get_by_logical_ids(
                persona_id=persona_id,
                store_kind=self.STORE_KIND,
                logical_ids=batch_logical_ids,
            )

        prepared: list[PersonaChunk] = []
        supersede_updates: list[PersonaChunk] = []
        for chunk in chunks:
            if self.SUPPORTS_VERSIONING:
                prepared_chunk, supersedes = self._prepare_versioned(
                    chunk,
                    existing=existing,
                    source=source,
                    written_by=written_by,
                    reason=reason,
                )
                prepared.append(prepared_chunk)
                if supersedes is not None:
                    supersede_updates.append(supersedes)
                    # Splice the prior head's update into the in-memory
                    # ``existing`` view so the next chunk in this batch sees
                    # the latest state.
                    existing = [supersedes if c.id == supersedes.id else c for c in existing]
                existing = [*existing, prepared_chunk]
            else:
                prepared.append(chunk)

        # Persist supersedes-link updates first so the prior head's metadata
        # reflects the new chain before the new head lands.
        if supersede_updates:
            self._backend.upsert(
                persona_id=persona_id,
                store_kind=self.STORE_KIND,
                chunks=supersede_updates,
            )
        self._backend.upsert(
            persona_id=persona_id,
            store_kind=self.STORE_KIND,
            chunks=prepared,
        )

        self._emit_audit(
            persona_id=persona_id,
            action=AuditAction.WRITE,
            source=source,
            written_by=written_by,
            reason=reason,
            chunk_ids=[c.id for c in prepared],
            logical_ids=[c.provenance.logical_id for c in prepared if c.provenance is not None],
        )

    def _prepare_versioned(
        self,
        chunk: PersonaChunk,
        *,
        existing: list[PersonaChunk],
        source: WriteSource,
        written_by: str | None,
        reason: str | None,
    ) -> tuple[PersonaChunk, PersonaChunk | None]:
        """Return ``(prepared_chunk, supersedes)``.

        ``prepared_chunk`` always has provenance populated; ``supersedes``
        is the prior head's chunk with its ``superseded_by`` link updated,
        or ``None`` if this is a first write for the logical chain.
        """
        # Use the chunk's existing provenance if the caller supplied one
        # (registry path); otherwise build one. logical_id defaults to the
        # chunk's id on first write (D-01-8).
        logical_id = chunk.provenance.logical_id if chunk.provenance is not None else chunk.id
        next_version = compute_next_version(existing, logical_id)

        provenance = ChunkProvenance(
            source=source,
            logical_id=logical_id,
            version=next_version,
            superseded_by=None,
            written_at=datetime.now(UTC),
            written_by=written_by,
            reason=reason,
        )
        prepared = chunk.model_copy(update={"provenance": provenance})

        supersedes: PersonaChunk | None = None
        if next_version > 1:
            head = current_version(existing, logical_id)
            if head is not None:
                supersedes = link_supersedes(head, prepared.id)
        return prepared, supersedes

    # ----- read ------------------------------------------------------------

    def query(
        self,
        persona_id: str,
        query: str,
        top_k: int,
        **filters: Any,  # noqa: ANN401 — backend-specific
    ) -> list[PersonaChunk]:
        results = self._backend.query(
            persona_id=persona_id,
            store_kind=self.STORE_KIND,
            text=query,
            top_k=top_k,
            where=filters or None,
        )
        # Filter out superseded versions (queries return the current view).
        return [c for c in results if c.provenance is None or c.provenance.superseded_by is None]

    def get_all(
        self,
        persona_id: str,
        *,
        include_superseded: bool = False,
    ) -> list[PersonaChunk]:
        all_chunks = self._backend.get_all(persona_id=persona_id, store_kind=self.STORE_KIND)
        if include_superseded:
            return all_chunks
        return [c for c in all_chunks if c.provenance is None or c.provenance.superseded_by is None]

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        """Return up to ``limit`` most-recently-created current chunks, newest first.

        Recency counterpart to :meth:`query`. Delegates to the backend's
        ``recent`` push-down (Spec K8, acceptance 1): Postgres serves it as
        ``ORDER BY (created_at, id) DESC LIMIT``, Chroma sorts in-process.
        Never materialises the whole store.
        """
        if limit <= 0:
            return []
        return self._backend.recent(persona_id=persona_id, store_kind=self.STORE_KIND, limit=limit)

    # ----- delete ----------------------------------------------------------

    def delete(self, persona_id: str) -> None:
        existing = self._backend.get_all(persona_id=persona_id, store_kind=self.STORE_KIND)
        self._backend.delete_persona(persona_id, self.STORE_KIND)
        if existing:
            self._emit_audit(
                persona_id=persona_id,
                action=AuditAction.DELETE,
                source=WriteSource.USER,
                chunk_ids=[c.id for c in existing],
                logical_ids=[c.provenance.logical_id for c in existing if c.provenance is not None],
            )

    def remove_documents(self, persona_id: str, doc_ids: list[str]) -> None:
        if not doc_ids:
            return
        self._backend.delete_documents(
            persona_id=persona_id, store_kind=self.STORE_KIND, ids=doc_ids
        )
        self._emit_audit(
            persona_id=persona_id,
            action=AuditAction.REMOVE_DOCUMENTS,
            source=WriteSource.USER,
            chunk_ids=list(doc_ids),
        )

    # ----- history / rollback ---------------------------------------------

    def history(self, persona_id: str, logical_id: str) -> list[PersonaChunk]:
        if not self.SUPPORTS_VERSIONING:
            from persona.errors import RuntimeWriteForbiddenError

            msg = "history is not supported on this store"
            raise RuntimeWriteForbiddenError(
                msg, context={"store": self.STORE_KIND, "persona_id": persona_id}
            )
        chain = [
            c
            for c in self._backend.get_all(persona_id=persona_id, store_kind=self.STORE_KIND)
            if c.provenance is not None and c.provenance.logical_id == logical_id
        ]
        chain.sort(key=lambda c: c.provenance.version if c.provenance else 0)
        validate_chain(chain)
        return chain

    def rollback(
        self,
        persona_id: str,
        logical_id: str,
        to_version: int,
        *,
        source: WriteSource,
        written_by: str | None = None,
        reason: str | None = None,
    ) -> None:
        if not self.SUPPORTS_VERSIONING:
            from persona.errors import RuntimeWriteForbiddenError

            msg = "rollback is not supported on this store"
            raise RuntimeWriteForbiddenError(
                msg, context={"store": self.STORE_KIND, "persona_id": persona_id}
            )

        from persona.errors import BrokenVersionChainError

        chain = self.history(persona_id, logical_id)
        if not chain:
            raise BrokenVersionChainError(
                "no chain for logical_id",
                context={
                    "store": self.STORE_KIND,
                    "persona_id": persona_id,
                    "logical_id": logical_id,
                },
            )
        target = next(
            (c for c in chain if c.provenance is not None and c.provenance.version == to_version),
            None,
        )
        if target is None:
            raise BrokenVersionChainError(
                "rollback target version does not exist",
                context={
                    "store": self.STORE_KIND,
                    "persona_id": persona_id,
                    "logical_id": logical_id,
                    "to_version": str(to_version),
                    "available": ",".join(
                        str(c.provenance.version) for c in chain if c.provenance is not None
                    ),
                },
            )

        # Build a new head whose text+metadata mirror the target.
        new_version_no = chain[-1].provenance.version + 1 if chain[-1].provenance else 1
        new_id = f"{logical_id}::v{new_version_no:04d}"
        new_provenance = ChunkProvenance(
            source=source,
            logical_id=logical_id,
            version=new_version_no,
            written_at=datetime.now(UTC),
            written_by=written_by,
            reason=reason or f"rollback to version {to_version}",
        )
        new_head = PersonaChunk(
            id=new_id,
            text=target.text,
            metadata=dict(target.metadata),
            created_at=datetime.now(UTC),
            provenance=new_provenance,
        )

        # Link the previous head to the new head, then upsert both.
        prev_head = chain[-1]
        supersedes = link_supersedes(prev_head, new_head.id)
        self._backend.upsert(
            persona_id=persona_id,
            store_kind=self.STORE_KIND,
            chunks=[supersedes, new_head],
        )

        self._emit_audit(
            persona_id=persona_id,
            action=AuditAction.ROLLBACK,
            source=source,
            written_by=written_by,
            reason=reason or f"rollback to version {to_version}",
            chunk_ids=[new_head.id],
            logical_ids=[logical_id],
            metadata={"to_version": str(to_version)},
        )

    # ----- audit helper ----------------------------------------------------

    def _emit_audit(
        self,
        *,
        persona_id: str,
        action: AuditAction,
        source: WriteSource,
        chunk_ids: list[str],
        logical_ids: list[str] | None = None,
        written_by: str | None = None,
        reason: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> None:
        store_kind: StoreKind = self.STORE_KIND  # type: ignore[assignment]
        event = AuditEvent(
            timestamp=datetime.now(UTC),
            persona_id=persona_id,
            action=action,
            store=store_kind,
            source=source,
            written_by=written_by,
            reason=reason,
            chunk_ids=chunk_ids,
            logical_ids=logical_ids or [],
            metadata=metadata or {},
        )
        self._audit.emit(event)
