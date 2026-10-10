"""Document-aware chunking — natural boundaries with token-aware fallback.

T05 (spec 14). Produces :class:`persona.schema.documents.DocumentChunk`
instances from parser-emitted structural :class:`DocumentSection` units.

Strategy (D-14-4):

1. **Natural-boundary first.** The parsers (T06–T10) emit ``DocumentSection``
   per paragraph / heading-block / sheet / page so the chunker can keep
   semantically-coherent units together where they fit. Retrieved chunks
   then read as whole paragraphs or whole sheets, not as arbitrary slices.
2. **Token windows, cut at a separator.** When a single ``DocumentSection``
   exceeds the chunk-token cap, it is cut into windows of at most
   ``chunk_size_tokens`` tokens. Each window ends at the last paragraph break
   (``\\n\\n``) in its final quarter, else the last line break, else the last
   space, else exactly at the cap. Chunks therefore fill to between three
   quarters of the cap and the cap. The recursive split this replaced counted
   each part's tokens on its own and summed them, which overcounted and left
   chunks at 224 to 331 tokens of 512 (third security review, finding 14).
   The text is encoded in segments (:data:`persona.skills._tokens.SEGMENT_CHARS`),
   never as one list for the whole section, and each character is encoded
   about twice in all. This is the inline "Spec 01 fallback" path D-14-4
   names; the four typed stores' chunking (one chunk per YAML field, no
   splitting) is untouched.
3. **Overlap.** Adjacent chunks within the same section share about
   ``overlap_tokens`` tokens: the next window starts that many tokens before
   the last one ended, moved forward to a word start when one is near, so
   retrieval over a chunk boundary doesn't lose semantic continuity. Default
   64 tokens (R-14-3 + predecessor convention at
   ``uia-rag-chatbot/src/uia_rag/chunking/splitter.py:27``).

Defaults (D-14-4 + R-14-3):

- ``chunk_size_tokens = 512`` — practitioner consensus for
  ``bge-small-en-v1.5``-class embedders + the predecessor's ``SentenceChunker``
  default.
- ``overlap_tokens = 64`` — same source.

Token counting goes through :func:`persona.skills.count_tokens` (the shared
``cl100k_base`` encoder, D-05-8). Estimate-for-budgeting; never conflated
with provider ``usage`` for billing. It is imported on first use, not at module
import: the parsers import :class:`DocumentSection` from here, and the document
parse worker (D-FV0-31) must start without ``persona.skills``, which loads the
tokenizer and, through the package, the provider SDKs.

This module deliberately does NOT modify
:mod:`persona.schema.chunks` /  :mod:`persona.registry` / the four typed
stores. The Phase 1 chunking contract (one ``PersonaChunk`` per
identity/self-facts/worldview/episodic field, no splitting) stays byte-for-
byte unchanged — see :func:`chunk_document`'s module-level docstring and
the regression assertion in
``packages/core/tests/unit/documents/test_chunker.py``.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from persona.schema.documents import DocumentChunk, make_document_chunk_id

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from types import ModuleType

__all__ = [
    "DEFAULT_CHUNK_SIZE_TOKENS",
    "DEFAULT_OVERLAP_TOKENS",
    "ChunkCursor",
    "DocumentSection",
    "chunk_document",
    "iter_document_chunks",
]


@functools.cache
def _tokens() -> ModuleType:
    """The shared ``cl100k_base`` token module, imported on first use."""
    from persona.skills import _tokens as module  # noqa: PLC0415

    return module


def count_tokens(text: str) -> int:
    """Count ``text`` with the shared encoder, importing it on first use."""
    return int(_tokens().count_tokens(text))


def count_tokens_capped(text: str, cap: int) -> int:
    """The exact count when at most ``cap``, else a number above it, from about one segment."""
    return int(_tokens().count_tokens_capped(text, cap))


def _encode(text: str) -> list[int]:
    """Encode one segment (bounded by the caller), special-token strings as plain text."""
    tokens: list[int] = _tokens().encode(text)
    return tokens


def _token_starts(tokens: list[int]) -> list[int]:
    """The character offset at which each token starts, in the text the tokens encode."""
    _, starts = _tokens()._ENCODER.decode_with_offsets(tokens)  # noqa: SLF001
    return list(starts)


#: Default per-chunk token cap (R-14-3 + predecessor convention).
DEFAULT_CHUNK_SIZE_TOKENS: int = 512

#: Default overlap between adjacent chunks within a section.
DEFAULT_OVERLAP_TOKENS: int = 64


class DocumentSection(BaseModel):
    """A parser-emitted natural-boundary unit of document text.

    Parsers (T06–T10) emit a list of these representing the document's
    structural shape: one section per paragraph for prose, per sheet for
    spreadsheets, per page for PDFs (or a finer granularity where the
    parser can infer it). The chunker uses the metadata to stamp each
    resulting :class:`DocumentChunk` so retrieved chunks can cite their
    source location.

    Attributes:
        text: The section's textual content.
        page: 1-indexed page number, when applicable (PDF, docx).
        section: Heading path or section identifier ("3.2 Methodology"),
            when applicable (prose).
        sheet: Sheet name, when applicable (XLSX).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    text: str
    page: int | None = Field(default=None, ge=1)
    section: str | None = None
    sheet: str | None = None


@dataclass(frozen=True)
class ChunkCursor:
    """Where chunking resumes: a section, a character offset into it, and the next index.

    ``offset`` counts characters of the section's stripped text. A cursor that
    :func:`iter_document_chunks` yields, passed back as ``start``, continues exactly
    where it stopped: the same chunks, with the same indexes, follow.
    """

    section: int = 0
    offset: int = 0
    index: int = 0


def chunk_document(
    sections: list[DocumentSection],
    *,
    conversation_id: str,
    doc_ref: str,
    document_format: str,
    title: str,
    chunk_size_tokens: int = DEFAULT_CHUNK_SIZE_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
    max_chunks: int | None = None,
) -> list[DocumentChunk]:
    """Split sections into :class:`DocumentChunk` instances.

    Empty input or all-empty sections produce an empty list (fail-safe; the
    predecessor's :func:`uia_rag.chunking.splitter.SentenceChunker.chunk_document`
    pattern).

    Args:
        sections: Parser-emitted natural-boundary units, in document order.
        conversation_id: Conversation scope. Stamped onto each chunk's ID
            via :func:`persona.schema.documents.make_document_chunk_id`.
        doc_ref: Document reference within the conversation. Stamped onto
            each chunk's ID + metadata.
        document_format: One of ``"pdf"`` / ``"docx"`` / ``"xlsx"`` /
            ``"csv"`` / ``"txt"`` / ``"md"`` / ``"code"``.
        title: Display title for the parent document.
        chunk_size_tokens: Per-chunk token cap (default 512, R-14-3).
        overlap_tokens: Overlap between adjacent chunks within one section
            (default 64). Must be strictly less than ``chunk_size_tokens``.
        max_chunks: Stop after this many chunks; the rest of the text is not
            encoded at all. ``None`` for no limit.

    Returns:
        Ordered list of :class:`DocumentChunk` instances, indexed
        ``0..N-1``. Empty list for empty/whitespace-only input.

    Raises:
        ValueError: If ``chunk_size_tokens`` is non-positive or
            ``overlap_tokens`` is negative / not strictly less than
            ``chunk_size_tokens``.
    """
    chunks: list[DocumentChunk] = []
    if max_chunks is not None and max_chunks <= 0:
        _validate(chunk_size_tokens, overlap_tokens)
        return chunks
    for chunk, _ in iter_document_chunks(
        sections,
        conversation_id=conversation_id,
        doc_ref=doc_ref,
        document_format=document_format,
        title=title,
        chunk_size_tokens=chunk_size_tokens,
        overlap_tokens=overlap_tokens,
    ):
        chunks.append(chunk)
        # A generator: once ``max_chunks`` is reached the rest is never encoded.
        if max_chunks is not None and len(chunks) >= max_chunks:
            break
    return chunks


def iter_document_chunks(
    sections: Sequence[DocumentSection],
    *,
    conversation_id: str,
    doc_ref: str,
    document_format: str,
    title: str,
    start: ChunkCursor = ChunkCursor(),  # noqa: B008 - frozen, so one shared default is safe
    chunk_size_tokens: int = DEFAULT_CHUNK_SIZE_TOKENS,
    overlap_tokens: int = DEFAULT_OVERLAP_TOKENS,
) -> Iterator[tuple[DocumentChunk, ChunkCursor]]:
    """Yield each chunk with the cursor that resumes just after it.

    The document index (D-FV0-31) embeds a document a slice at a time: it takes some
    chunks, stores the last cursor, and later passes it back as ``start``. Nothing
    before ``start`` is encoded again.

    Raises:
        ValueError: As :func:`chunk_document`.
    """
    _validate(chunk_size_tokens, overlap_tokens)
    created_at = datetime.now(UTC)
    index = start.index
    for number in range(start.section, len(sections)):
        section = sections[number]
        cleaned = section.text.strip()
        if not cleaned:
            continue
        offset = start.offset if number == start.section else 0
        for piece, next_offset in _split_to_token_cap(
            cleaned,
            start=offset,
            chunk_size_tokens=chunk_size_tokens,
            overlap_tokens=overlap_tokens,
        ):
            chunk = DocumentChunk(
                id=make_document_chunk_id(conversation_id, doc_ref, index),
                text=piece,
                doc_ref=doc_ref,
                format=document_format,
                title=title,
                page=section.page,
                section=section.section,
                sheet=section.sheet,
                created_at=created_at,
            )
            index += 1
            if next_offset < len(cleaned):
                yield chunk, ChunkCursor(number, next_offset, index)
            else:
                yield chunk, ChunkCursor(number + 1, 0, index)


def _validate(chunk_size_tokens: int, overlap_tokens: int) -> None:
    if chunk_size_tokens <= 0:
        msg = f"chunk_size_tokens must be positive; got {chunk_size_tokens}"
        raise ValueError(msg)
    if overlap_tokens < 0:
        msg = f"overlap_tokens must be non-negative; got {overlap_tokens}"
        raise ValueError(msg)
    if overlap_tokens >= chunk_size_tokens:
        msg = (
            f"overlap_tokens ({overlap_tokens}) must be strictly less than "
            f"chunk_size_tokens ({chunk_size_tokens})"
        )
        raise ValueError(msg)


#: The largest segment encoded at once while looking for a full window. A
#: segment starts at :data:`persona.skills._tokens.SEGMENT_CHARS` and doubles
#: while it holds no more than one window's tokens, which only long whitespace
#: runs need (about 128 characters per token). tiktoken fails at about
#: 1,000,000 characters of whitespace; this is a quarter of that.
_MAX_SEGMENT_CHARS: int = 262_144

#: Separators a window may end at, best first.
_SEPARATORS: tuple[str, ...] = ("\n\n", "\n", " ")


def _split_to_token_cap(
    text: str,
    *,
    start: int = 0,
    chunk_size_tokens: int,
    overlap_tokens: int,
) -> Iterator[tuple[str, int]]:
    """Yield ``text`` from ``start`` as chunks of at most ``chunk_size_tokens`` tokens.

    Each chunk comes with the offset its successor starts at (``len(text)`` after the
    last). Chunks are stripped and never empty. Text that fits is one chunk, found by a
    count that stops past the cap. Longer text is cut into token windows (see the module
    docstring), each ending at the best separator in its last quarter and each, counted
    on its own, within the cap.

    Linear: the text is encoded segment by segment from the current window's start;
    a segment is re-encoded only from the window that crosses its end.
    """
    if start == 0 and count_tokens_capped(text, chunk_size_tokens) <= chunk_size_tokens:
        yield text, len(text)
        return
    cap = chunk_size_tokens
    pos = start
    while pos < len(text):
        span = _tokens().SEGMENT_CHARS
        while True:
            end = _tokens().safe_cut(text, pos, pos + span)
            tokens = _encode(text[pos:end])
            if len(tokens) > cap or end == len(text) or span >= _MAX_SEGMENT_CHARS:
                break
            span *= 2
        starts = [pos + offset for offset in _token_starts(tokens)]
        starts.append(end)  # a sentinel: the end of the last token
        count = len(tokens)
        i = 0
        while True:
            if i + cap < count:
                k = _window_end(text, starts, i, i + cap, cap)
            elif end == len(text):
                k = count  # the last window runs to the end of the text
            elif i == 0:
                # The longest segment still holds no more than one window.
                yield from _non_empty(text[pos:end], end)
                pos = end
                break
            else:
                pos = starts[i]  # re-encode from this window's start
                break
            chunk = text[starts[i] : starts[k]]
            while k > i + 1 and count_tokens(chunk.strip()) > cap:
                # Counted on its own, a window can tokenise longer at its edges.
                k -= 1
                chunk = text[starts[i] : starts[k]]
            if k == count:
                yield from _non_empty(chunk, len(text))
                return
            following = _next_start(text, starts, i, k, overlap_tokens)
            yield from _non_empty(chunk, starts[following])
            i = following


def _non_empty(chunk: str, following: int) -> Iterator[tuple[str, int]]:
    stripped = chunk.strip()
    if stripped:
        yield stripped, following


def _window_end(text: str, starts: list[int], i: int, j: int, cap: int) -> int:
    """The token index to end the window ``[i, j)`` at: the best separator in its last quarter.

    A cut before token ``t`` is at a separator when the text just before
    ``starts[t]`` ends with it, or the token itself starts with it. With no
    separator in the last quarter, the window ends at ``j``.
    """
    floor = i + (cap * 3) // 4
    for separator in _SEPARATORS:
        for t in range(j, floor, -1):
            at = starts[t]
            before = at - len(separator)  # a negative start would count from the end
            at_separator = text.startswith(separator, at) or (
                before >= 0 and text.startswith(separator, before)
            )
            if at_separator and at > starts[i]:
                return t
    return j


def _next_start(text: str, starts: list[int], i: int, k: int, overlap_tokens: int) -> int:
    """The next window's first token: ``overlap_tokens`` before ``k``, moved to a word start.

    Moves forward, by at most half the overlap, to the first token that starts a
    word (it begins with a space or follows whitespace). Always after ``i`` in the
    text, so every window makes progress.
    """
    first = max(k - overlap_tokens, i + 1)
    for t in range(first, max(first, k - overlap_tokens // 2) + 1):
        at = starts[t]
        if at > starts[i] and (text.startswith(" ", at) or text[at - 1].isspace()):
            return t
    while first < k and starts[first] <= starts[i]:
        first += 1
    return first
