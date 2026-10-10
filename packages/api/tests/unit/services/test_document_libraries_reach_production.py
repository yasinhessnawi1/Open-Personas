"""Every library the document path imports reaches the production api install (D-FV0-06).

The defect this guards: the parsers lazy-import pypdf, python-docx, openpyxl and
pypdfium2, and the api image is built with ``uv sync --no-dev``. The libraries lived only
in the workspace dev group, so the tests (which run with dev dependencies) were green
while every pdf, docx and xlsx upload in production answered 503.

So this test does not trust a hand-written list. It reads the third-party modules the
document path actually imports, maps each to the distribution that provides it, and
requires that distribution to be declared where the production install will see it:
persona-core's base dependencies, its ``documents`` extra (which persona-api requests),
or persona-api's own dependencies.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from importlib.metadata import packages_distributions
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

_REPO = Path(__file__).resolve().parents[5]
_CORE_PYPROJECT = _REPO / "packages" / "core" / "pyproject.toml"
_API_PYPROJECT = _REPO / "packages" / "api" / "pyproject.toml"

#: The modules that make up the document path: the parsers, the archive guard, the
#: parse worker package that rasterises scanned pages, and the api service.
_DOCUMENT_PATH_SOURCES = (
    *sorted((_REPO / "packages/core/src/persona/documents/parsers").glob("*.py")),
    _REPO / "packages/core/src/persona/documents/archive_guard.py",
    *sorted((_REPO / "packages/api/src/persona_api/document_worker").glob("*.py")),
    _REPO / "packages/api/src/persona_api/services/document_service.py",
    _REPO / "packages/api/src/persona_api/services/document_parsing.py",
)


def _project(path: Path) -> dict[str, object]:
    with path.open("rb") as handle:
        project = tomllib.load(handle)["project"]
    assert isinstance(project, dict)
    return project


def _names(requirements: object) -> set[str]:
    assert isinstance(requirements, list)
    return {canonicalize_name(Requirement(str(r)).name) for r in requirements}


def _third_party_modules(path: Path) -> set[str]:
    """Top-level modules a source file imports, at any depth, minus stdlib and our own."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module.split(".")[0])
    return {
        name
        for name in found
        if name not in sys.stdlib_module_names and not name.startswith("persona")
    }


def _production_distributions() -> set[str]:
    core = _project(_CORE_PYPROJECT)
    api = _project(_API_PYPROJECT)
    api_requirements = [Requirement(str(r)) for r in api["dependencies"]]  # type: ignore[union-attr]
    core_request = next(r for r in api_requirements if canonicalize_name(r.name) == "persona-core")
    extras = core["optional-dependencies"]
    assert isinstance(extras, dict)
    declared = _names(core["dependencies"]) | _names(api["dependencies"])
    for extra in core_request.extras:
        declared |= _names(extras[extra])
    return declared


def test_persona_api_requests_the_documents_extra() -> None:
    api_requirements = [Requirement(str(r)) for r in _project(_API_PYPROJECT)["dependencies"]]  # type: ignore[union-attr]
    core_request = next(r for r in api_requirements if canonicalize_name(r.name) == "persona-core")
    assert "documents" in core_request.extras


def test_the_documents_extra_names_the_parser_libraries() -> None:
    extras = _project(_CORE_PYPROJECT)["optional-dependencies"]
    assert isinstance(extras, dict)
    assert _names(extras["documents"]) == {"openpyxl", "python-docx", "pypdf", "pypdfium2"}


def test_every_library_the_document_path_imports_reaches_production() -> None:
    modules: set[str] = set()
    for source in _DOCUMENT_PATH_SOURCES:
        assert source.is_file(), source
        modules |= _third_party_modules(source)
    # The discovery must see the libraries this test exists for, or it proves nothing.
    assert {"pypdf", "docx", "openpyxl", "pypdfium2"} <= modules, modules

    providers = packages_distributions()
    production = _production_distributions()
    missing = {
        module: providers.get(module)
        for module in modules
        if not {canonicalize_name(d) for d in providers.get(module, [])} & production
    }
    assert missing == {}, (
        f"imported by the document path but not installed in production: {missing}"
    )
