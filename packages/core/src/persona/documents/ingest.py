"""Size-aware ingestion strategy (spec 14 T12).

The decision function that bridges the parsers (T06–T11) → the chunker
(T05) → the DocumentStore (T03). On upload, a document goes through one
of three paths:

- **Whole-inject** (small docs, under D-14-1's token threshold). Returns
  the full text for the API service (T13) to attach to the conversation
  at the conversation-level (D-14-5 — small docs persist for the whole
  conversation). No `DocumentStore` writes.
- **Retrieval** (large docs, over the threshold). The chunker (T05) splits
  the parser-emitted :class:`DocumentSection` units into
  :class:`~persona.schema.documents.DocumentChunk` instances; the
  :class:`~persona.stores.document_store.DocumentStore` persists them
  (under ``store_kind="document"``, scope=`conversation_id`); the prompt
  builder retrieves chunks per turn (T15) above-episodic-when-retrieved
  (D-14-5).
- **Vision-handoff-required** (scanned PDFs — parse_result.needs_vision_handoff).
  The ingest layer does NOT chunk-and-embed (the document has no text to
  chunk); the API service (T13) reads the strategy and returns a clean
  422 *"vision_handoff_required"* response (interim until T21 lands the
  actual vision handoff per the user's T10 framing note + Spec 13
  fail-loud discipline).

D-14-1 (the 3000-token threshold) + env override
``PERSONA_DOC_INJECT_THRESHOLD`` lets operators tune for tighter contexts
(small-tier-primary deploys) or more generous frontier-context deploys.

The "if conflict in practice, the threshold drops, NOT the ladder
rearranges" sub-decision (D-14-1 sub) means: **this module's threshold is
the variable Phase 5 tunes under pressure**. The Spec 05 reduction ladder
(identity + constraints floor → episodic → worldview → self-facts) stays
untouched; documents fit underneath that floor or fall to retrieval.

This module is **storage-agnostic** about workspace files — it does NOT
touch the persona workspace or `resolve_sandbox_path`. T13's service
layer owns the workspace IO; T12 owns the strategy decision and the
DocumentStore write.
"""

from __future__ import annotations

import os
from enum import StrEnum
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from persona.documents.chunker import (
    DEFAULT_CHUNK_SIZE_TOKENS,
    DEFAULT_OVERLAP_TOKENS,
    chunk_document,
)
from persona.skills._tokens import count_tokens_capped

if TYPE_CHECKING:
    from persona.documents.parsers import ParseResult
    from persona.stores.document_store import DocumentStore

__all__ = [
    "DEFAULT_INJECT_THRESHOLD",
    "INJECT_THRESHOLD_ENV_VAR",
    "INJECT_THRESHOLD_MAX",
    "INJECT_THRESHOLD_MIN",
    "MAX_DOCUMENT_CHUNKS",
    "IngestResult",
    "IngestStrategy",
    "decide_strategy",
    "ingest_document",
    "resolve_inject_threshold",
]

#: D-14-1 default whole-inject vs chunk-retrieve threshold (tokens).
#: 3000 tokens leaves ~600 tokens cushion in the small tier's 8k window
#: after identity/constraints/skill-index/recent-history budgets.
DEFAULT_INJECT_THRESHOLD: int = 3000

#: Env override for operators tuning per-tier window size.
INJECT_THRESHOLD_ENV_VAR: str = "PERSONA_DOC_INJECT_THRESHOLD"

#: Minimum sensible value (below this, every document goes to retrieval —
#: even tiny memos — defeating the point of whole-injection).
INJECT_THRESHOLD_MIN: int = 100

#: Maximum sensible value (frontier-context tier window minus a generous
#: identity + history allowance). 64k tokens is roughly Claude/DeepSeek
#: window; allow up to half for whole-injection.
INJECT_THRESHOLD_MAX: int = 32_000


class IngestStrategy(StrEnum):
    """The ingestion paths a document can take.

    ``VISION_HANDOFF_REQUIRED`` is what :func:`ingest_document` returns when
    the parser sets ``needs_vision_handoff=True`` — the caller (T13's
    :func:`persona_api.services.document_service.upload`) detects this and
    performs the actual rasterisation + ImageContent creation (T21). The
    caller-side outcome ``VISION_HANDOFF`` records the completed handoff
    on the persisted :class:`DocumentRef`.
    """

    WHOLE_INJECT = "whole_inject"
    RETRIEVAL = "retrieval"
    VISION_HANDOFF_REQUIRED = "vision_handoff_required"
    VISION_HANDOFF = "vision_handoff"


class IngestResult(BaseModel):
    """The outcome of :func:`ingest_document`.

    The API service (T13) reads :attr:`strategy` to decide:

    - ``WHOLE_INJECT`` → attach :attr:`full_text` to the conversation;
      prompt-builder T14 reads it.
    - ``RETRIEVAL`` → no further action (chunks already written to
      :class:`~persona.stores.document_store.DocumentStore`); prompt-
      builder T15 queries per turn.
    - ``VISION_HANDOFF_REQUIRED`` → return 422
      ``"vision_handoff_required"`` to the user (interim until T21
      wires the real vision handoff).

    :attr:`token_count` is the cl100k_base estimate of the full document
    text. Used for the synopsis (T16) when the parser didn't report a
    natural size signal (page count / sheet count).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    strategy: IngestStrategy
    doc_ref: str
    token_count: int = Field(ge=0)
    #: When ``strategy == WHOLE_INJECT``, the full document text to attach
    #: to the conversation. ``None`` otherwise.
    full_text: str | None = None
    #: When ``strategy == RETRIEVAL``, the number of chunks written. ``0``
    #: otherwise.
    chunk_count: int = Field(default=0, ge=0)
    #: ``True`` when the document had more text than :data:`MAX_DOCUMENT_CHUNKS`
    #: chunks hold, so only the first chunks were written.
    chunks_truncated: bool = False


#: The most chunks one document writes to the store (D-FV0-31; third security review,
#: approved 1,000). Every chunk is embedded, at about 0.3 s of CPU each on the
#: production embedder: 1,000 chunks are about 448,000 tokens of text, roughly 600
#: pages of prose and five minutes of background CPU. Past this many the rest of the
#: document is not indexed, and the document says so (``partial``).
MAX_DOCUMENT_CHUNKS: int = 1_000


def resolve_inject_threshold() -> int:
    """Resolve the whole-inject threshold from the env override + clamp.

    Fail-safe: malformed env values or out-of-range values fall back to
    the default.
    """
    raw = os.environ.get(INJECT_THRESHOLD_ENV_VAR)
    if not raw:
        return DEFAULT_INJECT_THRESHOLD
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_INJECT_THRESHOLD
    if parsed < INJECT_THRESHOLD_MIN or parsed > INJECT_THRESHOLD_MAX:
        return DEFAULT_INJECT_THRESHOLD
    return parsed


def ingest_document(
    *,
    parse_result: ParseResult,
    conversation_id: str,
    doc_ref: str,
    title: str,
    document_format: str,
    document_store: DocumentStore,
    threshold_tokens: int | None = None,
    chunk_size_tokens: int = DEFAULT_CHUNK_SIZE_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> IngestResult:
    """Decide ingestion strategy + persist chunks for the retrieval path.

    Args:
        parse_result: Output of :func:`persona.documents.parsers.parse_document`.
        conversation_id: Conversation scope. Passed into ``DocumentStore.write``'s
            ``conversation_id`` slot (CSA-1 calling-convention discipline).
        doc_ref: Document reference within the conversation. Stamped onto
            chunk IDs via
            :func:`persona.schema.documents.make_document_chunk_id` so
            per-document deletion (T18) works as a prefix-match.
        title: Display title for the document (typically the filename).
        document_format: One of ``"pdf"`` / ``"docx"`` / ``"xlsx"`` /
            ``"csv"`` / ``"txt"`` / ``"md"`` / ``"code"``.
        document_store: The conversation-scoped store. Composed by the API
            service layer (T13).
        threshold_tokens: Whole-inject threshold override. ``None`` reads
            from the env / falls back to the default (D-14-1, 3000).
        chunk_size_tokens: Per-chunk cap for the retrieval path. Default
            512 (D-14-4).
        overlap_tokens: Adjacent-chunk overlap for the retrieval path.
            Default 64 (D-14-4).

    Returns:
        :class:`IngestResult` carrying the chosen strategy + outcome data
        for T13 to act on.
    """
    # Vision-handoff short-circuit (D-14-1 + the user's T10/T13 framing).
    # The document has no text to chunk; T13 reads this and returns 422.
    if parse_result.needs_vision_handoff:
        return IngestResult(
            strategy=IngestStrategy.VISION_HANDOFF_REQUIRED,
            doc_ref=doc_ref,
            token_count=0,
        )

    decided = decide_strategy(parse_result, doc_ref=doc_ref, threshold_tokens=threshold_tokens)
    if decided.strategy != IngestStrategy.RETRIEVAL:
        return decided

    # Retrieval path: chunk + write to DocumentStore.
    chunks = chunk_document(
        sections=list(parse_result.sections),
        conversation_id=conversation_id,
        doc_ref=doc_ref,
        document_format=document_format,
        title=title,
        chunk_size_tokens=chunk_size_tokens,
        overlap_tokens=overlap_tokens,
        max_chunks=MAX_DOCUMENT_CHUNKS + 1,
    )
    truncated = len(chunks) > MAX_DOCUMENT_CHUNKS
    chunks = chunks[:MAX_DOCUMENT_CHUNKS]
    document_store.write(conversation_id, chunks)

    return decided.model_copy(update={"chunk_count": len(chunks), "chunks_truncated": truncated})


def decide_strategy(
    parse_result: ParseResult,
    *,
    doc_ref: str,
    threshold_tokens: int | None = None,
) -> IngestResult:
    """Decide how a parsed document is used, without chunking or storing anything.

    The upload path calls this (D-FV0-31, third security review): a retrieval
    document is indexed later by the background job, never inside the request.
    Tokens are counted only until the count passes the threshold, so a four-million
    character document costs one segment of counting here, not a whole encode. For a
    retrieval document ``token_count`` is therefore that partial count: more than the
    threshold, not the document's total.
    """
    if parse_result.needs_vision_handoff:
        return IngestResult(
            strategy=IngestStrategy.VISION_HANDOFF_REQUIRED,
            doc_ref=doc_ref,
            token_count=0,
        )

    full_text = parse_result.full_text
    effective_threshold = (
        threshold_tokens if threshold_tokens is not None else resolve_inject_threshold()
    )
    token_count = count_tokens_capped(full_text, effective_threshold) if full_text else 0

    if token_count == 0:
        # No usable text — the parsers raise CorruptDocumentError before
        # reaching here, but guard the boundary. Treat as whole-inject
        # with empty text so T13 surfaces the situation cleanly.
        return IngestResult(
            strategy=IngestStrategy.WHOLE_INJECT,
            doc_ref=doc_ref,
            token_count=0,
            full_text="",
        )

    if token_count <= effective_threshold:
        return IngestResult(
            strategy=IngestStrategy.WHOLE_INJECT,
            doc_ref=doc_ref,
            token_count=token_count,
            full_text=full_text,
        )

    return IngestResult(
        strategy=IngestStrategy.RETRIEVAL,
        doc_ref=doc_ref,
        token_count=token_count,
    )
