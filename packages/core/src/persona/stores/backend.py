"""The ``Backend`` transport protocol — the seam every storage backend fills.

The four typed stores (:mod:`persona.stores.base` ``TypedStore`` subclasses)
own policy, versioning, audit emission, and history/rollback — all
backend-agnostic. Underneath sits a *transport*: a narrow object that just
stores and retrieves chunks. :class:`persona.stores.chroma.ChromaBackend` is
the v0.1 local transport; :class:`persona.stores.postgres.PostgresBackend`
(spec 07) is the production transport. Both satisfy this protocol; the typed
stores compose either interchangeably (Liskov).

The surface is exactly ``ChromaBackend``'s — this protocol is reverse-engineered
from what that class already does, so adding a second backend is purely
additive. The method names are storage-neutral on purpose: ``delete_persona``
rather than Chroma's ``delete_collection`` (Postgres has rows, not collections).

Where the two transports are NOT equivalent, written down so nobody mistakes it for an
oversight (``recent`` and ``band_histogram`` are noted on their methods):

- **One current version per chain** (R9-205). Postgres enforces it with the partial unique
  index ``uq_memory_chain_head``: when two writers race on one chain, the second ``upsert``
  raises ``ChainConflictError`` and the typed store re-reads and appends on top. Chroma has no
  index. Two UPDATES racing in one process are still caught there, because an update names the
  version it supersedes and Chroma checks that under its folder lock (R9-271, below), so the
  loser re-reads and appends on top as on Postgres. Two FIRST writes to a new chain name
  nothing, so on Chroma both land at version 1 and the chain refuses every later write until an
  operator runs ``persona repair --resolve-raced``; so do two processes on one folder. Two
  identical persona saves still collapse into one version on both, because the id the store
  mints for an update is deterministic on what is being saved.
- **Forgetting a version of a chain** (``excise_versions``, R9-209) is one transaction on
  Postgres and two calls on Chroma, delete first, so the forgotten text goes on both. The
  rewrite is planned from a read, and another writer can land in between. Postgres locks the
  chain and compares it with that read inside the transaction, so a stale plan never lands:
  the forget reads again and plans again. Chroma makes the same comparison under the folder's
  lock, which every write in the process also takes (R9-271), so within one process no writer
  lands inside the excise. A writer in another process on the same folder still can, and a
  stale plan that lands then leaves a chain with a version missing, which ``persona repair``
  refuses and will not settle, so that memory's history raises until a person looks at it.
  Its current version still reads.
- **A write that must still find what it read** (``upsert`` with ``supersedes`` and
  ``requires``, R9-271). Postgres locks the evidence ``FOR KEY SHARE`` and the superseded
  versions ``FOR UPDATE``, each in id order, compares, and writes, in one transaction, so a
  forget either waits for the write and then removes what it wrote, or commits first and the
  write is refused. Chroma has no transaction and keeps the same order with a lock per folder
  and persona, which every write and delete in the process takes: exact within one process, and
  not across two processes on one folder, which Chroma does not support anyway.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from persona.schema.chunks import PersonaChunk

__all__ = ["Backend"]


@runtime_checkable
class Backend(Protocol):
    """The transport contract a :class:`TypedStore` composes.

    All methods are keyword-only past ``persona_id``/``store_kind`` to mirror
    the concrete backends and keep call sites self-documenting. The transport
    is dumb: it does no policy, versioning, or audit — those live in the typed
    store above it.
    """

    def upsert(
        self,
        *,
        persona_id: str,
        store_kind: str,
        chunks: list[PersonaChunk],
        supersedes: Mapping[str, tuple[int, str]] | None = None,
        requires: Sequence[str] = (),
        requires_kinds: Sequence[str] = (),
    ) -> None:
        """Insert or replace ``chunks`` for ``(persona_id, store_kind)``.

        Empty ``chunks`` is a no-op. The transport embeds each chunk's text
        via its injected embedder and stores the vector alongside the chunk.

        Every chunk is checked against its own ``content_hash`` first, and one bad chunk
        refuses the whole call with :class:`persona.errors.ChunkIntegrityError` before
        anything is embedded or stored (R9-210). The check belongs here because every writer
        passes through this method, and a bad row that lands only fails on some later read.

        ``supersedes`` and ``requires`` are what a write read before it decided, and the call
        lands only if they still hold (R9-271). Everything or nothing, on every transport:

        - ``supersedes`` maps the id of each version on disk this write replaces to the version
          number it was read at and the id of the chunk that replaces it. Each must still be
          there, still current and still at that number, or this raises
          :class:`persona.errors.ChainConflictError`; otherwise its ``superseded_by`` moves to the
          new id, and nothing else about it changes. A forget that deleted or renumbered it in
          between is how it stops holding, and without this check the write put the deleted
          version back.
        - ``requires`` names the ids this write was built from, to be found in one of
          ``requires_kinds`` for the persona, or this raises
          :class:`persona.errors.ForgottenEvidenceError`. A derived memory (a core block, a gist)
          is built during a model call, and a forget of its evidence can land in that call.

        Required of every transport. A transport that ignores them puts forgotten memories back.
        """
        ...

    def query(
        self,
        *,
        persona_id: str,
        store_kind: str,
        text: str,
        top_k: int,
        where: dict[str, Any] | None = None,  # noqa: ANN401 — backend-specific filter shape
    ) -> list[PersonaChunk]:
        """Return up to ``top_k`` chunks nearest to ``text`` by cosine distance.

        Each returned chunk has ``distance`` populated (cosine distance, where
        ``similarity = 1 - distance`` for L2-normalised embeddings). ``where``
        is a backend-specific metadata filter; ``None`` means no filter.
        """
        ...

    def get_all(
        self,
        *,
        persona_id: str,
        store_kind: str,
    ) -> list[PersonaChunk]:
        """Return every chunk for ``(persona_id, store_kind)`` (all versions)."""
        ...

    def count(
        self,
        *,
        persona_id: str,
        store_kind: str,
        include_superseded: bool = False,
    ) -> int:
        """Number of chunks for ``(persona_id, store_kind)`` without materialising them.

        ``include_superseded=False`` counts only current heads (the
        ``superseded_by IS NULL`` view queries return); ``True`` counts every
        stored version. The write path and the P8 measurement counters use
        this instead of ``len(get_all(...))`` (Spec K8, acceptance 1).
        """
        ...

    def recent(
        self,
        *,
        persona_id: str,
        store_kind: str,
        limit: int,
    ) -> list[PersonaChunk]:
        """Up to ``limit`` most-recently-created current chunks, newest first.

        Contract: current heads only, ordered ``(created_at, id)`` descending —
        never by lexicographic id (chunk ids mix count-era and uuidv7-era
        formats; ``created_at`` is the one honest insertion-order key, K8-D-6).
        Postgres pushes this down to ``ORDER BY … LIMIT``; Chroma materialises
        and sorts in-process (documented Liskov: the community/local transport
        holds modest per-persona volumes).
        """
        ...

    def get_by_logical_ids(
        self,
        *,
        persona_id: str,
        store_kind: str,
        logical_ids: list[str],
    ) -> list[PersonaChunk]:
        """Every version of the chunks whose ``provenance.logical_id`` is listed.

        The scoped replacement for the write path's full-store ``get_all``
        (Spec K8, K8-D-12): version computation only ever reads a chunk's own
        logical chain, so the write fetches exactly the batch's chains —
        genuinely-versioned kinds still find their true prior heads; a fresh
        chain fetches nothing. Chunks without provenance are never returned
        (they belong to no chain). Empty ``logical_ids`` returns ``[]``.
        """
        ...

    def get_by_ids(
        self,
        *,
        persona_id: str,
        store_kind: str,
        ids: list[str],
    ) -> list[PersonaChunk]:
        """The chunks with these PHYSICAL ids, in no guaranteed order (Spec K13, T1).

        The counterpart to :meth:`get_by_logical_ids`, and the difference matters: a physical
        id names one version, a logical id names a chain. They are the same string for a
        chunk that has never been updated, which is why passing one where the other belongs
        looks like it works right up until a chain has a second version.

        Ids that are not there are simply absent from the result (never an error), so this
        doubles as the honest existence check. Empty ``ids`` returns ``[]``.
        """
        ...

    def reinforce(
        self,
        *,
        persona_id: str,
        store_kind: str,
        ids: list[str],
        recalled_at: Any,  # noqa: ANN401 — datetime; Any avoids a runtime import cycle here
    ) -> None:
        """Batched lifecycle bump: ``strength += 1``, ``last_recalled_at = recalled_at``.

        The K8-D-5 reinforcement primitive — ONE statement for the whole
        recalled id-set (Postgres: a single UPDATE; Chroma: a metadata-only
        update, no re-embed). Lifecycle state is hash-excluded, so this never
        changes chunk identity. Unknown ids are silently skipped (idempotent
        against races with deletes). Empty ``ids`` is a no-op.
        """
        ...

    def set_bands(
        self,
        *,
        persona_id: str,
        store_kind: str,
        bands: dict[str, int],
    ) -> None:
        """Materialize fidelity bands: ``{chunk_id: band}`` in one batch (K8 T7).

        Lifecycle-only, like ``reinforce``: touches the hash-excluded band
        state, never text/embedding/metadata — no identity churn, no re-embed.
        Unknown ids are silently skipped; empty ``bands`` is a no-op. The
        tiering pass writes what :func:`persona.stores.lifecycle.classify_band`
        computed — the read path trusts the stored value (K8-D-3 as clarified:
        materialized, staleness harmless).
        """
        ...

    def band_histogram(
        self,
        *,
        persona_id: str,
        store_kind: str,
    ) -> dict[int, int]:
        """Chunk count per fidelity band (the K8-D-7 / P8 counter surface).

        Postgres serves it as ``GROUP BY fidelity_band``; Chroma materialises
        in-process (documented Liskov, community volumes). Current heads only.
        """
        ...

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        """Remove every chunk for ``(persona_id, store_kind)``. Idempotent.

        Storage-neutral name for the per-persona-per-kind wipe. Chroma drops a
        collection; Postgres deletes rows. The caller does not know or care.
        """
        ...

    def relink(
        self,
        *,
        persona_id: str,
        store_kind: str,
        links: dict[str, str | None],
        versions: dict[str, int] | None = None,
    ) -> None:
        """Set ``superseded_by`` on the listed chunk ids and touch nothing else (Spec K13).

        The repair primitive (D-K13-6). ``links`` maps a chunk id to the id of the version
        that supersedes it, or ``None`` to make it the current version. Unknown ids are
        silently skipped, empty ``links`` is a no-op, and the whole map is applied as one
        operation per transport.

        Lifecycle-only in the sense that matters here: text, metadata and the embedding are
        never read and never written, so this needs no embedder and can never re-embed. That
        is what lets ``persona repair`` run without loading a model. ``content_hash`` covers
        text plus metadata and NOT provenance, so a relink also cannot change chunk identity
        or trip the tamper check.

        ``versions`` renumbers the listed chunks in the same operation, for the one repair that
        needs it, settling a raced chain (R9-205). Provenance as well, so the same holds.
        """
        ...

    def excise_versions(
        self,
        *,
        persona_id: str,
        store_kind: str,
        logical_id: str,
        expected: dict[str, tuple[int, str | None]],
        doomed_ids: list[str],
        links: dict[str, str | None],
        versions: dict[str, int],
    ) -> None:
        """Delete these versions of a chain and close it up around them (R9-209).

        ``links`` and ``versions`` move the survivors, exactly as :meth:`relink` does, so no text
        is rewritten and nothing is embedded. The deletes come first on both transports, so a
        forget never leaves the row it was asked to remove.

        ``expected`` is the chain the plan was made from, every row of ``logical_id`` as
        ``{id: (version, superseded_by)}``. If the chain on disk no longer matches, another
        writer moved it since that read, the plan is stale, and this raises
        :class:`persona.errors.ChainConflictError` without changing anything, so the caller
        reads again and plans again. A stale plan that leaves the head alone would otherwise
        land without complaint and leave a hole no repair will touch.

        The transports differ here, and the difference is the documented Liskov one. Postgres
        locks the chain, checks it and rewrites it in one transaction. Chroma has no transaction,
        so it checks just before acting and then makes two calls: a writer landing between the
        check and the calls is not seen, and a process killed between the calls leaves the rows
        gone and the chain unclosed. Either way the text is forgotten and reads still work.
        """
        ...

    def delete_documents(
        self,
        *,
        persona_id: str,
        store_kind: str,
        ids: list[str],
    ) -> None:
        """Remove the listed chunk ids (not logical ids). Idempotent."""
        ...
