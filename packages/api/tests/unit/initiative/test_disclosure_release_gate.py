"""D-ORCH-11-18-u: the wellbeing gate honours a release only where it is asked to.

The user can say a conversation that carried a disclosure may be drawn on again. The gate is
strict by default, so a consumer that does not ask (the conversation as its own citation,
whose raw transcript holds the disclosure, and every task path) never sees the release. A
consumer that asks (a node's sources, a lens target's, the judge's context notes) gets a
released conversation lifted from BOTH halves of ``disclosed``: the fact, and the tagged nodes
that cite it. The release stops holding when a disclosure is recorded after it (the fact's
count moved), when a tagged node cites the conversation with a write made after it, or while
the conversation has messages synthesis has not read (a new disclosure could be in them).
"""

# ruff: noqa: ARG002 (duck-typed store and reader fakes)
from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.schema.chunks import WriteSource
from persona_api.initiative.disclosures import DisclosureState
from persona_api.initiative.wellbeing_gate import ConversationWellbeingGate

if TYPE_CHECKING:
    from collections.abc import Iterable

_BEFORE = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
_RELEASED_AT = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
_AFTER = datetime(2026, 11, 18, 15, 0, tzinfo=UTC)

_IN_FORCE = DisclosureState(disclosure_seq=2, released_seq=2, released_at=_RELEASED_AT)
_OVERTAKEN = DisclosureState(disclosure_seq=3, released_seq=2, released_at=_RELEASED_AT)


def _tagged(node_id: str, source: str, *, written_at: datetime = _BEFORE) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name=node_id,
        content=f"content of {node_id}",
        wellbeing_category="disordered_eating",
        provenance=(
            NodeProvenance(
                source=WriteSource.PERSONA_SELF,
                interaction_id=source,
                interaction_kind="conversation",
                written_at=written_at,
            ),
        ),
        created_at=_BEFORE,
    )


class _Graph:
    def __init__(self, *nodes: ConceptNode) -> None:
        self._nodes = nodes

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        return list(self._nodes)


class _Facts:
    """Facts and their counters, per owner."""

    def __init__(self, **by_owner: dict[str, DisclosureState]) -> None:
        self._by_owner = by_owner

    def disclosed(self, owner_id: str, refs: Iterable[str]) -> set[str]:
        return set(self._by_owner.get(owner_id, {})) & set(refs)

    def states(self, owner_id: str, refs: Iterable[str]) -> dict[str, DisclosureState]:
        wanted = set(refs)
        return {r: s for r, s in self._by_owner.get(owner_id, {}).items() if r in wanted}


def _gate(
    monkeypatch: pytest.MonkeyPatch,
    graph: _Graph,
    facts: _Facts,
    *,
    unsynthesised: set[str] | None = None,
    engine: Any = None,  # noqa: ANN401
    releases: bool = True,
) -> ConversationWellbeingGate:
    gate = ConversationWellbeingGate(
        graph,  # type: ignore[arg-type]
        object() if engine is None else engine,  # type: ignore[arg-type]
        disclosures=facts,
        releases=facts if releases else None,
    )
    stale = unsynthesised or set()
    monkeypatch.setattr(gate, "unsynthesised", lambda _owner, ids: stale & set(ids))
    return gate


def _released_conv_d(*nodes: ConceptNode) -> tuple[_Graph, _Facts]:
    return _Graph(*nodes), _Facts(u={"conv-d": _IN_FORCE})


def test_the_gate_is_strict_unless_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant 1: a release honoured by default reaches the raw transcript and the tasks."""
    graph, facts = _released_conv_d(_tagged("n-tag", "conv-d"))
    gate = _gate(monkeypatch, graph, facts)
    assert gate.disclosed("u", ["conv-d"]) == {"conv-d"}
    assert gate.gated("u", ["conv-d"], include_unsynthesised=True) == {"conv-d"}


def test_asked_a_release_lifts_both_the_fact_and_the_tagged_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph, facts = _released_conv_d(_tagged("n-tag", "conv-d"))
    gate = _gate(monkeypatch, graph, facts)
    assert gate.disclosed("u", ["conv-d"], honour_releases=True) == set()
    assert gate.gated("u", ["conv-d"], include_unsynthesised=True, honour_releases=True) == set()


def test_a_disclosure_recorded_after_the_release_holds_it_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _Graph(_tagged("n-tag", "conv-d"))
    gate = _gate(monkeypatch, graph, _Facts(u={"conv-d": _OVERTAKEN}))
    assert gate.disclosed("u", ["conv-d"], honour_releases=True) == {"conv-d"}
    assert gate.released("u", ["conv-d"]) == {}


def test_a_tagged_write_after_the_release_holds_it_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant 2: the safety net for a tagging path that skips the fact."""
    graph, facts = _released_conv_d(
        _tagged("n-old", "conv-d"), _tagged("n-new", "conv-d", written_at=_AFTER)
    )
    gate = _gate(monkeypatch, graph, facts)
    assert gate.released("u", ["conv-d"]) == {}
    assert gate.disclosed("u", ["conv-d"], honour_releases=True) == {"conv-d"}


def test_a_tagged_write_at_the_release_instant_is_covered(monkeypatch: pytest.MonkeyPatch) -> None:
    graph, facts = _released_conv_d(_tagged("n-tag", "conv-d", written_at=_RELEASED_AT))
    gate = _gate(monkeypatch, graph, facts)
    assert gate.released("u", ["conv-d"]) == {"conv-d": _RELEASED_AT}


def test_a_release_does_not_hold_while_synthesis_is_behind(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mutant 5: even where freshness is not asked for, a new disclosure could be unread."""
    graph, facts = _released_conv_d(_tagged("n-tag", "conv-d"))
    gate = _gate(monkeypatch, graph, facts, unsynthesised={"conv-d"})
    assert gate.disclosed("u", ["conv-d"], honour_releases=True) == {"conv-d"}
    assert gate.gated("u", ["conv-d"], include_unsynthesised=False, honour_releases=True) == {
        "conv-d"
    }


def test_no_release_reader_honours_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    graph, facts = _released_conv_d()
    gate = _gate(monkeypatch, graph, facts, releases=False)
    gate._releases = None  # noqa: SLF001 - the shape without a database
    assert gate.disclosed("u", ["conv-d"], honour_releases=True) == {"conv-d"}


def test_no_engine_honours_nothing_since_freshness_cannot_be_read() -> None:
    graph, facts = _released_conv_d()
    gate = ConversationWellbeingGate(graph, disclosures=facts, releases=facts)  # type: ignore[arg-type]
    assert gate.disclosed("u", ["conv-d"], honour_releases=True) == {"conv-d"}


def test_a_release_is_its_owners_and_its_conversations_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    graph = _Graph(_tagged("n-c", "conv-c"))
    facts = _Facts(u={"conv-d": _IN_FORCE, "conv-e": _OVERTAKEN}, other={"conv-c": _IN_FORCE})
    gate = _gate(monkeypatch, graph, facts)
    got = gate.disclosed("u", ["conv-c", "conv-d", "conv-e", "conv-x"], honour_releases=True)
    assert got == {"conv-c", "conv-e"}, "another owner's release never lifts this owner's gate"


def test_released_names_only_releases_in_force_with_their_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    held = DisclosureState(disclosure_seq=1, released_seq=None, released_at=None)
    facts = _Facts(u={"conv-d": _IN_FORCE, "conv-e": _OVERTAKEN, "conv-f": held})
    gate = _gate(monkeypatch, _Graph(), facts)
    assert gate.released("u", ["conv-d", "conv-e", "conv-f", "conv-x"]) == {"conv-d": _RELEASED_AT}
    assert gate.released("u", []) == {}
