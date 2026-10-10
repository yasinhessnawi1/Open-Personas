"""Page rendering is bounded in pages, in size and in bytes (review item 5, review 2 F4).

A scanned PDF's pages are rendered to images for the vision tier. The file states both
how many pages it has and how large each one is (its MediaBox), so both are bounded,
each crossed in its real unit, and so are the PNG bytes written. A turn carries at most
four images, each sent at most 1,568 px on its long edge, so that is what is rendered.
"""

from __future__ import annotations

import math
import zlib
from typing import TYPE_CHECKING

import pytest
from persona_api.document_worker import untrusted as document_parsing
from persona_api.document_worker.untrusted import (
    MAX_PAGE_IMAGE_BYTES,
    MAX_RENDER_BYTES,
    MAX_RENDER_PAGES,
    RENDER_LONG_EDGE_PX,
    parse_untrusted_document,
    render_pdf_pages,
)
from PIL import Image

if TYPE_CHECKING:
    from pathlib import Path


def _blank_pdf(path: Path, *, pages: int, width: float = 612, height: float = 792) -> Path:
    """A PDF of ``pages`` empty pages sharing one MediaBox."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    first = len(objects) + 1
    kids = " ".join(f"{first + i} 0 R" for i in range(pages))
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode()
    box = f"[0 0 {width} {height}]".encode()
    for _ in range(pages):
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox " + box + b" >>")
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    path.write_bytes(bytes(out))
    return path


def _noise_pdf(path: Path, *, pages: int) -> Path:
    """The second review's ``render_100p_a4.pdf`` shape: a full-page image of pure noise."""
    import os  # noqa: PLC0415

    raw = os.urandom(1500 * 1500 * 3)
    image = zlib.compress(raw, 9)
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    objects.append(
        b"<< /Type /XObject /Subtype /Image /Width 1500 /Height 1500 /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n"
        % len(image)
        + image
        + b"\nendstream"
    )
    content = b"q 612 0 0 792 0 0 cm /Im0 Do Q"
    objects.append(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
    first = len(objects) + 1
    kids = " ".join(f"{first + i} 0 R" for i in range(pages))
    objects[1] = f"<< /Type /Pages /Kids [{kids}] /Count {pages} >>".encode()
    for _ in range(pages):
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /XObject << /Im0 3 0 R >> >> /Contents 4 0 R >>"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    path.write_bytes(bytes(out))
    return path


def _render(tmp_path: Path, source: Path, name: str = "out") -> tuple[Path, ...]:
    out = tmp_path / name
    out.mkdir()
    return render_pdf_pages(source, out)


def test_the_page_count_is_what_one_message_can_carry() -> None:
    """A turn carries at most ``PostMessageRequest.images`` images: render no more."""
    from persona_api.schemas.requests import PostMessageRequest  # noqa: PLC0415

    limits = PostMessageRequest.model_fields["images"].metadata
    max_length = next(m.max_length for m in limits if hasattr(m, "max_length"))
    assert max_length == MAX_RENDER_PAGES == 4


def test_the_long_edge_is_the_spec_13_downscale_ceiling() -> None:
    from persona_api.services.image_service import DOWNSCALE_CEILING_PX  # noqa: PLC0415

    assert RENDER_LONG_EDGE_PX == DOWNSCALE_CEILING_PX == 1568


def test_a_small_page_renders_at_its_full_resolution(tmp_path: Path) -> None:
    """Valid input passes: a page under the ceiling renders exactly as an uncapped render."""
    import pypdfium2

    source = _blank_pdf(tmp_path / "a.pdf", pages=1, width=400, height=500)
    (page,) = _render(tmp_path, source)
    pdf = pypdfium2.PdfDocument(str(source))
    try:
        reference = pdf[0].render(scale=150 / 72).to_pil()
    finally:
        pdf.close()
    with Image.open(page) as image:
        assert (image.width, image.height) == (reference.width, reference.height)
    assert max(reference.width, reference.height) < RENDER_LONG_EDGE_PX


def test_a_letter_page_renders_at_the_ceiling(tmp_path: Path) -> None:
    """At 150 dpi a letter page is 1,650 px tall; it is rendered 1,568 px tall instead."""
    (page,) = _render(tmp_path, _blank_pdf(tmp_path / "l.pdf", pages=1))
    with Image.open(page) as image:
        assert RENDER_LONG_EDGE_PX - 3 <= image.height <= RENDER_LONG_EDGE_PX
        assert image.width < image.height


def test_rendering_stops_at_the_page_limit(tmp_path: Path) -> None:
    at = _render(tmp_path, _blank_pdf(tmp_path / "at.pdf", pages=MAX_RENDER_PAGES))
    assert len(at) == MAX_RENDER_PAGES
    over = _render(tmp_path, _blank_pdf(tmp_path / "over.pdf", pages=MAX_RENDER_PAGES + 1), "out2")
    assert len(over) == MAX_RENDER_PAGES
    assert len(list((tmp_path / "out2").iterdir())) == MAX_RENDER_PAGES


def test_the_reviewers_noise_scan_writes_four_pages_within_the_byte_budget(
    tmp_path: Path,
) -> None:
    """render_100p_a4.pdf wrote 583 MiB for 100 pages; the worst page now still fits.

    Pure noise is the least compressible page there is, so it is the valid input closest
    to the per-page cap.
    """
    pages = _render(tmp_path, _noise_pdf(tmp_path / "noise.pdf", pages=8))
    assert len(pages) == MAX_RENDER_PAGES
    sizes = [p.stat().st_size for p in pages]
    assert all(size <= MAX_PAGE_IMAGE_BYTES for size in sizes)
    assert sum(sizes) <= MAX_RENDER_BYTES
    assert sum(sizes) > 4 * 1024 * 1024, "precondition: the pages really are noise"


def test_a_page_png_over_the_per_page_cap_is_deleted_and_rendering_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _blank_pdf(tmp_path / "b.pdf", pages=3)
    (one,) = _render(tmp_path, _blank_pdf(tmp_path / "one.pdf", pages=1), "probe")
    size = one.stat().st_size
    monkeypatch.setattr(document_parsing, "MAX_PAGE_IMAGE_BYTES", size)
    assert len(_render(tmp_path, source, "at")) == 3  # exactly at the cap: kept
    monkeypatch.setattr(document_parsing, "MAX_PAGE_IMAGE_BYTES", size - 1)
    assert _render(tmp_path, source, "over") == ()
    assert list((tmp_path / "over").iterdir()) == []


def test_the_document_byte_budget_stops_at_the_page_that_would_cross_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _blank_pdf(tmp_path / "b.pdf", pages=3)
    (one,) = _render(tmp_path, _blank_pdf(tmp_path / "one.pdf", pages=1), "probe")
    size = one.stat().st_size
    monkeypatch.setattr(document_parsing, "MAX_RENDER_BYTES", 2 * size)
    assert len(_render(tmp_path, source, "two")) == 2
    assert len(list((tmp_path / "two").iterdir())) == 2
    monkeypatch.setattr(document_parsing, "MAX_RENDER_BYTES", 3 * size)
    assert len(_render(tmp_path, source, "three")) == 3


@pytest.mark.asyncio
async def test_a_page_with_an_enormous_mediabox_renders_at_the_ceiling(
    tmp_path: Path,
) -> None:
    """14,400 points square is the largest page PDF allows: 900 MP at 150 dpi uncapped.

    Rendered in a real parse worker (D-FV0-31): if the size bound regressed, the render
    would ask for about 3.6 GB, and the worker's memory limit turns that into one
    refused parse instead of a dead test run.
    """
    from persona_api.document_worker.pool import ProcessDocumentParser  # noqa: PLC0415

    out = tmp_path / "out"
    out.mkdir()
    pool = ProcessDocumentParser(workers=1)
    try:
        parsed = await pool.parse(
            _blank_pdf(tmp_path / "huge.pdf", pages=1, width=14_400, height=14_400),
            render_dir=out,
            owner_id="test",
            purpose="upload",
        )
    finally:
        await pool.aclose()
    (page,) = parsed.page_images
    with Image.open(page) as image:
        # Clamped, not skipped: rendered at the ceiling.
        assert RENDER_LONG_EDGE_PX - 3 <= max(image.width, image.height) <= RENDER_LONG_EDGE_PX


def test_the_scale_is_untouched_at_exactly_the_ceiling() -> None:
    scale = 150 / 72
    side = RENDER_LONG_EDGE_PX / scale
    assert document_parsing._page_scale(side, side / 2, scale) == scale


def test_the_scale_shrinks_just_past_the_ceiling() -> None:
    scale = 150 / 72
    side = RENDER_LONG_EDGE_PX / scale * 1.001
    shrunk = document_parsing._page_scale(side / 2, side, scale)
    assert shrunk is not None
    assert shrunk < scale
    assert side * shrunk <= RENDER_LONG_EDGE_PX


@pytest.mark.parametrize(
    ("width", "height"),
    [(0.0, 792.0), (612.0, -1.0), (math.inf, 792.0), (math.nan, 792.0)],
)
def test_a_page_with_an_unusable_size_is_skipped(width: float, height: float) -> None:
    assert document_parsing._page_scale(width, height, 150 / 72) is None


def test_a_scanned_pdf_parse_renders_its_pages_and_a_text_parse_does_not(
    tmp_path: Path,
) -> None:
    out = tmp_path / "pages"
    out.mkdir()
    parsed = parse_untrusted_document(_blank_pdf(tmp_path / "s.pdf", pages=3), render_dir=out)
    assert parsed.parse_result.needs_vision_handoff
    assert [p.name for p in parsed.page_images] == [
        "page-0001.png",
        "page-0002.png",
        "page-0003.png",
    ]
    no_render = parse_untrusted_document(tmp_path / "s.pdf", render_dir=None)
    assert no_render.page_images == ()


def test_the_seam_cuts_whatever_the_dispatcher_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """F1: the text budget is applied in parse_untrusted_document as well, so nothing past
    it can cross to the api even if the dispatcher's own cut were bypassed."""
    from persona.documents.chunker import DocumentSection  # noqa: PLC0415
    from persona.documents.parsers import (  # noqa: PLC0415
        FLAG_PARTIAL_EXTRACTION,
        MAX_DOCUMENT_TEXT_CHARS,
        ParseResult,
    )

    def _flood(_path: Path) -> ParseResult:
        return ParseResult(sections=(DocumentSection(text="x" * (MAX_DOCUMENT_TEXT_CHARS + 7)),))

    monkeypatch.setattr(document_parsing, "parse_document", _flood)
    parsed = parse_untrusted_document(tmp_path / "any.txt", render_dir=None)
    assert len(parsed.parse_result.full_text) == MAX_DOCUMENT_TEXT_CHARS
    assert FLAG_PARTIAL_EXTRACTION in parsed.parse_result.flags
