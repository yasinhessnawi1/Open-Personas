"""One text budget for every format (second security review F1).

A 12 KB PDF whose 60 pages share one content stream of a million characters of text
parsed to 60 million characters, all of which crossed to the api to be tokenised,
chunked and embedded. Every format is now cut at the same four million characters, in
the dispatcher no parser can bypass, and the PDF parser stops reading pages once the
budget is spent. Valid input at the budget passes whole.
"""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING

from persona.documents import ingest as ingest_module
from persona.documents.chunker import DocumentSection
from persona.documents.ingest import MAX_DOCUMENT_CHUNKS, IngestStrategy, ingest_document
from persona.documents.parsers import (
    FLAG_PARTIAL_EXTRACTION,
    MAX_DOCUMENT_SECTIONS,
    MAX_DOCUMENT_TEXT_CHARS,
    ParseResult,
    cap_document_text,
    parse_document,
)

from tests.unit.documents._bounded_parse import bounded_parse

if TYPE_CHECKING:
    from pathlib import Path

    import pytest


def _shared_stream_pdf(path: Path, *, pages: int, text: bytes) -> Path:
    """The reviewer's text bomb: ``pages`` pages that all draw one content stream."""
    stream = zlib.compress(b"BT /F1 12 Tf 10 10 Td (" + text + b") Tj ET", 9)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    kids = []
    for _ in range(pages):
        kids.append(len(objects) + 1)
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents 4 0 R >>"
        )
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (
        b" ".join(b"%d 0 R" % kid for kid in kids),
        pages,
    )
    out = bytearray(b"%PDF-1.7\n")
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


_LOREM = b"lorem ipsum dolor sit amet "


# --------------------------------------------------------------------------- the bomb


def test_the_reviewers_text_bomb_is_cut_at_the_budget(tmp_path: Path) -> None:
    """textbomb_60p_1MB.pdf: 12 KB of file, 60 million characters before the fix."""
    text = _LOREM * (1_000_000 // len(_LOREM))
    bomb = _shared_stream_pdf(tmp_path / "textbomb_60p_1MB.pdf", pages=60, text=text)
    assert bomb.stat().st_size < 20_000, "precondition: a tiny file"
    result = bounded_parse(bomb).result
    assert len(result.full_text) <= MAX_DOCUMENT_TEXT_CHARS
    assert len(result.full_text) > MAX_DOCUMENT_TEXT_CHARS - 1_000_000
    assert FLAG_PARTIAL_EXTRACTION in result.flags
    # The parser stopped reading pages once the budget was spent: 4 of 60.
    assert len(result.sections) <= 5
    assert result.page_count == 60


def test_the_pdf_parser_stops_reading_pages_once_the_budget_is_spent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The work, not only the output: 4 of the bomb's 60 pages are extracted, not 60.

    A smaller bomb (40 pages of 200,000 characters) keeps this in-process test cheap.
    """
    import pypdf  # noqa: PLC0415

    extracted: list[int] = []
    real = pypdf.PageObject.extract_text

    def _counting(self: object, *args: object, **kwargs: object) -> str:
        extracted.append(1)
        return real(self, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(pypdf.PageObject, "extract_text", _counting)
    text = _LOREM * (200_000 // len(_LOREM))
    bomb = _shared_stream_pdf(tmp_path / "bomb.pdf", pages=40, text=text)
    monkeypatch.setattr("persona.documents.parsers.pdf.MAX_DOCUMENT_TEXT_CHARS", 1_000_000)
    result = parse_document(bomb)
    assert FLAG_PARTIAL_EXTRACTION in result.flags
    assert len(extracted) == 6  # five full pages and the one that crosses the budget


def test_the_text_parser_cuts_before_splitting(tmp_path: Path) -> None:
    """The plain-text parser applies the budget itself, not only through the dispatcher."""
    from persona.documents.parsers.text import parse_text  # noqa: PLC0415

    result = parse_text(_text_file(tmp_path, MAX_DOCUMENT_TEXT_CHARS + 10))
    assert len(result.full_text) == MAX_DOCUMENT_TEXT_CHARS
    assert FLAG_PARTIAL_EXTRACTION in result.flags


def test_a_genuine_long_pdf_inside_the_budget_is_read_whole(tmp_path: Path) -> None:
    """Valid input passes: 900 pages of 4,000 characters each, 3.6 million in all."""
    page_text = (_LOREM * (4_000 // len(_LOREM)))[:4_000]
    path = _shared_stream_pdf(tmp_path / "report.pdf", pages=900, text=page_text)
    result = bounded_parse(path).result
    assert FLAG_PARTIAL_EXTRACTION not in result.flags
    assert len(result.sections) == 900


# --------------------------------------------------------------------------- the dispatcher


def _text_file(tmp_path: Path, chars: int) -> Path:
    path = tmp_path / f"t{chars}.txt"
    path.write_text("a" * chars, encoding="utf-8")
    return path


def test_a_text_file_exactly_at_the_budget_is_read_whole(tmp_path: Path) -> None:
    result = parse_document(_text_file(tmp_path, MAX_DOCUMENT_TEXT_CHARS))
    assert len(result.full_text) == MAX_DOCUMENT_TEXT_CHARS
    assert FLAG_PARTIAL_EXTRACTION not in result.flags


def test_a_text_file_one_character_past_the_budget_is_cut_and_flagged(tmp_path: Path) -> None:
    result = parse_document(_text_file(tmp_path, MAX_DOCUMENT_TEXT_CHARS + 1))
    assert len(result.full_text) == MAX_DOCUMENT_TEXT_CHARS
    assert FLAG_PARTIAL_EXTRACTION in result.flags


def test_the_dispatcher_cuts_whatever_a_parser_returns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No parser can skip the budget: a parser that ignores it is cut at the dispatcher."""
    import importlib  # noqa: PLC0415

    csv_module = importlib.import_module("persona.documents.parsers.csv")

    def _flood(_path: Path) -> ParseResult:
        return ParseResult(sections=(DocumentSection(text="x" * (MAX_DOCUMENT_TEXT_CHARS * 2)),))

    monkeypatch.setattr(csv_module, "parse_csv", _flood)
    path = tmp_path / "f.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    result = parse_document(path)
    assert len(result.full_text) == MAX_DOCUMENT_TEXT_CHARS
    assert FLAG_PARTIAL_EXTRACTION in result.flags


# --------------------------------------------------------------------------- cap_document_text


def _sections(*lengths: int) -> ParseResult:
    return ParseResult(sections=tuple(DocumentSection(text="s" * n) for n in lengths))


def test_sections_that_fit_exactly_with_their_separators_are_untouched() -> None:
    half = (MAX_DOCUMENT_TEXT_CHARS - 2) // 2
    result = _sections(half, half)
    assert len(result.full_text) == MAX_DOCUMENT_TEXT_CHARS
    assert cap_document_text(result) is result


def test_the_section_that_crosses_the_budget_is_cut_and_later_ones_dropped() -> None:
    half = (MAX_DOCUMENT_TEXT_CHARS - 2) // 2
    capped = cap_document_text(_sections(half, half + 1, 10))
    assert len(capped.full_text) == MAX_DOCUMENT_TEXT_CHARS
    assert len(capped.sections) == 2
    assert FLAG_PARTIAL_EXTRACTION in capped.flags


def test_an_existing_partial_flag_is_not_repeated() -> None:
    result = ParseResult(
        sections=(DocumentSection(text="z" * (MAX_DOCUMENT_TEXT_CHARS + 5)),),
        flags=(FLAG_PARTIAL_EXTRACTION,),
    )
    assert cap_document_text(result).flags == (FLAG_PARTIAL_EXTRACTION,)


# --------------------------------------------------------------------------- the section cap


def _labelled(count: int) -> ParseResult:
    return ParseResult(
        sections=tuple(DocumentSection(text=f"para {n}", page=n + 1) for n in range(count))
    )


def test_the_section_cap_is_ten_thousand() -> None:
    assert MAX_DOCUMENT_SECTIONS == 10_000


def test_sections_at_the_cap_are_untouched() -> None:
    result = _labelled(MAX_DOCUMENT_SECTIONS)
    assert cap_document_text(result) is result


def test_sections_past_the_cap_are_joined_into_the_last_with_the_text_unchanged() -> None:
    """Third review finding 11: a million one-word paragraphs become at most 10,000."""
    result = _labelled(MAX_DOCUMENT_SECTIONS + 50)
    capped = cap_document_text(result)
    assert len(capped.sections) == MAX_DOCUMENT_SECTIONS
    assert capped.full_text == result.full_text
    assert capped.sections[-2] == result.sections[MAX_DOCUMENT_SECTIONS - 2]
    last = capped.sections[-1]
    assert last.page == MAX_DOCUMENT_SECTIONS  # the first joined section's label
    assert last.text.startswith(f"para {MAX_DOCUMENT_SECTIONS - 1}\n\npara ")
    assert last.text.endswith(f"para {MAX_DOCUMENT_SECTIONS + 49}")
    assert capped.flags == ()  # no text was lost


def test_blank_sections_past_the_cap_add_no_blank_lines() -> None:
    sections = [DocumentSection(text="a")] * MAX_DOCUMENT_SECTIONS
    sections += [DocumentSection(text="  "), DocumentSection(text="b")]
    result = ParseResult(sections=tuple(sections))
    capped = cap_document_text(result)
    assert capped.sections[-1].text == "a\n\nb"
    assert capped.full_text == result.full_text


def test_the_text_budget_and_the_section_cap_apply_together() -> None:
    per = MAX_DOCUMENT_TEXT_CHARS // (MAX_DOCUMENT_SECTIONS * 2)
    result = _sections(*([per] * (MAX_DOCUMENT_SECTIONS * 3)))
    capped = cap_document_text(result)
    assert len(capped.sections) == MAX_DOCUMENT_SECTIONS
    assert len(capped.full_text) == MAX_DOCUMENT_TEXT_CHARS
    assert FLAG_PARTIAL_EXTRACTION in capped.flags


# --------------------------------------------------------------------------- chunks


class _Store:
    def __init__(self) -> None:
        self.written: list[object] = []

    def write(self, _conversation_id: str, chunks: list[object]) -> None:
        self.written.extend(chunks)


def _ingest(sections: int) -> tuple[object, _Store]:
    store = _Store()
    parse = ParseResult(
        sections=tuple(
            DocumentSection(text=f"Paragraph {i} of the lease.") for i in range(sections)
        )
    )
    result = ingest_document(
        parse_result=parse,
        conversation_id="c",
        doc_ref="d",
        title="t",
        document_format="txt",
        document_store=store,  # type: ignore[arg-type]
        threshold_tokens=1,
    )
    return result, store


def test_the_chunk_cap_is_one_thousand() -> None:
    """Third security review, approved: 1,000 chunks per document, about 448,000 tokens."""
    assert MAX_DOCUMENT_CHUNKS == 1_000


def test_a_document_at_the_chunk_cap_writes_every_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ingest_module, "MAX_DOCUMENT_CHUNKS", 5)
    result, store = _ingest(5)
    assert result.strategy == IngestStrategy.RETRIEVAL  # type: ignore[attr-defined]
    assert result.chunk_count == 5  # type: ignore[attr-defined]
    assert result.chunks_truncated is False  # type: ignore[attr-defined]
    assert len(store.written) == 5


def test_a_document_one_chunk_past_the_cap_is_cut_and_says_so(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(ingest_module, "MAX_DOCUMENT_CHUNKS", 5)
    result, store = _ingest(6)
    assert result.chunk_count == 5  # type: ignore[attr-defined]
    assert result.chunks_truncated is True  # type: ignore[attr-defined]
    assert len(store.written) == 5
