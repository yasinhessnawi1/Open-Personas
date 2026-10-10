"""Document upload + lifecycle service (spec 14 T13).

Composes the per-format parsers (T11 dispatcher) + the size-aware ingest
strategy (T12) + the conversation-scoped :class:`DocumentStore` (T03) +
the persona workspace (Spec 03 sandbox-path resolver) into a clean
upload-on-the-API-boundary contract.

**Workspace layout** (v0.1 — workspace-only, no DB migration):

::

    {sandbox_root}/{owner_id}/{persona_id}/conversations/{conversation_id}/documents/
        {doc_ref}.{ext}           original bytes
        {doc_ref}.{ext}.meta.json DocumentRef sidecar (strategy, token_count,
                                  format, title, page_count, sheet_names, ...)
        {doc_ref}.{ext}.text.json the extracted text, as the parse worker returned it
                                  at upload (D-FV0-31): every document with text
        {doc_ref}.{ext}.index.json a retrieval document's index progress, written by the
                                  background index job (D-FV0-31): state, chunks
                                  stored, and where the job resumes
        {doc_ref}.page-NNNN.png   a scanned PDF's rendered pages

Every file of a document is found by its exact name, built from the document's
``.meta.json`` (``workspace_path``) or matched by an exact pattern, never by a
``{doc_ref}.`` prefix: another upload's name can start with that prefix (third
security review, finding 8). New doc refs contain no dot.

Why workspace + sidecar (not a DB column at v0.1): adding a
``conversations.documents JSONB`` column is a separate Alembic migration
(equivalent to Spec 13's D-13-X-now ``messages.images``). T13 does not
own that migration; the workspace + sidecar pattern lets T14/T15/T16 read
attached documents without a schema change, and a v0.2 refactor can
promote the sidecar to a DB column when the API surface grows. The
runtime impact is one ``os.listdir`` per turn (bounded by a small
document count per conversation — typically 0–10).

**Public surface:**

- :func:`upload` — validate format/size, store original bytes, parse +
  ingest, write the sidecar, return the :class:`DocumentRef`.
- :func:`list_for_conversation` — enumerate attached documents for the
  T14/T15/T16 prompt-builder extensions + the GET endpoint (T18).
- :func:`get_document_text` — read full text of a small whole-inject
  document for T14's prompt injection.
- :func:`remove_document` — per-document delete (T18 DELETE endpoint).
  Removes the workspace files (original + sidecar) AND walks the
  :class:`DocumentStore` to delete the doc's chunks (retrieval path).
- :func:`remove_all_for_conversation` — **the cascade-delete helper
  T19 reuses** when a conversation is deleted (co-landing with Spec 13's
  T12 in one DELETE-handler refactor per D-14-X-cascade-coordination).

**CSA-2 (cross-spec upload-route extension):** the route layer (T17,
gated on Spec 13's T11) dispatches by content-type to either
:func:`upload` (this module) or :func:`persona_api.services.image_service.upload`
(Spec 13's T10, not yet shipped). The signatures differ naturally —
documents are conversation-scoped, images are persona-scoped per Spec 13
§7 — but the dispatcher boundary is uniform.

**Scanned PDFs (T21, Spec 13's PDF contract):** when
:class:`~persona.documents.ingest.IngestStrategy.VISION_HANDOFF_REQUIRED`
fires (``parse_result.needs_vision_handoff=True``, so there is no text to
extract), this service rasterises the pages, persists each page PNG through
the storage backend, and returns a :class:`DocumentRef` whose ``images``
carry them as :class:`~persona.schema.content.ImageContent` references with
``strategy=VISION_HANDOFF``. The upload succeeds; the conversation message
carries the page images so the router sends the turn to a vision-capable
tier. The interim exception this path used to raise no longer exists.
"""

from __future__ import annotations

import asyncio
import json
import re
import tempfile
import unicodedata
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from persona.documents.chunker import DocumentSection
from persona.documents.errors import CorruptDocumentError
from persona.documents.ingest import IngestStrategy, decide_strategy
from persona.documents.parsers import (
    FLAG_PARTIAL_EXTRACTION,
    FLAG_ROW_CAP_TRUNCATED,
    SUPPORTED_EXTENSIONS,
)
from persona.errors import PersonaError
from persona.logging import get_logger
from persona.schema.content import ImageContent
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from persona_api.document_worker.untrusted import MAX_PAGE_IMAGE_BYTES
from persona_api.services.artifact_metadata import (
    DOCUMENT_INDEX_SIDECAR_SUFFIX,
    DOCUMENT_TEXT_SIDECAR_SUFFIX,
    SIDECAR_SUFFIX,
    SPEC_14_SIDECAR_SUFFIX,
    WorkspaceArtifactMetadata,
    utcnow,
)
from persona_api.services.artifact_storage import write_sidecar
from persona_api.services.document_parsing import DocumentParser, ParsedDocument
from persona_api.services.image_service import MAX_UPLOAD_BYTES

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from persona.stores.document_store import DocumentStore
    from persona_runtime.prompt import DocumentContext

    from persona_api.services.document_index import DocumentIndexScheduler
    from persona_api.storage import FileStorage

__all__ = [
    "DOCUMENT_DIR_NAME",
    "MAX_DOCUMENT_BYTES",
    "DocumentRef",
    "DocumentTooLargeError",
    "DocumentUnreadableError",
    "validate_upload",
    "build_document_context",
    "get_document_text",
    "list_for_conversation",
    "remove_all_for_conversation",
    "remove_document",
    "upload",
]

_log = get_logger("api.documents")

#: Workspace sub-directory holding a conversation's attached documents.
DOCUMENT_DIR_NAME: str = "documents"

#: The largest document an upload may carry (D-FV0-08). The same ceiling an uploaded image
#: and a task attachment get, and the one the web composer already enforces before sending
#: (``packages/web/src/lib/api/limits.ts``). One cap, one number to change.
MAX_DOCUMENT_BYTES: int = MAX_UPLOAD_BYTES


class DocumentTooLargeError(PersonaError):
    """An uploaded document is larger than :data:`MAX_DOCUMENT_BYTES`.

    Raised before anything is parsed or stored. ``context`` carries ``size_bytes`` and
    ``max_bytes`` (decimal strings).
    """


class DocumentUnreadableError(PersonaError):
    """A stored whole-inject document's text could not be read for this turn.

    ``context`` carries ``doc_ref`` and ``reason``: ``original_missing``, or the parse
    failure (``parse_limit``, ``busy``, ``corrupt``, ...). The turn says so in plain
    words instead of silently leaving the document out (D-FV0-31).
    """


#: What a turn's document context says, in the document's place, when its text could
#: not be read this turn.
UNREADABLE_DOCUMENT_SENTENCE: str = (
    "The attached document {title} could not be read this turn, so its text is not included here."
)

#: Version of the extracted-text sidecar's JSON shape. Version 2 keeps the parser's
#: sections (text plus page, section and sheet), which the index needs; version 1 kept
#: only the joined text and is still read.
_TEXT_SIDECAR_VERSION: int = 2


#: Characters a filename-derived doc_ref slug may keep: no ``::`` delimiter
#: (make_document_chunk_id) and, since the third security review (finding 8), no dot,
#: so no doc ref followed by a dot is the start of another's name.
_DOC_REF_SAFE_RE = re.compile(r"[^A-Za-z0-9_-]+")

#: The longest doc ref :func:`is_doc_ref_shape` accepts. No stored document has a longer
#: one: an object key is at most 1,024 bytes and a local file name at most 255, and a
#: document's names are its ref plus an extension. Uploads make ``{slug}-{8 hex}`` from
#: the file name's stem, generated files ``{slug of at most 60}-{8 hex}``.
MAX_DOC_REF_CHARS: int = 1_024

_DOC_REF_SLUG_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
_DOC_REF_HEX_CHARS = frozenset("0123456789abcdef")


def is_doc_ref_shape(doc_ref: str) -> bool:
    """Whether ``doc_ref`` has the shape of every doc ref ever made: a slug, ``-``, 8 hex.

    The slug is any run of ASCII letters, digits, ``.``, ``_`` and ``-``. Exactly the
    language of ``[A-Za-z0-9._-]*-[0-9a-f]{8}`` (fullmatch), up to
    :data:`MAX_DOC_REF_CHARS`, but checked in one linear pass: that regular expression
    backtracks polynomially on a run of dashes, because its starred class also holds the
    ``-`` that follows it (CodeQL, polynomial regular expression on uncontrolled data).
    """
    if not 9 <= len(doc_ref) <= MAX_DOC_REF_CHARS or doc_ref[-9] != "-":
        return False
    return _DOC_REF_HEX_CHARS.issuperset(doc_ref[-8:]) and _DOC_REF_SLUG_CHARS.issuperset(
        doc_ref[:-9]
    )


#: A document's original is ``{doc_ref}`` plus one extension of letters and digits.
_EXTENSION_PATTERN = r"\.[A-Za-z0-9]+"

#: How far a document's index has got. ``not_needed`` for a document that is not
#: searched by chunks (whole-inject, scanned); ``pending`` and ``indexing`` while its
#: chunks are being written; ``ready`` when all are; ``partial`` when the document was
#: longer than the per-document chunk limit; ``failed`` when indexing gave up.
IndexState = Literal["not_needed", "pending", "indexing", "ready", "partial", "failed"]


class DocumentRef(BaseModel):
    """Reference to an attached document — the API-boundary type.

    Persisted alongside the original file as a ``{doc_ref}.meta.json``
    sidecar in the workspace. Returned by :func:`upload`,
    :func:`list_for_conversation`, and (via JSON) the API GET endpoint
    (T18). Carries the metadata T14/T15/T16 need to render the prompt
    sections + the synopsis.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    doc_ref: str
    """Stable identifier within the conversation; URL-safe, no ``::``."""

    filename: str
    """The uploaded filename (control-char-stripped for safety)."""

    title: str
    """Display title — defaults to the filename; future v0.2 may allow
    a user-supplied override. Always clean (:func:`clean_document_title`), on store and
    on read: it is written into the prompt."""

    @field_validator("title")
    @classmethod
    def _title_is_clean(cls, value: str) -> str:
        return clean_document_title(value)

    format: str
    """One of ``"pdf"`` / ``"docx"`` / ``"xlsx"`` / ``"csv"`` /
    ``"txt"`` / ``"md"`` / ``"code"``."""

    workspace_path: str
    """Path under ``sandbox_root`` (e.g.
    ``<owner>/persona_X/conversations/conv_Y/documents/doc.pdf``)."""

    strategy: IngestStrategy
    """The ingestion path taken. ``WHOLE_INJECT`` means T14 reads the
    full text; ``RETRIEVAL`` means T15 queries the
    :class:`DocumentStore`; ``VISION_HANDOFF`` means the pages were
    rasterised and are carried on :attr:`images`. A persisted ref never
    reads ``VISION_HANDOFF_REQUIRED``: :func:`upload` completes the handoff
    and records the outcome."""

    token_count: int = Field(ge=0)
    """cl100k_base estimate of the full document text."""

    page_count: int | None = None
    """For PDF/docx — total pages."""

    sheet_names: tuple[str, ...] | None = None
    """For xlsx — sheet names in workbook order."""

    size_bytes: int | None = None
    """Original file size."""

    images: tuple[ImageContent, ...] = ()
    """T21 — scanned-PDF rasterised pages as
    :class:`persona.schema.content.ImageContent` references. Empty for
    text-extracted documents; populated when ``strategy ==
    VISION_HANDOFF`` (a scanned PDF whose pages were rasterised and
    routed through Spec 13's vision-capable backends per D-13-X-pdf-contract).
    The conversation message that references this document carries these
    images in its ``content: list[MessageContent]`` so the router's
    vision pre-filter (Spec 13 T09) routes the turn to a vision-capable
    tier."""

    text_stored: bool = False
    """``True`` when the upload stored the extracted text beside the document (a
    ``.text.json`` sidecar, D-FV0-31). Every upload with text stores it. A turn reads
    that text; if it is missing, the document is unreadable this turn, never re-parsed.
    ``False`` for a document stored before the sidecar existed: only those are
    re-parsed, and the first re-parse that succeeds stores the text, so it happens once
    (third security review, finding 12)."""

    text_partial: bool = False
    """``True`` when the parse kept only part of the document's text: the four-million
    character budget cut it, a spreadsheet passed its row cap, or a part could not be read
    (third security review, finding 3). The turn says so, and so does the web chip."""

    index_state: IndexState
    """How far the document's chunk index has got (:data:`IndexState`). A ``.meta.json``
    written before this field existed reads ``ready`` for a retrieval document (it was
    indexed during its upload) and ``not_needed`` for any other."""

    chunks_indexed: int | None = Field(default=None, ge=0)
    """How many chunks of the document are in the store. ``None`` when not recorded
    (a document stored before this field existed)."""

    @model_validator(mode="before")
    @classmethod
    def _index_state_of_an_older_document(cls, data: Any) -> Any:  # noqa: ANN401
        """An older ``.meta.json`` has no index state; derive the one it really has.

        An uploaded retrieval document was indexed during its upload: ``ready``. A file
        ``file_extract`` generated was never indexed at all, though it is a retrieval
        document: ``pending``, so the index job picks it up. Such a file is named after
        its own doc ref (``filename`` is the stored name), which an upload's never is: an
        upload keeps its own name and gets a doc ref with a random ending.
        """
        if isinstance(data, dict) and "index_state" not in data:
            retrieval = data.get("strategy") in (IngestStrategy.RETRIEVAL, "retrieval")
            stored_name = str(data.get("workspace_path", "")).rsplit("/", 1)[-1]
            generated = retrieval and data.get("filename") == stored_name
            state = "pending" if generated else "ready" if retrieval else "not_needed"
            data = {**data, "index_state": state}
        return data


#: Parse flags that mean only part of the document's text was kept.
_PARTIAL_FLAGS = frozenset({FLAG_PARTIAL_EXTRACTION, FLAG_ROW_CAP_TRUNCATED})


def _parts(count: int) -> str:
    return "1 part" if count == 1 else f"{count} parts"


#: The longest a document title may be, in characters.
MAX_DOCUMENT_TITLE_CHARS: int = 120

#: Characters a title never keeps (fourth security review finding 6): controls (with
#: U+0085, the C1 next-line), format characters, and the line and paragraph separators;
#: and (fifth review) lone surrogates, private-use and unassigned code points, which a
#: title has no use for and a lone surrogate cannot even be encoded.
#: A title is written into the turn's synopsis and notes, so a name holding a line break
#: could otherwise start a line of its own there.
_TITLE_DROPPED_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp"})


def clean_document_title(name: str) -> str:
    """``name`` as a document title: one line, no control or format characters, capped.

    Every dropped character becomes a space, runs of whitespace become one space, and the
    result is cut to :data:`MAX_DOCUMENT_TITLE_CHARS`. An empty result is ``document``.
    """
    kept = "".join(" " if unicodedata.category(c) in _TITLE_DROPPED_CATEGORIES else c for c in name)
    collapsed = " ".join(kept.split())
    return collapsed[:MAX_DOCUMENT_TITLE_CHARS].rstrip() or "document"


def quoted_title(title: str) -> str:
    """A title as a turn quotes it: clean, in double quotes, inner quotes escaped."""
    return json.dumps(clean_document_title(title), ensure_ascii=False)


def document_note(ref: DocumentRef) -> str | None:
    """What a turn must say about how much of ``ref`` the model can use, or ``None``.

    One short sentence per condition (D-FV0-31, third security review findings 3 and 4):
    the index state of a retrieval document, then a cut text. A scanned document has no
    text, so its one sentence says that and nothing else. A whole document that is ready
    gets none. The title is quoted (fourth security review finding 6).
    """
    title = quoted_title(ref.title)
    if ref.strategy == IngestStrategy.VISION_HANDOFF:
        return f"{title} is a scanned document whose pages are images you cannot see here."
    sentences: list[str] = []
    if ref.strategy == IngestStrategy.RETRIEVAL:
        done = ref.chunks_indexed or 0
        if ref.index_state == "pending" or (ref.index_state == "indexing" and done == 0):
            sentences.append(
                f"{title} is still waiting to be indexed, so none of its text can be searched yet."
            )
        elif ref.index_state == "indexing":
            sentences.append(
                f"{title} is still being indexed, so only its first {_parts(done)} can be "
                "searched this turn."
            )
        elif ref.index_state == "partial":
            verb = "was" if done == 1 else "were"
            sentences.append(
                f"Only the first {_parts(done)} of {title} {verb} indexed, because it is "
                "longer than a document may be."
            )
        elif ref.index_state == "failed":
            sentences.append(f"{title} could not be indexed, so its text cannot be searched.")
    if ref.text_partial:
        sentences.append(
            f"Only part of {title} could be read, so the rest of its text is not available."
        )
    return " ".join(sentences) or None


def validate_upload(*, file_bytes: bytes, filename: str) -> str:
    """Refuse an upload before anything parses it; return its lower-cased extension.

    Raises:
        DocumentTooLargeError: ``file_bytes`` is larger than :data:`MAX_DOCUMENT_BYTES`.
        UnsupportedFormatError: Extension not in
            :data:`persona.documents.parsers.SUPPORTED_EXTENSIONS`.
    """
    if len(file_bytes) > MAX_DOCUMENT_BYTES:
        raise DocumentTooLargeError(
            "document exceeds the upload size limit",
            context={
                "size_bytes": str(len(file_bytes)),
                "max_bytes": str(MAX_DOCUMENT_BYTES),
            },
        )
    extension = Path(filename).suffix.lower()
    if extension not in SUPPORTED_EXTENSIONS:
        from persona.documents.errors import UnsupportedFormatError  # noqa: PLC0415

        raise UnsupportedFormatError(
            "unsupported document format",
            context={
                "format": extension or "unknown",
                "filename": _safe_filename(filename),
            },
        )
    return extension


def upload(
    *,
    file_storage: FileStorage,
    owner_id: str,
    persona_id: str,
    conversation_id: str,
    file_bytes: bytes,
    filename: str,
    parsed: ParsedDocument,
    admit_retrieval: Callable[[], None] | None = None,
) -> DocumentRef:
    """Store an upload that has already been parsed. Returns the reference.

    The parse itself happened elsewhere, through the :class:`DocumentParser` port
    (:func:`persona_api.services.document_parsing.parse_untrusted_document`), so this
    function never reads the untrusted bytes as a document. It decides how the text is
    used and persists the original, the extracted text, the page images of a scanned
    PDF and the sidecars, all through the storage backend, and only after the parse
    succeeded. A retrieval document is NOT indexed here: it is stored ``pending`` and
    the caller schedules the background index job (D-FV0-31, third security review:
    embedding inside the request had no bound).

    Args:
        file_storage: The storage backend.
        owner_id: The authenticated owner.
        persona_id: The persona the document is uploaded to.
        conversation_id: The conversation scope for the document attachment.
        file_bytes: The raw uploaded bytes (persisted as the original).
        filename: The uploaded filename (no path components).
        parsed: What the parse of these bytes returned.
        admit_retrieval: Called before anything is stored when the document will be
            indexed (retrieval); it raises to refuse the upload (the owner's index
            backlog is full, fourth review finding 1).

    Returns:
        :class:`DocumentRef` with the strategy and metadata. A scanned PDF returns like
        any other document: its pages are carried on ``images`` with
        ``strategy=VISION_HANDOFF`` (Spec 13's PDF contract).

    Raises:
        DocumentTooLargeError, UnsupportedFormatError: As :func:`validate_upload`.
    """
    extension = validate_upload(file_bytes=file_bytes, filename=filename)
    parse_result = parsed.parse_result

    doc_ref = _make_doc_ref(filename)
    key = (
        f"{owner_id}/{persona_id}/conversations/{conversation_id}"
        f"/{DOCUMENT_DIR_NAME}/{doc_ref}{extension}"
    )
    document_format = _format_for_extension(extension)
    title = clean_document_title(filename)
    ingest_result = decide_strategy(parse_result, doc_ref=doc_ref)
    if ingest_result.strategy == IngestStrategy.RETRIEVAL and admit_retrieval is not None:
        admit_retrieval()

    images: tuple[ImageContent, ...] = ()
    if ingest_result.strategy == IngestStrategy.VISION_HANDOFF_REQUIRED:
        # T21 vision handoff: persist the rendered pages through the backend and carry
        # them as ImageContent references (Spec 13 option (c): workspace_path only).
        images = _persist_page_images(
            file_storage=file_storage,
            base_key=_conversation_documents_prefix(owner_id, persona_id, conversation_id),
            doc_ref=doc_ref,
            page_images=parsed.page_images,
        )
        ingest_result = ingest_result.model_copy(update={"strategy": IngestStrategy.VISION_HANDOFF})

    stores_text = ingest_result.strategy in (IngestStrategy.WHOLE_INJECT, IngestStrategy.RETRIEVAL)
    if ingest_result.strategy == IngestStrategy.RETRIEVAL:
        index_state: IndexState = "pending"
        chunks_indexed: int | None = 0
    else:
        index_state, chunks_indexed = "not_needed", None
    ref = DocumentRef(
        doc_ref=doc_ref,
        filename=_safe_filename(filename),
        title=title,
        format=document_format,
        workspace_path=key,
        strategy=ingest_result.strategy,
        token_count=ingest_result.token_count,
        page_count=parse_result.page_count,
        sheet_names=parse_result.sheet_names,
        size_bytes=parse_result.size_bytes,
        images=images,
        text_stored=stores_text,
        text_partial=bool(_PARTIAL_FLAGS.intersection(parse_result.flags)),
        index_state=index_state,
        chunks_indexed=chunks_indexed,
    )

    # R5-D-4: durable writes through the backend (local byte-identical / S3 object),
    # AFTER a successful parse so a bad upload never persists.
    file_storage.put(key, file_bytes)
    if stores_text:
        # D-FV0-31: keep the text the worker extracted, so every later turn (and the
        # index) reads it instead of parsing the untrusted original again. Written
        # before the ``.meta.json`` that lists the document, so a listed document has
        # its text.
        _store_text(file_storage, key, parse_result.sections)
    # The ``.meta.json`` DocumentRef sidecar — a SIBLING object (list works on S3).
    file_storage.put(f"{key}{SPEC_14_SIDECAR_SUFFIX}", ref.model_dump_json().encode("utf-8"))
    # Spec 35: ALSO register the upload as a conversation ARTIFACT via the distinct
    # ``.f5.json`` sidecar so it surfaces in the unified Files viewer; routed as a
    # sibling object through the SAME backend.
    write_sidecar(
        file_storage,
        key,
        WorkspaceArtifactMetadata(
            source="upload",
            type="doc",
            producing_spec="14",
            conversation_id=conversation_id,
            created_at=utcnow(),
            original_name=_safe_filename(filename),
        ),
    )

    _log.info(
        "document uploaded persona={} conv={} ref={} strategy={} tokens={}",
        persona_id,
        conversation_id,
        doc_ref,
        ingest_result.strategy.value,
        ingest_result.token_count,
    )

    return ref


def list_for_conversation(
    *,
    file_storage: FileStorage,
    owner_id: str,
    persona_id: str,
    conversation_id: str,
) -> list[DocumentRef]:
    """List attached documents in workspace order.

    Returns an empty list if the conversation directory doesn't exist
    (no documents uploaded yet).
    """
    prefix = _conversation_documents_prefix(owner_id, persona_id, conversation_id)
    refs: list[DocumentRef] = []
    objects = sorted(file_storage.list(prefix), key=lambda o: o.key)
    keys = {obj.key for obj in objects}
    for obj in objects:
        if not obj.key.endswith(SPEC_14_SIDECAR_SUFFIX):
            continue
        try:
            ref = DocumentRef.model_validate_json(file_storage.get(obj.key).decode("utf-8"))
        except Exception:  # noqa: BLE001 - sidecar may be corrupt; skip
            _log.warning("skipping unreadable sidecar {}", obj.key)
            continue
        original = obj.key[: -len(SPEC_14_SIDECAR_SUFFIX)]
        if f"{original}{DOCUMENT_INDEX_SIDECAR_SUFFIX}" in keys:
            progress = read_index_progress(file_storage, original)
            if progress is not None:
                ref = ref.model_copy(
                    update={
                        "index_state": progress.state,
                        "chunks_indexed": progress.chunks_indexed,
                    }
                )
        refs.append(ref)
    return refs


class IndexProgress(BaseModel):
    """A retrieval document's index progress: the ``.index.json`` the index job writes.

    Kept apart from the ``.meta.json``, which only the upload writes: a job that wrote
    the ``.meta.json`` could race a delete and list a deleted document again. A stray
    ``.index.json`` lists nothing.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    version: int = 1
    state: IndexState
    chunks_indexed: int = Field(ge=0)
    section: int = Field(default=0, ge=0)
    offset: int = Field(default=0, ge=0)
    restarts: int = Field(default=0, ge=0)


def read_index_progress(file_storage: FileStorage, original_key: str) -> IndexProgress | None:
    """The index progress beside the original at ``original_key``, or ``None``."""
    try:
        return IndexProgress.model_validate_json(
            file_storage.get(f"{original_key}{DOCUMENT_INDEX_SIDECAR_SUFFIX}").decode("utf-8")
        )
    except Exception:  # noqa: BLE001 - absent or unreadable both mean "no progress recorded"
        return None


def write_index_progress(
    file_storage: FileStorage, original_key: str, progress: IndexProgress
) -> None:
    """Write the index progress beside the original at ``original_key``."""
    file_storage.put(
        f"{original_key}{DOCUMENT_INDEX_SIDECAR_SUFFIX}",
        progress.model_dump_json().encode("utf-8"),
        content_type="application/json",
    )


def find_document(
    *,
    file_storage: FileStorage,
    owner_id: str,
    persona_id: str,
    conversation_id: str,
    doc_ref: str,
) -> DocumentRef | None:
    """The document ``doc_ref`` names, read from its ``.meta.json`` by exact name.

    ``None`` when there is none (deleted, never stored, or a ref of another shape).
    """
    if not is_doc_ref_shape(doc_ref):
        return None
    prefix = _conversation_documents_prefix(owner_id, persona_id, conversation_id)
    meta = re.compile(re.escape(doc_ref) + _EXTENSION_PATTERN + re.escape(SPEC_14_SIDECAR_SUFFIX))
    for obj in file_storage.list(prefix):
        relative = obj.key[len(prefix) + 1 :] if obj.key.startswith(f"{prefix}/") else ""
        if not meta.fullmatch(relative):
            continue
        try:
            ref = DocumentRef.model_validate_json(file_storage.get(obj.key).decode("utf-8"))
        except Exception:  # noqa: BLE001 - an unreadable .meta.json lists nothing either
            return None
        return ref if ref.doc_ref == doc_ref and _original_name(ref) is not None else None
    return None


def document_original_key(
    ref: DocumentRef, *, owner_id: str, persona_id: str, conversation_id: str
) -> str | None:
    """The exact storage key of ``ref``'s original, or ``None`` if its name is not its own."""
    original = _original_name(ref)
    if original is None:
        return None
    prefix = _conversation_documents_prefix(owner_id, persona_id, conversation_id)
    return f"{prefix}/{original}"


def load_stored_sections(
    file_storage: FileStorage, original_key: str
) -> tuple[DocumentSection, ...] | None:
    """The stored text of the original at ``original_key``, as its sections.

    A version-1 sidecar (joined text only) reads as one section. ``None`` when there is
    no readable text sidecar.
    """
    try:
        payload = json.loads(
            file_storage.get(f"{original_key}{DOCUMENT_TEXT_SIDECAR_SUFFIX}").decode("utf-8")
        )
    except Exception:  # noqa: BLE001 - absent or unreadable both mean "no stored text"
        return None
    if isinstance(payload, dict) and isinstance(payload.get("sections"), list):
        try:
            return tuple(DocumentSection.model_validate(item) for item in payload["sections"])
        except ValueError:
            return None
    text = _stored_full_text(payload)
    return (DocumentSection(text=text),) if text is not None else None


def text_sidecar_bytes(sections: Sequence[DocumentSection]) -> bytes:
    """The extracted-text sidecar for ``sections`` (version 2), as stored."""
    payload = {
        "version": _TEXT_SIDECAR_VERSION,
        "sections": [
            section.model_dump(mode="json", exclude_none=True)
            for section in sections
            if section.text.strip()
        ],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


async def build_document_context(
    *,
    file_storage: FileStorage,
    owner_id: str,
    persona_id: str,
    conversation_id: str,
    user_message: str,
    document_store: DocumentStore | None,
    parser: DocumentParser,
    retrieval_top_k: int = 5,
    index_scheduler: DocumentIndexScheduler | None = None,
) -> DocumentContext:
    """Build the per-turn :class:`persona_runtime.prompt.DocumentContext`.

    F3 follow-up — closes the third Spec 14 integration gap (after
    ``build_document_store`` wiring and the ``memory_chunks`` RLS aux
    policy). The chat path needs to thread attached documents into
    the prompt-builder; this helper is the boundary that reads from
    the workspace sidecars + queries the DocumentStore and produces
    the typed runtime shape.

    Args:
        sandbox_root: Workspace root.
        persona_id: Persona scope.
        conversation_id: Conversation scope (documents are
            conversation-scoped per Spec 14 §6).
        user_message: This turn's user message (drives RETRIEVAL ranking).
        document_store: The conversation-scoped store (returned from
            ``app.state.build_document_store``). ``None`` skips the
            RETRIEVAL path — whole-inject docs still inject their full
            text since they're sidecar-backed.
        parser: The port an older whole-inject document (one without an
            extracted-text sidecar) is re-parsed through: the same worker pool, with
            the same bounds, that parsed the upload (D-FV0-31).
        retrieval_top_k: Per-doc cap on RETRIEVAL chunks. Defaults to 5;
            T22's D-14-X-prompt-bound-target asserts < 30 000 tokens
            even at high retrieval volume.
        index_scheduler: The document index's scheduler. Each turn hands it the
            documents still waiting to be indexed, so a document whose job was never
            scheduled (a request cancelled after its store, an older generated file)
            is picked up. ``None`` skips that.

    Returns:
        A :class:`DocumentContext` populated with:
          - ``whole_inject_docs``: every doc with
            ``strategy == WHOLE_INJECT`` (small enough for full-text
            injection per D-14-1's 3000-token threshold). A document whose text
            could not be read this turn carries :data:`UNREADABLE_DOCUMENT_SENTENCE`
            in its place, never a silent gap;
          - ``retrieved_chunks``: per-doc top-K query results for docs
            with ``strategy == RETRIEVAL``;
          - ``attached_documents``: the synopsis row for every attached
            doc regardless of strategy (T16 structural defence).
        Empty ``DocumentContext`` when no docs are attached.
    """
    # Lazy import — runtime is api's downstream, but the runtime types are
    # safe to import at call time (api already depends on persona_runtime).
    from persona_runtime.prompt import (  # noqa: PLC0415
        DocumentContext,
        DocumentDescriptor,
        DocumentInjection,
    )

    # Storage listing and retrieval are synchronous I/O and embedding; they run on a
    # thread so the event loop keeps serving while a turn is prepared.
    refs = await asyncio.to_thread(
        list_for_conversation,
        file_storage=file_storage,
        owner_id=owner_id,
        persona_id=persona_id,
        conversation_id=conversation_id,
    )
    if not refs:
        return DocumentContext()
    if index_scheduler is not None:
        await index_scheduler.pick_up(
            owner_id=owner_id, persona_id=persona_id, conversation_id=conversation_id, refs=refs
        )

    whole_inject: list[DocumentInjection] = []
    retrieved: list[Any] = []
    descriptors: list[DocumentDescriptor] = []

    for ref in refs:
        descriptors.append(
            DocumentDescriptor(
                title=ref.title,
                format=ref.format,
                page_count=ref.page_count,
                sheet_names=ref.sheet_names,
                size_bytes=ref.size_bytes,
                note=document_note(ref),
            )
        )
        if ref.strategy == IngestStrategy.WHOLE_INJECT:
            try:
                text = await get_document_text(
                    file_storage=file_storage,
                    owner_id=owner_id,
                    persona_id=persona_id,
                    conversation_id=conversation_id,
                    ref=ref,
                    parser=parser,
                )
            except DocumentUnreadableError as exc:
                # Never a silent gap: the turn says the document could not be read, and
                # the log says why.
                _log.warning(
                    "document text unavailable this turn ref={} reason={}",
                    ref.doc_ref,
                    exc.context.get("reason", "unknown"),
                )
                text = UNREADABLE_DOCUMENT_SENTENCE.format(title=quoted_title(ref.title))
            if text:
                whole_inject.append(
                    DocumentInjection(
                        title=ref.title,
                        format=ref.format,
                        full_text=text,
                    )
                )
        elif ref.strategy == IngestStrategy.RETRIEVAL and document_store is not None:
            # Per-doc retrieval scoped by metadata.doc_ref so each doc's
            # chunks compete only within their own document (avoids one
            # noisy long doc swamping the top-K).
            try:
                chunks = await asyncio.to_thread(
                    document_store.query,
                    conversation_id,
                    user_message,
                    top_k=retrieval_top_k,
                    doc_ref=ref.doc_ref,
                )
            except Exception:  # noqa: BLE001 — RETRIEVAL is best-effort
                _log.warning(
                    "document retrieval failed for ref {} — skipping",
                    ref.doc_ref,
                )
                continue
            retrieved.extend(chunks)

    return DocumentContext(
        whole_inject_docs=tuple(whole_inject),
        retrieved_chunks=tuple(retrieved),
        attached_documents=tuple(descriptors),
    )


async def get_document_text(
    *,
    file_storage: FileStorage,
    owner_id: str,
    persona_id: str,
    conversation_id: str,
    ref: DocumentRef,
    parser: DocumentParser,
) -> str:
    """Read the full text for a small whole-inject document.

    Used by T14's :class:`PromptBuilder` extension for the small-doc whole-injection
    section. Reads the extracted-text sidecar the upload wrote (D-FV0-31). An older
    document has no sidecar; its stored original is parsed again through ``parser``,
    the same worker pool and bounds an upload gets, as a re-parse: it waits for a busy
    pool only briefly and is exempt from the one-upload-per-user limit. The first
    re-parse that succeeds stores the text, so later turns read it (finding 12).

    Args:
        file_storage: The storage backend.
        owner_id: Owner scope.
        persona_id: Persona scope.
        conversation_id: Conversation scope.
        ref: The document, as its ``.meta.json`` lists it. Its files are found by
            exact name from it. When its :attr:`DocumentRef.text_stored` is set, a
            missing or unreadable sidecar makes the document unreadable this turn; the
            untrusted original is never parsed again for it (F3).
        parser: The document parser port.

    Returns:
        The full extracted text (empty when the document has none).

    Raises:
        DocumentUnreadableError: The original is missing, or the re-parse failed or was
            refused. The caller says so in the turn rather than dropping the document.
    """
    doc_ref = ref.doc_ref
    prefix = _conversation_documents_prefix(owner_id, persona_id, conversation_id)
    stored, original = await asyncio.to_thread(
        _load_document_text, file_storage=file_storage, prefix=prefix, ref=ref
    )
    if stored is not None:
        return stored
    if ref.text_stored:
        msg = "the document's stored text is missing"
        raise DocumentUnreadableError(
            msg, context={"doc_ref": doc_ref, "reason": "stored_text_missing"}
        )
    if original is None:
        msg = "the document's original file is missing"
        raise DocumentUnreadableError(
            msg, context={"doc_ref": doc_ref, "reason": "original_missing"}
        )
    name, data = original
    with tempfile.TemporaryDirectory(prefix="persona-doc-") as scratch:
        path = Path(scratch) / f"document{Path(name).suffix.lower()}"
        await asyncio.to_thread(path.write_bytes, data)
        try:
            parsed = await parser.parse(path, render_dir=None, owner_id=owner_id, purpose="reparse")
        except Exception as exc:  # noqa: BLE001 (every failure becomes a stated gap)
            reason = (exc.context.get("reason") if isinstance(exc, PersonaError) else None) or type(
                exc
            ).__name__
            msg = "the document could not be parsed this turn"
            raise DocumentUnreadableError(
                msg, context={"doc_ref": doc_ref, "reason": reason}
            ) from exc
    await asyncio.to_thread(
        _backfill_text,
        file_storage=file_storage,
        original_key=f"{prefix}/{name}",
        sections=parsed.parse_result.sections,
    )
    return parsed.parse_result.full_text


def _store_text(
    file_storage: FileStorage, original_key: str, sections: Sequence[DocumentSection]
) -> None:
    """Write the extracted-text sidecar beside the original at ``original_key``."""
    file_storage.put(
        f"{original_key}{DOCUMENT_TEXT_SIDECAR_SUFFIX}",
        text_sidecar_bytes(sections),
        content_type="application/json",
    )


def _backfill_text(
    *, file_storage: FileStorage, original_key: str, sections: Sequence[DocumentSection]
) -> None:
    """Store an older document's text after its first successful re-parse (finding 12).

    Only the text sidecar is written; the ``.meta.json`` is left as it is, so a delete
    racing this never finds its document listed again. If the ``.meta.json`` is gone
    once the text is written, the document was deleted meanwhile and the text is
    removed again. A failure here costs only the saving: the turn already has its text.
    """
    try:
        _store_text(file_storage, original_key, sections)
        if not file_storage.exists(f"{original_key}{SPEC_14_SIDECAR_SUFFIX}"):
            file_storage.delete(f"{original_key}{DOCUMENT_TEXT_SIDECAR_SUFFIX}")
    except Exception:  # noqa: BLE001 - the turn has its text; only the saving failed
        _log.warning("could not store the re-parsed text of {}", original_key)


def _stored_full_text(payload: object) -> str | None:
    """The joined text of a text sidecar's payload; ``None`` when it is not one.

    Version 2 holds sections, joined as :attr:`ParseResult.full_text` joins them;
    version 1 held the joined text itself.
    """
    if not isinstance(payload, dict):
        return None
    sections = payload.get("sections")
    if isinstance(sections, list):
        texts: list[str] = []
        for section in sections:
            text = section.get("text") if isinstance(section, dict) else None
            if not isinstance(text, str):
                return None
            if text.strip():
                texts.append(text)
        return "\n\n".join(texts)
    text = payload.get("full_text")
    return text if isinstance(text, str) else None


def _original_name(ref: DocumentRef) -> str | None:
    """The exact base name of ``ref``'s original: the last part of its ``workspace_path``.

    ``None`` unless it is ``{doc_ref}`` plus one extension, so a ``.meta.json`` can
    never point a read or a delete at another document's file.
    """
    name = ref.workspace_path.rsplit("/", 1)[-1]
    if re.fullmatch(re.escape(ref.doc_ref) + _EXTENSION_PATTERN, name):
        return name
    return None


def _document_file_names(doc_ref: str) -> re.Pattern[str]:
    """Every name a document's files can have, exactly, and no other document's.

    The original ``{doc_ref}.<ext>``, its ``.meta.json``, ``.f5.json``, ``.text.json`` and
    ``.index.json`` sidecars, and its ``{doc_ref}.page-NNNN.png`` pages (finding 9: a name
    that merely contains ``.page-`` is not a page).

    For a ``doc_ref`` of :func:`is_doc_ref_shape`, no other document's name matches.
    Another document's ref ``d`` also ends in ``-`` and eight hex digits. If ``d``
    is longer and starts with ``{doc_ref}.``, what follows that dot in its names holds
    the ``-`` of ``d``'s ending, which an extension, a sidecar suffix and
    ``page-NNNN.png`` cannot. If ``d`` is shorter, ``doc_ref`` would have to end inside
    ``d``'s extension, sidecar suffix or page name, none of which holds ``-`` and eight
    hex digits.
    """
    sidecars = "|".join(
        re.escape(suffix)
        for suffix in (
            SPEC_14_SIDECAR_SUFFIX,
            SIDECAR_SUFFIX,
            DOCUMENT_TEXT_SIDECAR_SUFFIX,
            DOCUMENT_INDEX_SIDECAR_SUFFIX,
        )
    )
    escaped = re.escape(doc_ref)
    return re.compile(rf"{escaped}{_EXTENSION_PATTERN}(?:{sidecars})?|{escaped}\.page-\d{{4}}\.png")


def _load_document_text(
    *,
    file_storage: FileStorage,
    prefix: str,
    ref: DocumentRef,
) -> tuple[str | None, tuple[str, bytes] | None]:
    """The stored extracted text for ``ref``, else its original's name and bytes.

    ``(text, None)`` when the text sidecar is there and readable; ``(None, (name,
    bytes))`` for an older document without one (or an unreadable one); ``(None,
    None)`` when the original is missing too.
    """
    original = _original_name(ref)
    names = {obj.key.rsplit("/", 1)[-1] for obj in file_storage.list(prefix)}
    if original is None or original not in names:
        return None, None
    if f"{original}{DOCUMENT_TEXT_SIDECAR_SUFFIX}" in names:
        try:
            payload = json.loads(
                file_storage.get(f"{prefix}/{original}{DOCUMENT_TEXT_SIDECAR_SUFFIX}").decode(
                    "utf-8"
                )
            )
        except (OSError, ValueError):
            payload = None
        text = _stored_full_text(payload)
        if text is not None:
            return text, None
        _log.warning("unreadable text sidecar for ref {}; parsing the original", ref.doc_ref)
    try:
        return None, (original, file_storage.get(f"{prefix}/{original}"))
    except OSError:
        _log.warning("get_document_text failed for ref {}", ref.doc_ref)
        return None, None


def read_document_bytes(
    *,
    file_storage: FileStorage,
    owner_id: str,
    persona_id: str,
    conversation_id: str,
    ref: DocumentRef,
) -> bytes | None:
    """Read the raw, original bytes for an attached document.

    Returns the ORIGINAL uploaded file's bytes (not the parsed text), so the
    document-workspace cascade can stage the actual file into the sandbox input
    mount where ``code_execution`` / a sandbox ``file_read`` can read it. The
    original is found by its exact name, from ``ref``'s ``workspace_path``.

    Args:
        file_storage: The storage backend.
        owner_id: Owner scope.
        persona_id: Persona scope.
        conversation_id: Conversation scope (documents are conversation-scoped).
        ref: The document, as its ``.meta.json`` lists it.

    Returns:
        The raw file bytes, or ``None`` when the document is missing/unreadable
        (the caller skips it — a partial staging beats a hard turn failure).
    """
    original = _original_name(ref)
    prefix = _conversation_documents_prefix(owner_id, persona_id, conversation_id)
    if original is None or not file_storage.exists(f"{prefix}/{original}"):
        return None
    try:
        # LocalFileStorage reads via the O_NOFOLLOW opener (symlink-swap safe →
        # OSError → skip); S3 returns the object bytes.
        return file_storage.get(f"{prefix}/{original}")
    except OSError:
        _log.warning("read_document_bytes failed for ref {}", ref.doc_ref)
        return None


def remove_document(
    *,
    file_storage: FileStorage,
    owner_id: str,
    persona_id: str,
    conversation_id: str,
    doc_ref: str,
    document_store: DocumentStore,
) -> None:
    """Remove a single document — workspace files + DocumentStore chunks.

    Idempotent: removing a non-existent ref is a no-op. Only this document's files go:
    each is matched by its exact name (:func:`_document_file_names`), never by a
    ``{doc_ref}.`` prefix, which another upload's name could share (finding 8).

    Args:
        file_storage: The storage backend.
        owner_id: Owner scope.
        persona_id: Persona scope.
        conversation_id: Conversation scope.
        doc_ref: Document reference to remove.
        document_store: The conversation-scoped store — its chunks for
            this ``doc_ref`` are removed via the 4-component
            chunk-ID prefix-match (D-14-X-document-chunk-id).
    """
    prefix = _conversation_documents_prefix(owner_id, persona_id, conversation_id)
    if not is_doc_ref_shape(doc_ref):
        # No document has this ref; and a ref of another shape could name another
        # document's file (``x.txt.meta`` names ``x.txt.meta.json``).
        document_store.delete_document(conversation_id, doc_ref)
        return
    names = _document_file_names(doc_ref)

    def _matching() -> list[str]:
        keys = []
        for obj in file_storage.list(prefix):
            relative = obj.key[len(prefix) + 1 :] if obj.key.startswith(f"{prefix}/") else ""
            if names.fullmatch(relative):
                keys.append(obj.key)
        return keys

    _delete_meta_first(file_storage, _matching())
    document_store.delete_document(conversation_id, doc_ref)
    # Fourth review finding 9: an index slice that wrote its ``.index.json`` after the
    # listing above, but checked for the ``.meta.json`` before it went, leaves that file
    # behind; a second pass, after the ``.meta.json`` is gone, takes it. A slice that
    # writes after that sees the document gone and removes its own file.
    for key in _matching():
        file_storage.delete(key)


def remove_all_for_conversation(
    *,
    file_storage: FileStorage,
    owner_id: str,
    persona_id: str,
    conversation_id: str,
    document_store: DocumentStore,
) -> None:
    """Cascade-delete helper — removes every document for a conversation.

    **T19 reuses this helper** when a conversation is deleted (per
    D-14-X-cascade-coordination — co-landing with Spec 13's T12 in one
    DELETE-handler refactor). Removes the entire
    ``persona_{id}/conversations/{conv}/documents/`` sub-tree from the
    workspace AND wipes the :class:`DocumentStore` chunks for the
    conversation.

    Idempotent: removing an empty / non-existent set is a no-op.
    """
    prefix = _conversation_documents_prefix(owner_id, persona_id, conversation_id)
    _delete_meta_first(file_storage, [obj.key for obj in file_storage.list(prefix)])
    document_store.delete(conversation_id)
    for obj in file_storage.list(prefix):  # the second pass, as in remove_document
        file_storage.delete(obj.key)


def _delete_meta_first(file_storage: FileStorage, keys: list[str]) -> None:
    """Delete ``keys``, every ``.meta.json`` first: a listed document is then gone at once.

    The index checks for the ``.meta.json`` after each slice (D-FV0-31), so removing it
    first is what tells a running slice the document is being deleted.
    """
    for key in sorted(keys, key=lambda k: not k.endswith(SPEC_14_SIDECAR_SUFFIX)):
        file_storage.delete(key)


def _persist_page_images(
    *,
    file_storage: FileStorage,
    base_key: str,
    doc_ref: str,
    page_images: tuple[Path, ...],
) -> tuple[ImageContent, ...]:
    """Persist rendered page PNGs as ``{doc_ref}.page-NNNN.png`` beside the document.

    The page number comes from the rendered file's name (``page-NNNN.png``), so a page
    the renderer skipped leaves a gap rather than renumbering the rest. Every page is
    read and checked (:func:`_read_page_image`) before any is stored, so a refused page
    leaves nothing behind.
    """
    pages = [(page.stem.rsplit("-", 1)[-1], _read_page_image(page)) for page in page_images]
    images: list[ImageContent] = []
    for number, data in pages:
        page_key = f"{base_key}/{doc_ref}.page-{number}.png"
        # R5-D-4: persist through the backend (local byte-identical / S3 object).
        file_storage.put(page_key, data, content_type="image/png")
        images.append(ImageContent(workspace_path=page_key, media_type="image/png"))
    return tuple(images)


#: The first bytes of every PNG file.
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def _read_page_image(page: Path) -> bytes:
    """A page image the worker wrote, read as the api distrusts it (F5, second review).

    The worker is untrusted once it has parsed a hostile file. Its page image is opened
    without following a link in any path component, must be a regular file with exactly
    one name (no hard link to a file the api's user can read), at most
    :data:`MAX_PAGE_IMAGE_BYTES` (checked on the open descriptor), and must start with
    the PNG signature.

    Raises:
        CorruptDocumentError: The page fails any of those checks.
    """
    from persona.sandbox._produced_io import read_produced_file  # noqa: PLC0415
    from persona.sandbox.errors import SandboxError  # noqa: PLC0415

    try:
        data = read_produced_file(
            page.parent, page.name, session_id="document-render", cap_bytes=MAX_PAGE_IMAGE_BYTES
        )
    except (SandboxError, OSError) as exc:
        msg = "a rendered page could not be read safely"
        raise CorruptDocumentError(
            msg, context={"format": "pdf", "reason": "parse_limit", "limit": "page_image"}
        ) from exc
    if not data.startswith(_PNG_SIGNATURE):
        msg = "a rendered page is not a PNG image"
        raise CorruptDocumentError(
            msg, context={"format": "pdf", "reason": "parse_limit", "limit": "page_image"}
        )
    return data


# ----- helpers (private) -----------------------------------------------------


def _conversation_documents_prefix(owner_id: str, persona_id: str, conversation_id: str) -> str:
    """The FileStorage key prefix for a conversation's documents (R5-D-4).

    Owner-scoped layout (matches image_service + the F5 artifacts walk:
    ``<owner_id>/<persona_id>/...``). This is the ``list`` prefix + the stem every
    document/sidecar/page key is built under. Backend-agnostic (the local backend
    still resolves it under the workspace root via ``resolve_sandbox_path``)."""
    return f"{owner_id}/{persona_id}/conversations/{conversation_id}/{DOCUMENT_DIR_NAME}"


def _make_doc_ref(filename: str) -> str:
    """Derive a stable, URL-safe doc_ref from the filename.

    Strategy: take the stem (no extension), strip unsafe chars, append a
    short uuid suffix for uniqueness within the conversation. The result
    is delimiter-free (no ``::``) per
    :func:`persona.schema.documents.make_document_chunk_id` validation.
    """
    stem = Path(filename).stem
    safe = _DOC_REF_SAFE_RE.sub("-", stem).strip("-") or "doc"
    suffix = uuid.uuid4().hex[:8]
    return f"{safe}-{suffix}"


def _safe_filename(filename: str) -> str:
    """Strip control chars + truncate filenames for audit-log + ref safety."""
    cleaned = "".join(c for c in filename if c == "\t" or ord(c) >= 32)
    return cleaned[:120]


def _format_for_extension(extension: str) -> str:
    """Map a file extension to the format string used in :class:`DocumentRef`."""
    code_extensions = {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".rs",
        ".go",
        ".java",
        ".kt",
        ".swift",
        ".rb",
        ".php",
        ".c",
        ".cc",
        ".cpp",
        ".h",
        ".hpp",
        ".cs",
        ".scala",
        ".sh",
        ".bash",
        ".zsh",
        ".sql",
        ".html",
        ".css",
        ".scss",
        ".yaml",
        ".yml",
        ".toml",
        ".json",
        ".xml",
    }
    if extension == ".pdf":
        return "pdf"
    if extension == ".docx":
        return "docx"
    if extension == ".xlsx":
        return "xlsx"
    if extension == ".csv":
        return "csv"
    if extension == ".md":
        return "md"
    if extension == ".txt":
        return "txt"
    if extension in code_extensions:
        return "code"
    return "unknown"
