"""Tests for ``persona.documents.parsers.text`` (spec 14 T06).

Three formats handled by one parser: ``.txt`` (paragraph sections),
``.md`` (heading-bounded sections), source code (language-fenced single
section). Empty-extraction-from-non-empty-file is criterion #8's binary —
both the literal-empty-file path and the bytes-decode-to-nothing path
raise :class:`CorruptDocumentError`.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from persona.documents.errors import CorruptDocumentError
from persona.documents.parsers import FLAG_CORRUPT
from persona.documents.parsers.text import (
    CODE_FENCE_LANGUAGE_BY_EXTENSION,
    parse_text,
)

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "fixtures" / "documents"


class TestPlainText:
    def test_paragraphs_become_sections(self) -> None:
        result = parse_text(FIXTURE_DIR / "sample.txt")
        # Four paragraphs in the fixture.
        assert len(result.sections) == 4

    def test_section_text_excludes_paragraph_separators(self) -> None:
        result = parse_text(FIXTURE_DIR / "sample.txt")
        # No section text should contain a leading or trailing newline.
        for section in result.sections:
            assert section.text == section.text.strip()

    def test_size_bytes_populated(self) -> None:
        result = parse_text(FIXTURE_DIR / "sample.txt")
        assert result.size_bytes is not None
        assert result.size_bytes > 0

    def test_full_text_property_concatenates(self) -> None:
        result = parse_text(FIXTURE_DIR / "sample.txt")
        assert "twelve months" in result.full_text
        assert "husleieloven" in result.full_text

    def test_no_flags_on_clean_input(self) -> None:
        result = parse_text(FIXTURE_DIR / "sample.txt")
        assert result.flags == ()

    def test_is_not_empty(self) -> None:
        result = parse_text(FIXTURE_DIR / "sample.txt")
        assert not result.is_empty


class TestMarkdown:
    def test_markdown_splits_on_headings(self) -> None:
        result = parse_text(FIXTURE_DIR / "sample.md")
        # The fixture has 1 H1 + 4 H2 = 5 heading-bounded sections.
        assert len(result.sections) == 5

    def test_section_heading_stamped(self) -> None:
        result = parse_text(FIXTURE_DIR / "sample.md")
        headings = [s.section for s in result.sections]
        assert "Tenancy Memo" in headings
        assert "Overview" in headings
        assert "Term" in headings
        assert "Rent" in headings
        assert "Termination" in headings

    def test_section_body_includes_heading_for_context(self) -> None:
        result = parse_text(FIXTURE_DIR / "sample.md")
        # The body of the "Term" section should include the heading text and
        # the term content, so a chunk read in isolation has its heading.
        term_section = next(s for s in result.sections if s.section == "Term")
        assert "Term" in term_section.text
        assert "twelve months" in term_section.text


class TestSourceCode:
    def test_code_fence_wraps_python_file(self) -> None:
        result = parse_text(FIXTURE_DIR / "sample.py")
        assert len(result.sections) == 1
        assert result.sections[0].text.startswith("```python\n")
        assert result.sections[0].text.rstrip().endswith("```")

    def test_code_content_preserved_inside_fence(self) -> None:
        result = parse_text(FIXTURE_DIR / "sample.py")
        body = result.sections[0].text
        assert "def calculate_monthly_rent" in body
        assert "annual_rent" in body

    def test_extension_map_carries_common_languages(self) -> None:
        # Quick guard: the language map covers the common cases (full
        # coverage is verified by inspection; this catches regressions).
        for ext in (".py", ".js", ".ts", ".rs", ".go", ".java", ".sql"):
            assert ext in CODE_FENCE_LANGUAGE_BY_EXTENSION


class TestEmptyAndCorrupt:
    def test_literal_empty_file_raises_corrupt(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.txt"
        empty.write_bytes(b"")
        with pytest.raises(CorruptDocumentError) as excinfo:
            parse_text(empty)
        assert excinfo.value.context["reason"] == "empty_file"
        assert excinfo.value.context["format"] == "txt"
        assert excinfo.value.context["filename"] == "empty.txt"

    def test_non_empty_bytes_no_text_raises_corrupt(self, tmp_path: Path) -> None:
        # Criterion #8 — empty extraction from a non-empty file is flagged.
        # An all-whitespace file qualifies.
        whitespace_only = tmp_path / "whitespace.txt"
        whitespace_only.write_bytes(b"   \n\n\t\n   \n")
        with pytest.raises(CorruptDocumentError) as excinfo:
            parse_text(whitespace_only)
        assert excinfo.value.context["reason"] == "empty_after_decode"
        assert excinfo.value.context["format"] == "txt"

    def test_invalid_utf8_falls_back_and_flags(self, tmp_path: Path) -> None:
        # Mixed valid + invalid UTF-8 bytes — decode replaces, flag is set.
        bad = tmp_path / "mixed.txt"
        bad.write_bytes(b"Valid prefix.\n\n\xff\xfe invalid bytes\n\nValid suffix.")
        result = parse_text(bad)
        assert FLAG_CORRUPT in result.flags
        # But extraction proceeded — the valid prefix and suffix are there.
        assert "Valid prefix" in result.full_text
        assert "Valid suffix" in result.full_text


class TestFrozenResult:
    def test_parse_result_is_immutable(self) -> None:
        result = parse_text(FIXTURE_DIR / "sample.txt")
        with pytest.raises(Exception):  # noqa: B017,PT011 — pydantic validation error
            result.flags = ("mutated",)  # type: ignore[misc]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_line_endings_do_not_change_the_paragraphs(tmp_path: Path, newline: str) -> None:
    """Spec WIN T2: LF, CRLF (Windows) and lone CR give identical sections, on every
    platform. The bytes are written as-is, never through text-mode translation."""
    body = newline.join(
        ["First paragraph line one.", "Line two.", "", "Second paragraph.", "", "Third."]
    )
    target = tmp_path / f"endings{len(newline)}{'r' if newline == chr(13) else 'n'}.txt"
    target.write_bytes(body.encode("utf-8"))
    sections = [s.text for s in parse_text(target).sections]
    assert sections == ["First paragraph line one.\nLine two.", "Second paragraph.", "Third."]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_markdown_headings_split_the_same_for_every_line_ending(
    tmp_path: Path, newline: str
) -> None:
    body = newline.join(["# One", "alpha", "", "## Two", "beta"])
    target = tmp_path / f"endings{len(newline)}{'r' if newline == chr(13) else 'n'}.md"
    target.write_bytes(body.encode("utf-8"))
    sections = parse_text(target).sections
    assert [s.section for s in sections] == ["One", "Two"]
    assert all("\r" not in s.text for s in sections)
