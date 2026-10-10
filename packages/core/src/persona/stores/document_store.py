"""``DocumentStore`` — conversation-scoped storage for uploaded document chunks.

The fifth store kind (spec 14). A **sibling** of the four typed stores
(``identity`` / ``self_facts`` / ``worldview`` / ``episodic`` — spec 01), NOT
an extension. Documents are working material handed to the persona for a
conversation, NOT persona identity (Dominant Concern #1); the sibling
discipline reads itself in the type system.

Calling-convention discipline (D-14-X-scope-binding-discipline,
[architectural-rule][project-wide] CSA-1 in ``docs/DECISIONS.md``):
``DocumentStore`` reuses the existing :class:`persona.stores.backend.Backend`
transport by passing ``conversation_id`` into the ``persona_id`` slot at
every call. The parameter name on the Protocol stays as ``persona_id`` (Spec
01 stability > clarity-rename ripple across ~30 callers); this module
documents the substitution at the boundary, and every internal call sets
``store_kind=DOCUMENT_STORE_KIND`` so cross-contamination with the four
typed stores is structurally impossible.

What this class deliberately does NOT do:

- **No source-policy axis** (D-14-X-no-source-policy-on-documents). The
  ``WriteSource`` enum is meaningless for documents — the upload IS the
  provenance. ``write`` takes no ``source`` / ``force`` keywords.
- **No versioning** (sibling, not :class:`persona.stores.base.TypedStore`
  subclass — D-14-X-store-shared-base). Documents are immutable once
  uploaded; replacing a document is a delete + upload, not a version-bump.
- **No decay-rerank** (D-14-X-document-store-divergence-from-episodic).
  Uploaded documents are fresh-for-conversation; a 2-day-old upload is as
  relevant on turn 50 as on turn 1 until removed.
- **No spec-01 ``AuditLogger`` emission.** Audit happens at the API service
  boundary (:mod:`persona_api.services.document_service` will call
  :mod:`persona_api.services.audit_service` for upload/list/delete intents).
  Documents are not persona-scoped mutations; they don't go through the
  ``AuditEvent`` channel that records identity/self/worldview/episodic
  writes.

Criterion #6 (the binary structural guard for Dominant Concern #1) is
verified by ``packages/core/tests/integration/test_document_store_no_leak.py``
(T04, built immediately after this class): ``DocumentStore`` never invokes
the four typed stores' ``.write()`` methods on any path. T04 stays green for
the rest of Phase 5 as the regression guard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from persona.logging import get_logger
from persona.schema.documents import (
    DOCUMENT_STORE_KIND,
    DocumentChunk,
)

if TYPE_CHECKING:
    from persona.stores.backend import Backend

__all__ = ["DocumentStore"]


class DocumentStore:
    """Conversation-scoped store for uploaded-document chunks.

    Composes :class:`persona.stores.backend.Backend` directly; both
    :class:`persona.stores.chroma.ChromaBackend` (v0.1 default) and
    :class:`persona.stores.postgres.PostgresBackend` (spec 07 hosted path)
    satisfy the Protocol unchanged. The ``store_kind`` is fixed at
    :data:`persona.schema.documents.DOCUMENT_STORE_KIND` for every call.

    Args:
        backend: Storage transport. Reuses the existing typed-store backends.

    Note on the calling-convention discipline:
        Every call to ``backend`` sets ``persona_id=conversation_id`` (the
        Protocol's literal parameter name) and ``store_kind="document"``.
        The combination uniquely identifies a conversation-scoped document
        chunk and cannot collide with the four typed-store kinds.
    """

    def __init__(self, *, backend: Backend) -> None:
        self._backend = backend
        self._log = get_logger("stores.document")

    # ----- write -----------------------------------------------------------

    def write(self, conversation_id: str, chunks: list[DocumentChunk]) -> None:
        """Upsert ``chunks`` for ``conversation_id``.

        Empty ``chunks`` is a no-op. Each chunk is converted to its
        storage-boundary :class:`persona.schema.chunks.PersonaChunk`
        representation via :meth:`DocumentChunk.to_persona_chunk` and
        upserted under ``store_kind="document"``.

        Args:
            conversation_id: Conversation scope. Passed into the ``Backend``
                Protocol's ``persona_id`` slot per the calling-convention
                discipline (CSA-1).
            chunks: Document chunks to persist.
        """
        if not chunks:
            return
        persona_chunks = [chunk.to_persona_chunk() for chunk in chunks]
        self._backend.upsert(
            persona_id=conversation_id,
            store_kind=DOCUMENT_STORE_KIND,
            chunks=persona_chunks,
        )

    # ----- read ------------------------------------------------------------

    def query(
        self,
        conversation_id: str,
        query: str,
        top_k: int,
        **filters: Any,  # noqa: ANN401 — backend-specific filter shape
    ) -> list[DocumentChunk]:
        """Return up to ``top_k`` document chunks nearest to ``query``.

        Returns chunks ranked by ``Backend.query``'s cosine distance — no
        decay-rerank (D-14-X-document-store-divergence-from-episodic).
        Distance is carried over on each returned chunk so callers can
        compute confidence if needed.

        Args:
            conversation_id: Conversation scope.
            query: Query text.
            top_k: Maximum chunks to return.
            **filters: Backend-specific ``where`` filter
                (e.g. ``{"doc_ref": "tenancy.pdf"}`` to scope to one
                document under the metadata convention).

        Returns:
            Up to ``top_k`` :class:`DocumentChunk` instances, ordered by
            similarity (closest first).
        """
        results = self._backend.query(
            persona_id=conversation_id,
            store_kind=DOCUMENT_STORE_KIND,
            text=query,
            top_k=top_k,
            where=filters or None,
        )
        return [DocumentChunk.from_persona_chunk(c) for c in results]

    def get_all(self, conversation_id: str) -> list[DocumentChunk]:
        """Return every document chunk for ``conversation_id``.

        Useful for the prompt builder's small-doc whole-injection path
        (T14) and the document-list synopsis (T16) which both need to
        enumerate every attached document, not just the chunks retrieved
        this turn.
        """
        results = self._backend.get_all(
            persona_id=conversation_id,
            store_kind=DOCUMENT_STORE_KIND,
        )
        return [DocumentChunk.from_persona_chunk(c) for c in results]

    # ----- delete ----------------------------------------------------------

    def delete(self, conversation_id: str) -> None:
        """Remove every document chunk for ``conversation_id``. Idempotent.

        The cascade path called by Spec 14 T19 when a conversation is
        deleted (co-landing with Spec 13's T12 image-cascade per
        D-14-X-cascade-coordination).
        """
        self._backend.delete_persona(conversation_id, DOCUMENT_STORE_KIND)

    def delete_document(self, conversation_id: str, doc_ref: str) -> None:
        """Remove every chunk belonging to one document. Idempotent.

        Implements ``DELETE /v1/conversations/:id/documents/:ref`` (T18 /
        spec §9 criterion #10). Locates chunks via the 4-component
        chunk-ID format (D-14-X-document-chunk-id —
        ``{conversation_id}::document::{doc_ref}::{index}``) so a multi-
        document conversation can address each upload independently.

        Args:
            conversation_id: Conversation scope.
            doc_ref: The document reference. Must not contain the ``"::"``
                delimiter (validated by
                :func:`persona.schema.documents.make_document_chunk_id`).
        """
        if "::" in doc_ref:
            msg = f"doc_ref must not contain '::'; got {doc_ref!r}"
            raise ValueError(msg)

        prefix = f"{conversation_id}::{DOCUMENT_STORE_KIND}::{doc_ref}::"
        # A backend that can delete by id prefix does it in one statement: reading every
        # chunk of the conversation with its vector to find one document's cost 3 s and
        # 350 MiB at 20,000 chunks (fourth security review finding 3).
        delete_by_prefix = getattr(self._backend, "delete_by_id_prefix", None)
        if delete_by_prefix is not None:
            delete_by_prefix(
                persona_id=conversation_id, store_kind=DOCUMENT_STORE_KIND, prefix=prefix
            )
            return
        all_chunks = self._backend.get_all(
            persona_id=conversation_id,
            store_kind=DOCUMENT_STORE_KIND,
        )
        matching_ids = [c.id for c in all_chunks if c.id.startswith(prefix)]
        if not matching_ids:
            return
        self._backend.delete_documents(
            persona_id=conversation_id,
            store_kind=DOCUMENT_STORE_KIND,
            ids=matching_ids,
        )
