"""V13-T1 — structural: the voice turn reads the graph ONLY through the gated callable.

The chat analog is ``runtime/tests/unit/test_task_no_bypass_composition.py``
(``ConversationLoop.__init__`` takes ``graph_retrieval``, never ``graph_store``).
This is the voice-layer structural criterion: the one place a voice turn can read
the graph is the injected, K4-gated ``graph_retrieval`` callable — never a raw
``GraphStore``. A raw store on the turn context would be a bare read outside the
gate (the safety regression V13 exists to prevent).

(V13-T2 flips ``test_task_no_bypass_composition.py``'s voice line from "graph-OFF,
safe" to "graph-ON only through the gated composition" — the deliberate
re-baseline. This test pins the voice turn container itself.)
"""

from __future__ import annotations

from dataclasses import fields

from persona_voice.model.graph import VoiceGraphComposition
from persona_voice.model.turn_context import VoiceTurnContext


def _field_names() -> set[str]:
    return {f.name for f in fields(VoiceTurnContext)}


def test_turn_context_exposes_graph_only_as_a_gated_callable() -> None:
    names = _field_names()
    # The gated seam is present …
    assert "graph_retrieval" in names
    assert "graph_surfacing_guidance" in names
    # … and there is NO raw store on the turn — never a bare read surface.
    assert not any("store" in n and "graph" in n for n in names)
    assert "graph_store" not in names


def test_turn_context_graph_retrieval_is_a_callable_not_a_store() -> None:
    # The field's annotation is the query->GraphContext callable (a value that
    # cannot be dereferenced into a raw store), matching chat's injected shape.
    ann = VoiceTurnContext.__annotations__["graph_retrieval"]
    assert "Callable" in str(ann)
    assert "GraphContext" in str(ann)
    assert "Store" not in str(ann)


def test_composition_returns_callables_never_the_store() -> None:
    # ``build_voice_graph_retrieval`` hands the runner two callables — the store it
    # is built over is captured in the closure, never surfaced on the result.
    comp_fields = {f.name for f in fields(VoiceGraphComposition)}
    assert comp_fields == {"retrieval", "surfacing_guidance"}
    assert "store" not in comp_fields
