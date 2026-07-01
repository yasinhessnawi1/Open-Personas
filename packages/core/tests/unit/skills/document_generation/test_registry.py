"""Registry + handler descriptor tests (A1/A2; D-24-1 Reading B)."""

from __future__ import annotations

import pytest
from persona.errors import UnknownDocumentFormatError, UnknownDocumentTemplateError
from persona.skills.document_generation import (
    FORMAT_HANDLERS,
    TEMPLATES,
    DocumentHandler,
    resolve_format,
    resolve_template,
    supported_formats,
    supported_templates,
)

_EXPECTED_FORMATS = {"docx", "pdf", "pptx", "xlsx", "md", "txt"}


def test_registry_exposes_exactly_the_curated_six_formats() -> None:
    assert set(FORMAT_HANDLERS) == _EXPECTED_FORMATS
    assert supported_formats() == tuple(sorted(_EXPECTED_FORMATS))


def test_each_handler_key_matches_its_descriptor_format_key() -> None:
    for key, handler in FORMAT_HANDLERS.items():
        assert handler.format_key == key


def test_every_handler_satisfies_the_document_handler_protocol() -> None:
    for handler in FORMAT_HANDLERS.values():
        assert isinstance(handler, DocumentHandler)


@pytest.mark.parametrize(
    ("fmt", "ext", "lib"),
    [
        # ``library`` records the SANDBOX route the SKILL.md teaches. The sandbox
        # has egress disabled (no runtime ``pip install``); libs are pre-installed
        # (baked into the template at BUILD time). Spec P5: pdf try-imports reportlab
        # (custom template) and falls back to matplotlib; pptx is python-pptx on the
        # custom template, else degraded honestly — both documented conditionally.
        ("docx", ".docx", "python-docx==1.1.2"),
        ("pdf", ".pdf", "reportlab||matplotlib"),
        ("pptx", ".pptx", "python-pptx (custom template) || unavailable-offline"),
        ("xlsx", ".xlsx", "openpyxl==3.1.5"),
        ("md", ".md", "stdlib"),
        ("txt", ".txt", "stdlib"),
    ],
)
def test_resolve_format_returns_descriptor_with_expected_metadata(
    fmt: str, ext: str, lib: str
) -> None:
    handler = resolve_format(fmt)
    assert handler.output_extension == ext
    assert handler.library == lib


def test_resolve_format_rejects_unknown_format_with_context() -> None:
    with pytest.raises(UnknownDocumentFormatError) as exc_info:
        resolve_format("odt")
    assert exc_info.value.context["format"] == "odt"
    assert "docx" in exc_info.value.context["available"]


def test_binary_format_handlers_declare_supplement_topics() -> None:
    assert resolve_format("docx").supplement_topics == ("tables", "styles", "images", "toc")
    assert resolve_format("pdf").supplement_topics == ("flowables", "pagination", "images")


def test_text_format_handlers_declare_no_supplements() -> None:
    assert resolve_format("md").supplement_topics == ()
    assert resolve_format("txt").supplement_topics == ()


def test_resolve_template_returns_bundled_filename() -> None:
    assert resolve_template("business_letter") == "business_letter.md"
    assert set(TEMPLATES) == set(supported_templates())


def test_resolve_template_rejects_unknown_template_with_context() -> None:
    with pytest.raises(UnknownDocumentTemplateError) as exc_info:
        resolve_template("invoice")
    assert exc_info.value.context["template"] == "invoice"
    assert "memo" in exc_info.value.context["available"]
