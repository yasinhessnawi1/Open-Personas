"""The one function that turns untrusted document bytes into text and page images.

Everything that parses or renders an uploaded file goes through
:func:`parse_untrusted_document`: the archive guard, the format parser and, for a
scanned PDF, page rendering. It takes a path and returns plain data. It touches no
database, no storage backend, no secret and no request context.

In the api it runs inside the document parse worker, a separate process with a memory
limit, a CPU limit and a wall-clock kill (D-FV0-31; :mod:`persona_api.document_worker`).
Persisting, ingesting and auditing stay with the caller, in the api, after it returns.

This module is imported by the worker process, so it imports only the parsers and
pydantic: no api service, no provider SDK, no tokenizer (a test pins that).
"""

from __future__ import annotations

import math
import os
from pathlib import Path

from persona.documents.parsers import ParseResult, cap_document_text, parse_document
from pydantic import BaseModel, ConfigDict

__all__ = [
    "DEFAULT_RASTER_DPI",
    "MAX_PAGE_IMAGE_BYTES",
    "MAX_RENDER_BYTES",
    "MAX_RENDER_PAGES",
    "RENDER_LONG_EDGE_PX",
    "RASTER_DPI_ENV_VAR",
    "ParsedDocument",
    "parse_untrusted_document",
    "render_pdf_pages",
    "resolve_raster_dpi",
]

#: T21: DPI for PDF page rasterisation (R-14-2 lean: 150, the sweet spot for
#: Anthropic and GPT-4o-class vision per the research; Spec 13's downscale D-13-1
#: reduces effective resolution to about 1568 px on the long edge).
#: Overridable via ``PERSONA_DOC_PDF_RASTER_DPI`` (R-14-2 + D-14-2 sub).
DEFAULT_RASTER_DPI: int = 150

#: Env var for operators tuning the rasterisation DPI (operates at the rasterisation
#: stage, NOT after Spec 13's downscale; see D-14-2 sub). Read in the api process: the
#: worker's environment is an allowlist, so the resolved value travels with each job.
RASTER_DPI_ENV_VAR: str = "PERSONA_DOC_PDF_RASTER_DPI"

#: The most pages of a scanned PDF rendered to images: the most images one chat message
#: can carry (``PostMessageRequest.images``, ``max_length=4``, D-13-5; a test keeps the
#: two equal). A turn can never show a model more pages than that, so rendering more only
#: costs CPU, disk and stored PNGs (second security review F4: 100 pages wrote 583 MiB).
#: Pages past it are not rendered; the text path still reports the real page count.
MAX_RENDER_PAGES: int = 4

#: The longest edge, in pixels, of a rendered page: Spec 13's downscale ceiling (D-13-1,
#: ``image_service.DOWNSCALE_CEILING_PX``; a test keeps the two equal). Every vision
#: provider is sent images at most this size, so a page rendered larger is downscaled
#: before a model ever sees it. A page whose MediaBox claims kilometres is rendered at
#: the scale that fits.
RENDER_LONG_EDGE_PX: int = 1568

#: The most bytes one rendered page's PNG may have. A 1568 by 1568 page of pure noise
#: compresses to about 7.4 MB; a scan is a fraction of that.
MAX_PAGE_IMAGE_BYTES: int = 8 * 1024 * 1024

#: The most PNG bytes one document's rendered pages may have in all, enforced in the
#: worker as it writes them: rendering stops at the page that would cross it.
MAX_RENDER_BYTES: int = MAX_RENDER_PAGES * MAX_PAGE_IMAGE_BYTES


class ParsedDocument(BaseModel):
    """What a parse returns: the extracted text, and page images for a scanned PDF.

    Plain data only, so it crosses the process boundary as JSON.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    parse_result: ParseResult
    #: Rendered page PNGs, in page order, inside the ``render_dir`` the caller gave.
    page_images: tuple[Path, ...] = ()


def resolve_raster_dpi() -> int:
    """Resolve the rasterisation DPI from env override (range 100 to 300)."""
    raw = os.environ.get(RASTER_DPI_ENV_VAR)
    if not raw:
        return DEFAULT_RASTER_DPI
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_RASTER_DPI
    if parsed < 100 or parsed > 300:
        return DEFAULT_RASTER_DPI
    return parsed


def parse_untrusted_document(
    path: Path, *, render_dir: Path | None, dpi: int | None = None
) -> ParsedDocument:
    """Parse an uploaded file and, for a scanned PDF, render its pages.

    Args:
        path: The file, named with its real extension.
        render_dir: Where page PNGs are written, or ``None`` to never render (the
            re-parse path only needs text).
        dpi: The rasterisation DPI; ``None`` resolves it from this process's
            environment (:func:`resolve_raster_dpi`).

    Returns:
        The parse result and any rendered page images.

    Raises:
        UnsupportedFormatError, CorruptDocumentError, MissingDependencyError: As
            :func:`persona.documents.parsers.parse_document` raises them.
    """
    # The text budget is applied by the dispatcher too; applying it here as well means
    # nothing a parser returns can cross to the api past it (F1).
    result = cap_document_text(parse_document(path))
    pages: tuple[Path, ...] = ()
    if render_dir is not None and result.needs_vision_handoff:
        pages = render_pdf_pages(path, render_dir, dpi=dpi)
    return ParsedDocument(parse_result=result, page_images=pages)


def _page_scale(width: float, height: float, scale: float) -> float | None:
    """The render scale that keeps a page's long edge within :data:`RENDER_LONG_EDGE_PX`.

    ``None`` when the page's stated size is not a usable number.
    """
    if not (math.isfinite(width) and math.isfinite(height)) or width <= 0 or height <= 0:
        return None
    long_edge = max(width, height) * scale
    if long_edge <= RENDER_LONG_EDGE_PX:
        return scale
    # A hair under the exact fit, so rounding the bitmap up never crosses the ceiling.
    return scale * RENDER_LONG_EDGE_PX / long_edge * 0.999


def render_pdf_pages(
    pdf_path: Path, render_dir: Path, *, dpi: int | None = None
) -> tuple[Path, ...]:
    """Render up to :data:`MAX_RENDER_PAGES` pages to PNG files, within the size budgets.

    Uses ``pypdfium2`` (BSD-3-Clause or Apache-2.0; D-14-X-pdf-library-license). A page
    whose stated size is not a usable number is skipped. Each page is rendered at the
    configured DPI or :data:`RENDER_LONG_EDGE_PX` on its long edge, whichever is
    smaller. A page whose PNG is over :data:`MAX_PAGE_IMAGE_BYTES`, or would take the
    document past :data:`MAX_RENDER_BYTES`, is deleted and rendering stops.

    Args:
        pdf_path: The PDF.
        render_dir: An existing directory the PNGs are written into.
        dpi: The rasterisation DPI; ``None`` resolves it from the environment.

    Returns:
        The written PNG paths in page order.
    """
    import pypdfium2  # noqa: PLC0415 (the [documents] extra, imported where it is used)

    scale = (dpi if dpi is not None else resolve_raster_dpi()) / 72.0
    written: list[Path] = []
    total_bytes = 0
    pdf = pypdfium2.PdfDocument(str(pdf_path))
    try:
        for page_index in range(min(len(pdf), MAX_RENDER_PAGES)):
            page = pdf[page_index]
            try:
                width, height = page.get_size()
                page_scale = _page_scale(width, height, scale)
                if page_scale is None:
                    continue
                image = page.render(scale=page_scale).to_pil()
                target = render_dir / f"page-{page_index + 1:04d}.png"
                image.save(target, format="PNG")
                size = target.stat().st_size
                if size > MAX_PAGE_IMAGE_BYTES or total_bytes + size > MAX_RENDER_BYTES:
                    target.unlink()
                    break
                total_bytes += size
                written.append(target)
            finally:
                page.close()
    finally:
        pdf.close()
    return tuple(written)
