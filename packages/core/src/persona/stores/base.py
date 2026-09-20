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

from pydantic import BaseModel, ConfigDict

from persona.audit import AuditAction, AuditEvent
from persona.logging import get_logger
from persona.schema.chunks import (
    BOOKKEEPING_METADATA_KEYS,
    ChunkProvenance,
    PersonaChunk,
    WriteSource,
)
from persona.stores.policy import PolicyTable, evaluate_write_policy
from persona.stores.versioning import (
    ChainDefect,
    ChainDiagnosis,
    compute_next_version,
    current_version,
    current_view,
    diagnose_chain,
    link_supersedes,
    plan_relink,
    validate_chain,
)

if TYPE_CHECKING:
    from persona.audit import AuditLogger, StoreKind
    from persona.stores.backend import Backend

__all__ = ["StoreDiagnosis", "TypedStore"]


#: How many extra candidates a versioned query asks the backend for, so superseded versions
#: inside the window cannot cost the caller results. Three is the convention the episodic
#: store already used for its retention rerank; the bound stays small because the filtered
#: answer is truncated back to ``top_k`` immediately.
_SUPERSEDED_OVERFETCH = 3


class StoreDiagnosis(BaseModel):
    """What a scan of one store found (Spec K13, T3).

    ``chains_checked`` is reported even when ``findings`` is empty, because the ordinary
    answer is "nothing wrong" and a maintenance command that says nothing at all reads like
    it did not run.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    store_kind: str
    chains_checked: int
    findings: tuple[ChainDiagnosis, ...] = ()

    @property
    def healthy(self) -> bool:
        """True when nothing needs attention."""
        return not self.findings


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

        # ONE upsert, deduplicated by id, supersede links before new heads
        # (Spec K13, D-K13-1 and D-K13-2).
        #
        # This was two backend calls, the links and then the heads, with nothing around them.
        # A process that died between them left the prior head pointing at a chunk that was
        # never written: zero current heads, so an ordinary read could not find the node at
        # all, and a dangling link, so history() and rollback() raised on that logical id from
        # then on, permanently, with no repair anywhere in the package. Measured on the real
        # transports with a real SIGKILL rather than a raised exception standing in for one:
        # two calls tore 4 of 8 kills, one call tore 0 of 8 (chromadb 1.5.9). One call is one
        # transaction on Postgres, and one all or nothing operation on Chroma.
        #
        # The dedupe is not tidiness, it is the other half of the fix. When one batch carries
        # two updates to the SAME logical chain, the chunk that is version N+1 for the first
        # update is the prior head of the second, so its id appears in BOTH lists: unlinked in
        # ``prepared``, linked in ``supersede_updates``. The old order wrote the link and then
        # overwrote it with the unlinked copy, leaving two heads and a chain that raised on
        # read, with no crash involved (R9-206). Ordering alone cannot fix that either, because
        # the transports disagree about duplicates: Postgres silently keeps the last row of an
        # executemany, Chroma raises DuplicateIDError and refuses the whole call. So the
        # duplicate never reaches a transport, and the LINKED copy is the one that survives.
        #
        # Links before heads is for a constraint that does not exist yet: the partial unique
        # index on current heads (R9-205) is checked per statement, so a batch that wrote a new
        # head before the old one lost its NULL would trip it. Ordering costs nothing today and
        # keeps that index addable without reopening this method.
        superseded_ids = {c.id for c in supersede_updates}
        batch = [*supersede_updates, *(c for c in prepared if c.id not in superseded_ids)]
        self._backend.upsert(
            persona_id=persona_id,
            store_kind=self.STORE_KIND,
            chunks=batch,
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
            # An update must never reuse a physical id already in this chain. Every backend
            # writes ON CONFLICT (id) DO UPDATE, so a reused id does not append a version, it
            # OVERWRITES the one it claims to supersede: the store was left holding a single
            # row carrying ``version=2``, and ``history()`` then raised BrokenVersionChainError
            # on the chain it was asked to read. Minting here, rather than trusting the caller,
            # makes append-only a property of the STORE instead of a rule every writer has to
            # remember. Callers that already mint distinct ids (``persona.autonomy`` puts the
            # version in its own id) are untouched: the id changes only when it would collide.
            chain_ids = {
                c.id
                for c in existing
                if (c.provenance.logical_id if c.provenance is not None else c.id) == logical_id
            }
            if prepared.id in chain_ids:
                prepared = prepared.model_copy(update={"id": f"{logical_id}::v{next_version:04d}"})
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
        # Ask for more than the caller wants, because a superseded version sitting in the
        # window used to cost a result outright: the filter below ran on the ANSWER, so a
        # logical chain edited more times than ``top_k`` could fill the whole window with its
        # own dead versions and return nothing current. That is silent memory loss, and it grew
        # with use (``persona.autonomy`` appends a versioned chain into self_facts, so its dead
        # versions accumulate for every persona that learns).
        #
        # The Postgres backend excludes them in SQL, so this over-read costs it nothing. The
        # over-fetch is what keeps the guarantee true for a backend that does not push down.
        fetch_k = top_k * _SUPERSEDED_OVERFETCH if self.SUPPORTS_VERSIONING else top_k
        results = self._backend.query(
            persona_id=persona_id,
            store_kind=self.STORE_KIND,
            text=query,
            top_k=fetch_k,
            where=filters or None,
        )
        # Filter out superseded versions (queries return the current view), THEN honour the
        # caller's bound, so top_k counts what it promises: current chunks.
        current = [c for c in results if c.provenance is None or c.provenance.superseded_by is None]
        return current[:top_k]

    def get_all(
        self,
        persona_id: str,
        *,
        include_superseded: bool = False,
    ) -> list[PersonaChunk]:
        all_chunks = self._backend.get_all(persona_id=persona_id, store_kind=self.STORE_KIND)
        if include_superseded:
            return all_chunks
        # ``current_view`` rather than a plain superseded filter (Spec K13, D-K13-8): a chain
        # broken by an interrupted save has NO unsuperseded row, so a plain filter drops the
        # node out of every read while its rows sit on disk. The fallback is deliberately
        # narrow, only a chain with no current version at all, and it is a READ: it never
        # writes the repair back. ``persona repair`` does that, on purpose and audited.
        return current_view(all_chunks)

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

    def chain_ids(self, persona_id: str, doc_ids: list[str]) -> list[str]:
        """Every version of every chain the given PHYSICAL ids belong to (Spec K13, T1).

        A forget is asked for in physical ids, because that is what a search hands back, and
        a search only ever returns current versions. Deleting exactly those ids removes the
        version the user can see and leaves every superseded version of the same fact on
        disk, still holding the text they asked to be rid of, with the older version now
        pointing at a row that no longer exists (R9-201). So a delete resolves its ids to
        their chains first.

        Ids that belong to no chain (a legacy chunk written before provenance, or an id that
        is simply not there) are returned unchanged, so a caller always deletes at least what
        it asked for and never less. Idempotent: expanding an already expanded set adds
        nothing. Read only.
        """
        if not doc_ids or not self.SUPPORTS_VERSIONING:
            return list(doc_ids)
        named = self._backend.get_by_ids(
            persona_id=persona_id, store_kind=self.STORE_KIND, ids=list(doc_ids)
        )
        logical_ids = sorted({c.provenance.logical_id for c in named if c.provenance is not None})
        siblings = (
            self._backend.get_by_logical_ids(
                persona_id=persona_id, store_kind=self.STORE_KIND, logical_ids=logical_ids
            )
            if logical_ids
            else []
        )
        return sorted({*doc_ids, *(c.id for c in siblings)})

    def remove_documents(self, persona_id: str, doc_ids: list[str]) -> None:
        """Delete the named chunks AND every other version of the same facts.

        The audit event records the ids actually deleted rather than the ids asked for, so
        the trail can be read afterwards as proof of what went.
        """
        if not doc_ids:
            return
        doomed = self.chain_ids(persona_id, doc_ids)
        self._backend.delete_documents(
            persona_id=persona_id, store_kind=self.STORE_KIND, ids=doomed
        )
        self._emit_audit(
            persona_id=persona_id,
            action=AuditAction.REMOVE_DOCUMENTS,
            source=WriteSource.USER,
            chunk_ids=doomed,
        )

    # ----- history / rollback ---------------------------------------------

    def history(self, persona_id: str, logical_id: str) -> list[PersonaChunk]:
        if not self.SUPPORTS_VERSIONING:
            from persona.errors import RuntimeWriteForbiddenError

            msg = "history is not supported on this store"
            raise RuntimeWriteForbiddenError(
                msg, context={"store": self.STORE_KIND, "persona_id": persona_id}
            )
        # The indexed chain read, not a full store scan (Spec K13, D-K13-13). Behaviourally
        # identical: this filters on provenance anyway, which is exactly the set
        # ``get_by_logical_ids`` never returns, on both transports. Measured on 3,000 rows
        # with real 384 dimension vectors: the ``get_all`` shape took 216 ms because
        # ``SELECT *`` ships every embedding for ``_row_to_chunk`` to throw away, and one
        # chain by logical id takes 1 ms. ``rollback`` calls this, and so do
        # ``persona.autonomy`` and the tool-consent path on every write they make.
        chain = [
            c
            for c in self._backend.get_by_logical_ids(
                persona_id=persona_id,
                store_kind=self.STORE_KIND,
                logical_ids=[logical_id],
            )
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
        # The target's text comes forward; its BOOKKEEPING does not (Spec K13, D-K13-12).
        # Metadata holds two different things, and only one of them belongs to the text. A
        # conversation id is how the conversation-delete cascade FINDS a chunk, and a session
        # id is how the autonomy cooldown GATES the next write: carrying those forward stamps
        # a new row with a conversation that may have been deleted and a session that ended,
        # and both are then read as current by code that has no way to know they time
        # travelled. Everything that describes the text itself rides along with it.
        new_head = PersonaChunk(
            id=new_id,
            text=target.text,
            metadata={
                key: value
                for key, value in target.metadata.items()
                if key not in BOOKKEEPING_METADATA_KEYS
            },
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

    # ----- diagnose / repair (Spec K13, T3) ---------------------------------

    def _chains(self, persona_id: str) -> dict[str, list[PersonaChunk]]:
        """Every logical chain this store holds for the persona, keyed by logical id."""
        chains: dict[str, list[PersonaChunk]] = {}
        for chunk in self._backend.get_all(persona_id=persona_id, store_kind=self.STORE_KIND):
            if chunk.provenance is None:
                continue  # unversioned rows belong to no chain
            chains.setdefault(chunk.provenance.logical_id, []).append(chunk)
        return chains

    def diagnose(self, persona_id: str) -> StoreDiagnosis:
        """Check every version chain in this store. Reads only; repairs nothing.

        The expected answer is that nothing is wrong, so the result carries the number of
        chains checked as well as the findings: a scan that found a healthy store should be
        able to say so.
        """
        if not self.SUPPORTS_VERSIONING:
            return StoreDiagnosis(store_kind=self.STORE_KIND, chains_checked=0)
        chains = self._chains(persona_id)
        findings = [
            diagnosis
            for diagnosis in (diagnose_chain(chain) for chain in chains.values())
            if diagnosis.defect is not ChainDefect.NONE
        ]
        findings.sort(key=lambda d: d.logical_id)
        return StoreDiagnosis(
            store_kind=self.STORE_KIND,
            chains_checked=len(chains),
            findings=tuple(findings),
        )

    def repair(self, persona_id: str, *, written_by: str = "stores.repair") -> int:
        """Put the repairable chains right, and return how many were repaired.

        Only the chains :func:`persona.stores.versioning.diagnose_chain` calls repairable are
        touched; the rest are left exactly as they are, because rebuilding links from the
        version order is only honest while the version order is known to be the truth. The
        write goes through the transport's ``relink``, which moves version pointers and
        nothing else: no text, no metadata, no embedding, so no model is loaded and no vector
        moves. One audit event per repaired chain.
        """
        if not self.SUPPORTS_VERSIONING:
            return 0
        repaired = 0
        for logical_id, chain in self._chains(persona_id).items():
            diagnosis = diagnose_chain(chain)
            if diagnosis.defect is ChainDefect.NONE or not diagnosis.repairable:
                continue
            links = plan_relink(chain)
            if not links:
                continue
            self._backend.relink(persona_id=persona_id, store_kind=self.STORE_KIND, links=links)
            self._emit_audit(
                persona_id=persona_id,
                action=AuditAction.REPAIR,
                source=WriteSource.USER,
                written_by=written_by,
                reason=f"repaired a {diagnosis.defect} version chain",
                chunk_ids=sorted(links),
                logical_ids=[logical_id],
                metadata={"defect": str(diagnosis.defect)},
            )
            repaired += 1
        return repaired

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
