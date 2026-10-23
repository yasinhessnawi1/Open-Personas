"""The explicit media-type table is the same on every machine (Spec WIN, T2)."""

from __future__ import annotations

import ast
import mimetypes
from pathlib import Path

import pytest
from persona.media_types import DEFAULT_MEDIA_TYPE, extension_for_media_type, media_type_for

REPO = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("a.csv", "text/csv"),  # the Windows registry says application/vnd.ms-excel
        ("a.json", "application/json"),
        ("a.pdf", "application/pdf"),
        (
            "report.docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("sheet.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("chart.png", "image/png"),
        ("photo.JPG", "image/jpeg"),
        ("pic.webp", "image/webp"),
        ("notes.md", "text/markdown"),
        ("config.yaml", "application/yaml"),
        ("script.js", "text/javascript"),
        ("page.xml", "application/xml"),
        ("charts/2026/sales.png", "image/png"),
        ("C:\\Users\\x\\out\\report.pdf", "application/pdf"),
        ("data.parquet", DEFAULT_MEDIA_TYPE),
        ("noext", DEFAULT_MEDIA_TYPE),
    ],
)
def test_each_extension_has_one_answer(name: str, expected: str) -> None:
    assert media_type_for(name) == expected


def test_the_answer_ignores_the_machine_mimetypes_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Whatever the registry or /etc/mime.types says, the table decides."""
    monkeypatch.setattr(
        mimetypes, "guess_type", lambda *_a, **_k: ("application/vnd.ms-excel", None)
    )
    assert media_type_for("a.csv") == "text/csv"


@pytest.mark.parametrize(
    ("media_type", "expected"),
    [
        ("image/jpeg", ".jpg"),
        ("text/html; charset=utf-8", ".html"),
        ("IMAGE/PNG", ".png"),
        ("application/x-unknown", None),
    ],
)
def test_the_preferred_extension_for_a_media_type(media_type: str, expected: str | None) -> None:
    assert extension_for_media_type(media_type) == expected


def test_no_product_module_consults_the_machine_mimetypes_database() -> None:
    """A guard, so the machine-dependent answer cannot come back."""
    sources = sorted(REPO.glob("packages/*/src/**/*.py"))
    assert len(sources) > 200
    offenders = []
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            else:
                continue
            if "mimetypes" in names:
                offenders.append(f"{path.relative_to(REPO).as_posix()}:{node.lineno}")
    assert not offenders, f"use persona.media_types instead of mimetypes: {offenders}"
