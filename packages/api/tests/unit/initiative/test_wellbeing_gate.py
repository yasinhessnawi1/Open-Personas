"""R9-249: the shared conversation wellbeing gate (unit level; the SQL half is integration).

One definition serves A5's scan reader and pipeline subject rule and A7's event door. The
"disclosed" half (a flagged node sourced from the conversation) needs only the graph store
and is proven here with a fake; the "unsynthesised" half runs SQL over the synthesis
markers and is proven on Postgres in ``tests/integration/test_r9_249_wellbeing_gate.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.schema.chunks import WriteSource
from persona_api.events.candidate_producer import ApiEventWellbeingCheck
from persona_api.initiative.wellbeing_gate import SYNTHESISED_ROLES, ConversationWellbeingGate

_NOW = datetime(2026, 9, 26, 5, 0, tzinfo=UTC)


def _flagged(node_id: str, *sources: str | None) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.CIRCUMSTANCE,
        concept_name=node_id,
        content="A sensitive disclosure.",
        wellbeing_category="disordered_eating",
        provenance=tuple(
            NodeProvenance(
                source=WriteSource.SYSTEM,
                interaction_id=source,
                interaction_kind="conversation" if source else None,
                written_at=_NOW,
            )
            for source in sources
        ),
        created_at=_NOW,
    )


class _Store:
    def __init__(self, *nodes: ConceptNode) -> None:
        self._nodes = list(nodes)
        self.reads = 0

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:  # noqa: ARG002
        self.reads += 1
        return self._nodes


def test_a_conversation_is_disclosed_when_a_flagged_node_was_sourced_from_it() -> None:
    gate = ConversationWellbeingGate(
        _Store(_flagged("n1", "conv-a", None), _flagged("n2", "conv-b"))
    )  # type: ignore[arg-type]
    assert gate.disclosed("u", ["conv-a", "conv-c", "n1"]) == {"conv-a"}
    assert gate.disclosed("u", []) == set()


def test_the_caller_may_pass_the_flagged_set_to_avoid_a_second_read() -> None:
    store = _Store(_flagged("n1", "conv-a"))
    gate = ConversationWellbeingGate(store)  # type: ignore[arg-type]
    flagged = store.flagged_nodes("u")
    assert gate.gated("u", ["conv-a"], include_unsynthesised=False, flagged=flagged) == {"conv-a"}
    assert store.reads == 1


def test_the_freshness_half_refuses_to_run_without_an_engine() -> None:
    gate = ConversationWellbeingGate(_Store())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="needs a database engine"):
        gate.gated("u", ["conv-a"], include_unsynthesised=True)
    assert gate.gated("u", ["conv-a"], include_unsynthesised=False) == set()


def _a7_original_rule(nodes: list[ConceptNode], ref: str) -> bool:
    """A7's layer (a) as it stood before R9-249 (copied here as the parity reference)."""
    for node in nodes:  # noqa: SIM110 (the original loop, verbatim)
        if any(p.interaction_id == ref for p in node.provenance):
            return True
    return False


def test_a7s_event_door_matches_the_shared_gate_and_its_original_rule() -> None:
    """No drift from the gate it delegates to, and no change from the rule it replaced,
    on every shape: several flagged nodes, several provenance entries, missing ids, run
    ids, a node id as the ref, an empty ref, and no flagged nodes at all."""
    worlds = [
        [],
        [_flagged("n1", "conv-a")],
        [_flagged("n1", None, "conv-a"), _flagged("n2", "run-7", "conv-b")],
        [_flagged("n3", None)],
    ]
    refs = ("conv-a", "conv-b", "conv-c", "run-7", "n1", "task-1", "")
    for nodes in worlds:
        store = _Store(*nodes)
        check = ApiEventWellbeingCheck(store)  # type: ignore[arg-type]
        gate = ConversationWellbeingGate(store)  # type: ignore[arg-type]
        for ref in refs:
            door = check.is_gated_subject("u", "conversation", ref)
            assert door is bool(gate.gated("u", [ref], include_unsynthesised=False)), ref
            assert door is _a7_original_rule(nodes, ref), (nodes, ref)


def test_the_freshness_count_uses_the_roles_synthesis_reads() -> None:
    """``synthesised_up_to`` counts synthesis's transcript rows; the gate must count the same."""
    from persona_api.jobs.handlers import synthesis

    assert SYNTHESISED_ROLES == synthesis._TRANSCRIPT_ROLES  # noqa: SLF001
