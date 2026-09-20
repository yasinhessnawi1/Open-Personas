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
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
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
    ) -> None:
        """Insert or replace ``chunks`` for ``(persona_id, store_kind)``.

        Empty ``chunks`` is a no-op. The transport embeds each chunk's text
        via its injected embedder and stores the vector alongside the chunk.
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
