"""The worker root closes the erase sweep's waiting thread when the worker drains (M27).

Building the whole worker registry is too much for a unit test, so the source is read: the
``on_drain`` the worker gets is ``_on_drain``, and ``_on_drain`` calls ``erase_sweep.close()``.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path


def _tree() -> ast.Module:
    spec = importlib.util.find_spec("persona_api.background.worker_root")
    assert spec is not None
    assert spec.origin is not None
    return ast.parse(Path(spec.origin).read_text(encoding="utf-8"))


def test_the_workers_drain_closes_the_erase_sweep() -> None:
    tree = _tree()
    drains = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_on_drain"]
    assert len(drains) == 1
    closes = [
        n
        for n in ast.walk(drains[0])
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "close"
        and isinstance(n.func.value, ast.Name)
        and n.func.value.id == "erase_sweep"
    ]
    assert closes, "_on_drain closes the sweep"
    passed = [
        k
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "build_worker"
        for k in n.keywords
        if k.arg == "on_drain"
    ]
    assert len(passed) == 1
    assert isinstance(passed[0].value, ast.Name)
    assert passed[0].value.id == "_on_drain"
