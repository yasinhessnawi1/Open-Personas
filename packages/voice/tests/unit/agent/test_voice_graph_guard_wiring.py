"""The voice runner turns the R9-284 graph write guard on (review mutant M8).

``build_agent_session`` composes the whole voice session, too much to run for one keyword, so
the runner's source is read: every graph construction in it must pass the literal ``True``.
(``persona_guard`` is required, LOW-1, so leaving it out already fails at construction.)
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

_CONSTRUCTORS = {"PostgresGraphBackend", "build_graph_store"}


def test_the_voice_runner_builds_its_graph_store_guarded() -> None:
    spec = importlib.util.find_spec("persona_voice.agent.runner")
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

    assert guards == [True], "one graph store, guarded"
