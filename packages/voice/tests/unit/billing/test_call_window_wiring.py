"""The billable window is actually handed to the session that bills (R9-202).

The window itself is well tested and the session uses it correctly, but both of
those stay true if the composition root stops passing it: ``AgentSession`` takes
``billing_window`` as an optional argument, and ``None`` means "bill the session
object's lifetime", which is the original defect. Dropping one keyword in
``build_agent_session`` restores an eleven hour empty room costing a real user
$13.34, and every other test in this package stays green while it does.

``build_agent_session`` needs a live database and a LiveKit room, so it has no
behavioural test to catch that. These read its source instead. Same reason, same
shape as the daily-cap guard next door: the wiring is the feature.
"""

from __future__ import annotations

import ast
from pathlib import Path

from persona_voice.agent import runner as runner_module


def _runner_tree() -> ast.Module:
    return ast.parse(Path(runner_module.__file__).read_text(encoding="utf-8"))


def _calls_to(tree: ast.Module, name: str) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
    ]


def test_the_session_is_built_with_the_billable_window() -> None:
    """Every ``AgentSession`` the runner builds carries a real window, not a default."""
    builds = _calls_to(_runner_tree(), "AgentSession")

    assert builds, "the runner no longer builds an AgentSession; this guard needs rewriting"
    for call in builds:
        passed = {kw.arg: kw.value for kw in call.keywords}
        assert "billing_window" in passed, (
            f"AgentSession built without billing_window at runner.py:{call.lineno}. "
            "The session then bills the whole lifetime of the session object, which is "
            "R9-202: a room that outlives its conversation charges the gap per minute."
        )
        value = passed["billing_window"]
        assert isinstance(value, ast.Name), (
            f"billing_window at runner.py:{call.lineno} is not a named window. "
            "A literal None here is the defect with the keyword present."
        )


def test_the_turn_meter_feeds_the_window() -> None:
    """The fallback end of conversation mark has a producer at the composition root.

    Without ``on_turn_committed`` the window only learns from a room departure, so a
    worker drained before any departure event arrives falls back to wall clock. That
    is exactly the production shape: several calls ending at one identical second.
    """
    builds = _calls_to(_runner_tree(), "VoiceTurnBillingMeter")

    assert builds, "the runner no longer builds a turn billing meter; this guard needs rewriting"
    for call in builds:
        kwargs = {kw.arg for kw in call.keywords}
        assert "on_turn_committed" in kwargs, (
            f"VoiceTurnBillingMeter built without on_turn_committed at runner.py:{call.lineno}, "
            "so a committed turn never marks the end of the conversation."
        )
