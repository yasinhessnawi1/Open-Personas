"""The production graph roots turn the R9-284 write guard on (review mutants M7 and M8).

``persona_guard`` is a required keyword (LOW-1), so leaving it out fails at construction; these
pin that the api's two roots pass ``True``. The runtime factory is exercised: its
``enable_graph_writes`` runs with ``build_graph_store`` recorded. The worker root builds the
whole worker registry, too much to run in a unit test, so its source is read instead: every
graph construction in the module must pass the literal ``True``. The voice runner is pinned
the same way in packages/voice.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from persona_api.services.runtime_factory import RuntimeFactory

_CONSTRUCTORS = {"PostgresGraphBackend", "build_graph_store"}


def _guards_in(module: str) -> list[object]:
    """``persona_guard`` of every graph construction in ``module``'s source (``None`` if absent)."""
    spec = importlib.util.find_spec(module)
    assert spec is not None
    assert spec.origin is not None
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    guards: list[object] = []
    for node in ast.walk(tree):
        func = getattr(node, "func", None)
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if isinstance(node, ast.Call) and name in _CONSTRUCTORS:
            value = next((k.value for k in node.keywords if k.arg == "persona_guard"), None)
            guards.append(value.value if isinstance(value, ast.Constant) else value)
    return guards


def _releasers_in(module: str) -> list[object]:
    """The class each graph construction in ``module`` builds its ``node_releaser`` from."""
    spec = importlib.util.find_spec(module)
    assert spec is not None
    assert spec.origin is not None
    tree = ast.parse(Path(spec.origin).read_text(encoding="utf-8"))
    found: list[object] = []
    for node in ast.walk(tree):
        func = getattr(node, "func", None)
        name = getattr(func, "id", None) or getattr(func, "attr", None)
        if isinstance(node, ast.Call) and name in _CONSTRUCTORS:
            value = next((k.value for k in node.keywords if k.arg == "node_releaser"), None)
            built = getattr(value, "func", None)
            found.append(getattr(built, "id", None) if isinstance(value, ast.Call) else value)
    return found


def test_the_worker_root_releases_on_every_node_delete() -> None:
    """R9-287 review MEDIUM-1: both worker constructions pass a ForgetReleaser."""
    assert _releasers_in("persona_api.background.worker_root") == [
        "ForgetReleaser",
        "ForgetReleaser",
    ]


def test_the_worker_root_builds_every_graph_write_path_guarded() -> None:
    guards = _guards_in("persona_api.background.worker_root")

    assert len(guards) == 2, "the backend and the store"
    assert guards == [True, True]


def test_the_runtime_factory_builds_its_graph_store_guarded(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    built: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> object:  # noqa: ANN401
        built.append(kwargs)
        return object()

    monkeypatch.setattr("persona.graph.build_graph_store", _record)
    factory = RuntimeFactory.__new__(RuntimeFactory)  # composition-free instance
    factory._graph_store = None  # noqa: SLF001 - the seam under test
    factory._engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))  # noqa: SLF001
    factory._embedder = object()  # noqa: SLF001
    factory._audit_logger = object()  # noqa: SLF001

    factory.enable_graph_writes(audit_root=tmp_path)

    assert len(built) == 1
    assert built[0]["persona_guard"] is True


def test_the_runtime_factory_releases_on_every_node_delete(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """R9-287 review MEDIUM-1: the request path's forget and delete release what was copied."""
    from persona_api.services.forget_graph import ForgetReleaser  # noqa: PLC0415

    built: list[dict[str, Any]] = []

    def _record(**kwargs: Any) -> object:  # noqa: ANN401
        built.append(kwargs)
        return object()

    monkeypatch.setattr("persona.graph.build_graph_store", _record)
    factory = RuntimeFactory.__new__(RuntimeFactory)  # composition-free instance
    factory._graph_store = None  # noqa: SLF001 - the seam under test
    factory._engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))  # noqa: SLF001
    factory._embedder = object()  # noqa: SLF001
    factory._audit_logger = object()  # noqa: SLF001

    factory.enable_graph_writes(audit_root=tmp_path)

    assert isinstance(built[0]["node_releaser"], ForgetReleaser)
