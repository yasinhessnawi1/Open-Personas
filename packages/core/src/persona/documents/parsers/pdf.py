"""PDF parser — text-extraction path only (spec 14 T10).

Uses ``pypdf>=6.0,<7`` (D-14-2; R-14-1 lean: BSD-3, 0 transitives). Gated
behind the ``[documents]`` extra (D-14-X-documents-extra); lazy-imported
inside :func:`parse_pdf` so the package imports cleanly without the extra.

**This module implements the text-extraction path only.** The scanned-PDF
→ vision handoff (criterion #7) is T21's substance, sequenced LAST in
Phase 5 and gated on Spec 13's `ImageContent` + per-provider vision
serialisers + router pre-filter. At T10 (this task), the no-text-layer
heuristic is wired and the :attr:`ParseResult.needs_vision_handoff`
boolean is set so T21's vision-handoff wiring is a clean drop-in. Until
T21 lands, the document service (T13) reads ``needs_vision_handoff=True``
and returns a clean *"scanned PDF — vision support not yet wired in this
build"* error to the user — the Spec 13 fail-loud discipline applied at
Spec 14's interim state, not silently degraded into empty text.

No-text-layer detection (D-14-2):

The heuristic is *coverage* — extracted-text characters per page. The lean
value (conservative, tolerates extraction noise without false-positive
routing to expensive vision):

    extracted_chars / page_count < NO_TEXT_LAYER_THRESHOLD

with ``NO_TEXT_LAYER_THRESHOLD = 50``. A scanned page yields 0–5 chars
under ``pypdf``; a born-digital page rarely produces under 50 even on
sparse content. The threshold is overridable via
``PERSONA_DOC_PDF_NO_TEXT_LAYER_THRESHOLD`` for operators tuning the
sensitivity.

Fail-safe handling:

- Genuinely-corrupt PDFs (``PyPdfError`` / ``EmptyFileError``) →
  :class:`~persona.documents.errors.CorruptDocumentError` at the adapter
  boundary.
- Encrypted PDFs without a passphrase →
  :class:`~persona.documents.errors.CorruptDocumentError` with
  ``reason="encrypted"``.
- A successfully-opened PDF below the no-text-layer threshold becomes a
  ``ParseResult`` with empty/minimal sections, ``needs_vision_handoff=True``,
  and :data:`FLAG_NO_TEXT_LAYER` set. NO exception — the vision path
  (T21) takes over.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from persona.documents.chunker import DocumentSection
from persona.documents.errors import (
    CorruptDocumentError,
    MissingDependencyError,
)
from persona.documents.parsers import (
    FLAG_NO_TEXT_LAYER,
    FLAG_PARTIAL_EXTRACTION,
    MAX_DOCUMENT_TEXT_CHARS,
    ParseResult,
)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "DEFAULT_NO_TEXT_LAYER_THRESHOLD",
    "MAX_PDF_PAGES",
    "MAX_PDF_TEXT_PAGES",
    "NO_TEXT_LAYER_THRESHOLD_ENV_VAR",
    "parse_pdf",
    "resolve_no_text_layer_threshold",
]

#: The most pages a PDF may have at all; a larger one is refused before its pages are
#: read. A thousand-page manual is ordinary; ten thousand pages is not a document a
#: person attaches to a chat. Checked against the declared count first, then the real one.
MAX_PDF_PAGES: int = 10_000

#: The most pages text is extracted from. A PDF can point every page at one shared
#: content stream, so pages cost work, not bytes; past this the rest is skipped and the
#: result is flagged partial.
MAX_PDF_TEXT_PAGES: int = 2_000

#: Default no-text-layer detection threshold per D-14-2 (chars per page).
DEFAULT_NO_TEXT_LAYER_THRESHOLD: int = 50

#: Operator env override for the threshold.
NO_TEXT_LAYER_THRESHOLD_ENV_VAR: str = "PERSONA_DOC_PDF_NO_TEXT_LAYER_THRESHOLD"

#: Minimum sensible value (under this, every PDF would route to vision).
_THRESHOLD_MIN: int = 1
#: Maximum sensible value (above this, we'd false-positive on real text PDFs).
_THRESHOLD_MAX: int = 5000


def _import_pypdf() -> Any:  # noqa: ANN401
    """Lazy-import ``pypdf`` with a clear MissingDependencyError on failure."""
    try:
        import pypdf  # noqa: PLC0415 — deliberate lazy import
    except ImportError as exc:
        raise MissingDependencyError(
            "pypdf is not installed",
            context={
                "format": "pdf",
                "install_hint": "pip install persona-core[documents]",
            },
        ) from exc
    return pypdf


def resolve_no_text_layer_threshold() -> int:
    """Resolve the no-text-layer threshold from env override + clamp the range.

    Fail-safe — malformed/out-of-range env values fall back to the default.
    """
    raw = os.environ.get(NO_TEXT_LAYER_THRESHOLD_ENV_VAR)
    if not raw:
        return DEFAULT_NO_TEXT_LAYER_THRESHOLD
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_NO_TEXT_LAYER_THRESHOLD
    if parsed < _THRESHOLD_MIN or parsed > _THRESHOLD_MAX:
        return DEFAULT_NO_TEXT_LAYER_THRESHOLD
    return parsed


def parse_pdf(path: Path) -> ParseResult:
    """Parse a ``.pdf`` file via text-extraction.

    Args:
        path: Path to the ``.pdf`` file.

    Returns:
        :class:`ParseResult` carrying one :class:`DocumentSection` per page
        (with the page number stamped via :attr:`DocumentSection.page`).
        :attr:`ParseResult.page_count` is the total page count.
        :attr:`ParseResult.needs_vision_handoff` is ``True`` when the
        no-text-layer heuristic fires; T21 reads this to dispatch to the
        vision path.

    Raises:
        MissingDependencyError: ``pypdf`` is not installed.
        CorruptDocumentError: The file isn't a valid PDF, is encrypted
            without a passphrase, or is otherwise unreadable.
    """
    pypdf = _import_pypdf()
    size_bytes = path.stat().st_size
    filename = path.name

    if size_bytes == 0:
        raise CorruptDocumentError(
            "file is empty",
            context={"format": "pdf", "reason": "empty_file", "filename": filename},
        )

    try:
        reader = pypdf.PdfReader(str(path))
    except (MemoryError, RecursionError):
        raise
    except Exception as exc:  # noqa: BLE001 — adapter-boundary catch
        reason = _classify_open_failure(exc)
        raise CorruptDocumentError(
            "could not open PDF",
            context={"format": "pdf", "reason": reason, "filename": filename},
        ) from exc

    if getattr(reader, "is_encrypted", False):
        # Try empty-passphrase decrypt (a common case for "decrypt-just-
        # to-read" PDFs); on failure, surface as encrypted.
        try:
            decrypted_ok = reader.decrypt("") not in (0,)  # 0 = failure
        except Exception:  # noqa: BLE001
            decrypted_ok = False
        if not decrypted_ok:
            raise CorruptDocumentError(
                "PDF is encrypted",
                context={
                    "format": "pdf",
                    "reason": "encrypted",
                    "filename": filename,
                },
            )

    declared = _declared_page_count(reader)
    if declared is not None and declared > MAX_PDF_PAGES:
        raise _too_many_pages(filename, declared)
    try:
        page_count = len(reader.pages)
    except (MemoryError, RecursionError):
        raise
    except Exception as exc:  # noqa: BLE001 (adapter-boundary catch)
        raise CorruptDocumentError(
            "could not read the PDF page tree",
            context={"format": "pdf", "reason": "unreadable_pages", "filename": filename},
        ) from exc
    if page_count > MAX_PDF_PAGES:
        raise _too_many_pages(filename, page_count)

    pages_to_read = min(page_count, MAX_PDF_TEXT_PAGES)
    sections, total_chars, page_errors, pages_read = _extract_pages(reader, pages=pages_to_read)

    threshold = resolve_no_text_layer_threshold()
    coverage = (total_chars / pages_read) if pages_read > 0 else 0.0
    no_text_layer = coverage < threshold

    flags: list[str] = []
    if no_text_layer:
        flags.append(FLAG_NO_TEXT_LAYER)
    if page_errors > 0 or pages_read < page_count:
        flags.append(FLAG_PARTIAL_EXTRACTION)

    return ParseResult(
        sections=tuple(sections),
        flags=tuple(flags),
        page_count=page_count,
        size_bytes=size_bytes,
        needs_vision_handoff=no_text_layer,
    )


def _declared_page_count(reader: Any) -> int | None:  # noqa: ANN401
    """The page count the PDF's page tree root claims, read without flattening the tree.

    ``len(reader.pages)`` flattens every page object first; this reads one number, so
    an absurd claim is refused before that work starts. ``None`` when it is missing or
    not a number (the real count is then checked after flattening).
    """
    try:
        value = reader.trailer["/Root"]["/Pages"]["/Count"]
        return int(value)
    except (MemoryError, RecursionError):
        raise
    except Exception:  # noqa: BLE001 (a malformed tree is checked by the real count)
        return None


def _too_many_pages(filename: str, count: int) -> CorruptDocumentError:
    return CorruptDocumentError(
        "PDF has too many pages",
        context={
            "format": "pdf",
            "reason": "too_many_pages",
            "filename": filename,
            "page_count": str(count),
            "limit": str(MAX_PDF_PAGES),
        },
    )


def _classify_open_failure(exc: BaseException) -> str:
    """Map a pypdf open failure to a stable ``reason`` discriminator."""
    name = type(exc).__name__
    if "Empty" in name:
        return "empty_file"
    if "PdfRead" in name or "PdfStream" in name:
        return "not_a_pdf"
    return "unknown"


def _extract_pages(
    reader: Any,  # noqa: ANN401
    *,
    pages: int,
) -> tuple[list[DocumentSection], int, int, int]:
    """Extract text from each page, stopping once the document text budget is spent.

    Returns ``(sections, total_chars, error_count, pages_read)``: ``sections`` is one
    :class:`DocumentSection` per non-empty page; ``total_chars`` is the
    summed char count across pages for the no-text-layer heuristic;
    ``error_count`` is the number of pages that raised during extraction
    (each treated as zero text contribution; not a crash); ``pages_read`` is how many
    pages were read before :data:`MAX_DOCUMENT_TEXT_CHARS` was reached (pages sharing
    one huge text stream reach it in a few pages, F1).
    """
    sections: list[DocumentSection] = []
    total_chars = 0
    error_count = 0
    pages_read = 0

    for page_index in range(1, pages + 1):
        if total_chars >= MAX_DOCUMENT_TEXT_CHARS:
            break
        pages_read = page_index
        try:
            page_text = reader.pages[page_index - 1].extract_text() or ""
        except (MemoryError, RecursionError):
            raise
        except Exception:  # noqa: BLE001 — adapter-boundary catch
            page_text = ""
            error_count += 1
        cleaned = page_text.strip()[: MAX_DOCUMENT_TEXT_CHARS - total_chars]
        total_chars += len(cleaned)
        if cleaned:
            sections.append(DocumentSection(text=cleaned, page=page_index))

    return sections, total_chars, error_count, pages_read
