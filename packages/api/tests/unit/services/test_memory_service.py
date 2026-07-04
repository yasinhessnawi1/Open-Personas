"""Unit tests for ``memory_service`` projection (Spec K5, Group A) — no DB.

A fake GraphStore exercises the window + detail projections: seed vs focus, degree
from induced edges, K4 ``wellbeing_category`` passthrough, evolution from the
provenance trail, and the not-found → ``None`` contract. Search is covered in the
store/route integration tests (it drives the real HybridRetriever against Postgres).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.graph.models import ConceptNode, LinkType, NodeKind, NodeProvenance, TypedLink
from persona.schema.chunks import WriteSource
from persona_api.services import memory_service

if TYPE_CHECKING:
    from collections.abc import Sequence

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _node(
    nid: str,
    *,
    kind: NodeKind = NodeKind.FACT,
    wellbeing: str | None = None,
    provenance: Sequence[NodeProvenance] | None = None,
) -> ConceptNode:
    trail = tuple(provenance or [NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW)])
    return ConceptNode(
        id=nid,
        node_kind=kind,
        concept_name=nid.title(),
        content=f"about {nid}",
        wellbeing_category=wellbeing,
        provenance=trail,
        created_at=NOW,
    )


def _edge(src: str, dst: str, link_type: LinkType = LinkType.SEMANTIC) -> TypedLink:
    return TypedLink(
        id=f"{src}->{dst}", src_node_id=src, dst_node_id=dst, link_type=link_type, created_at=NOW
    )


class _FakeStore:
    """The slice of the GraphStore surface the read projections call."""

    def __init__(
        self, nodes: list[ConceptNode], edges: list[TypedLink], *, total: int | None = None
    ) -> None:
        self._nodes = {n.id: n for n in nodes}
        self._edges = edges
        self._total = total if total is not None else len(nodes)

    def count_nodes(self, owner_id: str) -> int:  # noqa: ARG002 — owner scope is the fixture's
        return self._total

    def seed_nodes(self, owner_id: str, *, limit: int) -> list[ConceptNode]:  # noqa: ARG002
        return list(self._nodes.values())[:limit]

    def get_node(self, owner_id: str, node_id: str) -> ConceptNode | None:  # noqa: ARG002
        return self._nodes.get(node_id)

    def edges_among(self, owner_id: str, node_ids: Sequence[str]) -> list[TypedLink]:  # noqa: ARG002
        s = set(node_ids)
        return [e for e in self._edges if e.src_node_id in s and e.dst_node_id in s]

    def neighbors(  # protocol-shaped fake; owner/link_types unused here
        self,
        owner_id: str,  # noqa: ARG002
        node_id: str,
        *,
        link_types: object = None,  # noqa: ARG002
        limit: int,
    ) -> list[tuple[TypedLink, ConceptNode]]:
        out: list[tuple[TypedLink, ConceptNode]] = []
        for e in self._edges:
            if e.src_node_id == node_id:
                out.append((e, self._nodes[e.dst_node_id]))
            elif e.dst_node_id == node_id:
                out.append((e, self._nodes[e.src_node_id]))
        return out[:limit]


def _star() -> _FakeStore:
    nodes = [_node("hub"), _node("leaf1"), _node("leaf2", wellbeing="grief")]
    edges = [_edge("hub", "leaf1"), _edge("hub", "leaf2", LinkType.CAUSAL)]
    return _FakeStore(nodes, edges, total=42)


def test_seed_window_is_marked_and_reports_full_total() -> None:
    store = _star()
    win = memory_service.build_window(store, "u1")  # type: ignore[arg-type]
    assert win.is_seed is True
    assert win.focus_id is None
    assert win.total_nodes == 42  # noqa: PLR2004 — the full tally, not the window size
    assert {n.id for n in win.nodes} == {"hub", "leaf1", "leaf2"}
    assert {(link.src_node_id, link.dst_node_id) for link in win.links} == {
        ("hub", "leaf1"),
        ("hub", "leaf2"),
    }


def test_window_degree_counts_induced_edges() -> None:
    win = memory_service.build_window(_star(), "u1")  # type: ignore[arg-type]
    by_id = {n.id: n for n in win.nodes}
    assert by_id["hub"].degree == 2  # noqa: PLR2004 — linked to both leaves
    assert by_id["leaf1"].degree == 1


def test_window_carries_wellbeing_mark_on_summary() -> None:
    win = memory_service.build_window(_star(), "u1")  # type: ignore[arg-type]
    leaf2 = next(n for n in win.nodes if n.id == "leaf2")
    assert leaf2.wellbeing_category == "grief"


def test_focus_window_centres_on_the_node() -> None:
    win = memory_service.build_window(_star(), "u1", focus_id="hub")  # type: ignore[arg-type]
    assert win.is_seed is False
    assert win.focus_id == "hub"
    assert "hub" in {n.id for n in win.nodes}
    assert {"leaf1", "leaf2"} <= {n.id for n in win.nodes}


def test_focus_window_unknown_node_is_empty_but_keeps_total() -> None:
    win = memory_service.build_window(_star(), "u1", focus_id="ghost")  # type: ignore[arg-type]
    assert win.nodes == []
    assert win.links == []
    assert win.total_nodes == 42  # noqa: PLR2004


def test_node_detail_projects_provenance_evolution_and_typed_links() -> None:
    trail = [
        NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW, reason="first learned"),
        NodeProvenance(
            source=WriteSource.USER,
            written_at=NOW,
            reason="user correction",
            superseded_content="the old wording",
        ),
    ]
    store = _FakeStore([_node("hub", provenance=trail), _node("leaf1")], [_edge("hub", "leaf1")])
    detail = memory_service.node_detail(store, "u1", "hub")  # type: ignore[arg-type]
    assert detail is not None
    assert detail.origin.source == "persona_self"  # the first contribution = where it came from
    assert len(detail.evolution) == 2  # noqa: PLR2004 — the full accumulation trail
    assert detail.evolution[1].superseded_content == "the old wording"
    assert detail.links[0].direction == "out"
    assert detail.links[0].neighbor.id == "leaf1"


def test_node_detail_carries_wellbeing_mark() -> None:
    store = _FakeStore([_node("grief_node", wellbeing="grief")], [])
    detail = memory_service.node_detail(store, "u1", "grief_node")  # type: ignore[arg-type]
    assert detail is not None
    assert detail.wellbeing_category == "grief"


def test_node_detail_missing_returns_none() -> None:
    assert memory_service.node_detail(_star(), "u1", "ghost") is None  # type: ignore[arg-type]


# --- Defect 2: provenance attribution (R-K5-PROV-PERSONA / R-K5-OPEN-CONV) -------


def test_node_detail_resolves_persona_name_from_persona_id() -> None:
    """The panel shows "learned by <persona>" — persona_id resolves to a name (owner-scoped)."""
    prov = [
        NodeProvenance(
            source=WriteSource.SYSTEM,
            persona_id="persona-1",
            interaction_id="conv-1",
            interaction_kind="conversation",
            written_at=NOW,
        )
    ]
    store = _FakeStore([_node("hub", provenance=prov)], [])
    detail = memory_service.node_detail(
        store,  # type: ignore[arg-type]
        "u1",
        "hub",
        persona_name_resolver=lambda pid: "Aria" if pid == "persona-1" else None,
    )
    assert detail is not None
    assert detail.origin.persona_name == "Aria"


def test_node_detail_persona_name_none_without_resolver() -> None:
    """Absent a resolver (or an unresolved id) the name stays None → source-based fallback."""
    prov = [
        NodeProvenance(source=WriteSource.SYSTEM, persona_id="persona-1", written_at=NOW),
    ]
    store = _FakeStore([_node("hub", provenance=prov)], [])
    detail = memory_service.node_detail(store, "u1", "hub")  # type: ignore[arg-type]
    assert detail is not None
    assert detail.origin.persona_name is None


def test_node_detail_conversation_link_for_conversation_source() -> None:
    """A chat/voice-sourced memory exposes a conversation_id the panel can open."""
    prov = [
        NodeProvenance(
            source=WriteSource.SYSTEM,
            interaction_id="conv-42",
            interaction_kind="conversation",
            written_at=NOW,
        )
    ]
    store = _FakeStore([_node("hub", provenance=prov)], [])
    detail = memory_service.node_detail(store, "u1", "hub")  # type: ignore[arg-type]
    assert detail is not None
    assert detail.origin.conversation_id == "conv-42"


def test_node_detail_no_conversation_link_for_run_source() -> None:
    """A run-sourced memory's id is a run_id — must NOT be offered as a /chat link (404 guard)."""
    prov = [
        NodeProvenance(
            source=WriteSource.SYSTEM,
            interaction_id="run-7",
            interaction_kind="agentic_run",
            written_at=NOW,
        )
    ]
    store = _FakeStore([_node("hub", provenance=prov)], [])
    detail = memory_service.node_detail(store, "u1", "hub")  # type: ignore[arg-type]
    assert detail is not None
    assert detail.origin.conversation_id is None
    assert detail.origin.interaction_id == "run-7"  # still shown, just not linkable
