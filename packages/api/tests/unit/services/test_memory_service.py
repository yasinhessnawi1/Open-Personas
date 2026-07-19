"""Unit tests for ``memory_service`` projection (Spec K5, Group A) — no DB.

A fake GraphStore exercises the window + detail projections: seed vs focus, degree
from induced edges, K4 ``wellbeing_category`` passthrough, evolution from the
provenance trail, and the not-found → ``None`` contract. Search is covered in the
store/route integration tests (it drives the real HybridRetriever against Postgres).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.graph.models import (
    ConceptNode,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    make_edge_id,
)
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
    created_at: datetime = NOW,
) -> ConceptNode:
    trail = tuple(
        provenance or [NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=created_at)]
    )
    return ConceptNode(
        id=nid,
        node_kind=kind,
        concept_name=nid.title(),
        content=f"about {nid}",
        wellbeing_category=wellbeing,
        provenance=trail,
        created_at=created_at,
    )


def _edge(src: str, dst: str, link_type: LinkType = LinkType.SEMANTIC) -> TypedLink:
    return TypedLink(
        id=f"{src}->{dst}", src_node_id=src, dst_node_id=dst, link_type=link_type, created_at=NOW
    )


class _FakeStore:
    """The slice of the GraphStore surface the read projections call."""

    def __init__(
        self,
        nodes: list[ConceptNode],
        edges: list[TypedLink],
        *,
        entity_edges: list[TypedLink] | None = None,
        total: int | None = None,
    ) -> None:
        self._nodes = {n.id: n for n in nodes}
        self._edges = edges
        self._entity_edges = entity_edges or []
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

    def entity_edges_among(self, owner_id: str, node_ids: Sequence[str]) -> list[TypedLink]:  # noqa: ARG002
        """D-K12-A: the on-the-fly ENTITY edges the join table would expand to."""
        s = set(node_ids)
        return [e for e in self._entity_edges if e.src_node_id in s and e.dst_node_id in s]

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


def test_window_includes_entity_edges_from_the_join_table() -> None:
    """D-K12-A: entity relations (join-table-only, never in ``edges_among``) surface on the window.

    Today only ``edges_among``'s materialised semantic/temporal/causal edges show; this proves
    ``_assemble_window`` also unions in ``entity_edges_among``'s on-the-fly ENTITY edges.
    """
    nodes = [_node("hub"), _node("leaf1"), _node("leaf2")]
    semantic_edge = _edge("hub", "leaf1")
    entity_edge = _edge("hub", "leaf2", LinkType.ENTITY)
    store = _FakeStore(nodes, [semantic_edge], entity_edges=[entity_edge])
    win = memory_service.build_window(store, "u1")  # type: ignore[arg-type]
    assert {(link.src_node_id, link.dst_node_id, link.link_type) for link in win.links} == {
        ("hub", "leaf1", "semantic"),
        ("hub", "leaf2", "entity"),
    }


def test_window_dedupes_the_same_entity_relationship_from_diverging_edge_ids() -> None:
    """The SAME A-B entity relationship is synthesised with a DIFFERENT id depending on which
    source produced it: ``neighbors`` (feeding a focus window's ``extra_edges``) anchors the id
    at the FOCUS node (``make_edge_id(focus_id, neighbor_id, ENTITY)``), while
    ``entity_edges_among`` canonicalises lexicographically (``node_id < node_id``) — and
    ``make_edge_id`` is order-dependent, so the two conventions only happen to agree when the
    focus is already the lexicographically-smaller id. Here the focus ("leaf1") is NOT the
    smaller one ("hub" < "leaf1"), so the two sources genuinely mint different ids for one
    relationship. The assembled window must still collapse this to ONE edge — real degree, not
    double-counted — regardless of which source produced it or which direction its id encodes.
    """
    nodes = [_node("leaf1"), _node("hub")]
    focus_anchored_id = make_edge_id("leaf1", "hub", LinkType.ENTITY)
    lexicographic_id = make_edge_id("hub", "leaf1", LinkType.ENTITY)
    assert focus_anchored_id != lexicographic_id  # the divergence this test exists to catch

    # neighbors()-shaped: what build_window's ``extra_edges`` receives for the focus node.
    focus_anchored_edge = TypedLink(
        id=focus_anchored_id,
        src_node_id="leaf1",
        dst_node_id="hub",
        link_type=LinkType.ENTITY,
        created_at=NOW,
    )
    # entity_edges_among()-shaped: the SAME relationship, lexicographically-canonicalised id.
    lexicographic_edge = TypedLink(
        id=lexicographic_id,
        src_node_id="hub",
        dst_node_id="leaf1",
        link_type=LinkType.ENTITY,
        created_at=NOW,
    )
    store = _FakeStore(nodes, [focus_anchored_edge], entity_edges=[lexicographic_edge])
    win = memory_service.build_window(store, "u1", focus_id="leaf1")  # type: ignore[arg-type]
    assert len(win.links) == 1
    by_id = {n.id: n for n in win.nodes}
    assert by_id["leaf1"].degree == 1
    assert by_id["hub"].degree == 1


# --- D-K12-B: read-time-derived TEMPORAL edges --------------------------------


def test_temporal_edge_derived_between_entity_sharing_nodes_only() -> None:
    """Two window nodes sharing a canonical entity, with distinct timestamps, get exactly
    one TEMPORAL edge (older→newer) — an unrelated third node gets none.
    """
    older = _node("older", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = _node("newer", created_at=datetime(2026, 2, 1, tzinfo=UTC))
    unrelated = _node("unrelated", created_at=datetime(2026, 3, 1, tzinfo=UTC))
    entity_edge = _edge("older", "newer", LinkType.ENTITY)
    store = _FakeStore([older, newer, unrelated], [], entity_edges=[entity_edge])
    win = memory_service.build_window(store, "u1")  # type: ignore[arg-type]
    temporal_links = [link for link in win.links if link.link_type == "temporal"]
    assert len(temporal_links) == 1
    assert (temporal_links[0].src_node_id, temporal_links[0].dst_node_id) == ("older", "newer")
    assert all(
        link.src_node_id != "unrelated" and link.dst_node_id != "unrelated" for link in win.links
    )


def test_temporal_edge_derived_between_same_interaction_nodes() -> None:
    """Same-interaction sharing (no shared entity) also groups nodes for temporal chaining."""
    prov_a = [
        NodeProvenance(
            source=WriteSource.PERSONA_SELF,
            interaction_id="conv-1",
            written_at=datetime(2026, 1, 1, tzinfo=UTC),
        )
    ]
    prov_b = [
        NodeProvenance(
            source=WriteSource.PERSONA_SELF,
            interaction_id="conv-1",
            written_at=datetime(2026, 2, 1, tzinfo=UTC),
        )
    ]
    older = _node("older", provenance=prov_a, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = _node("newer", provenance=prov_b, created_at=datetime(2026, 2, 1, tzinfo=UTC))
    store = _FakeStore([older, newer], [])
    win = memory_service.build_window(store, "u1")  # type: ignore[arg-type]
    temporal_links = [link for link in win.links if link.link_type == "temporal"]
    assert len(temporal_links) == 1
    assert (temporal_links[0].src_node_id, temporal_links[0].dst_node_id) == ("older", "newer")


def test_temporal_edges_chain_consecutive_not_all_pairs() -> None:
    """A 3-node shared-context group chains A→B→C — not the all-pairs A→B, A→C, B→C."""
    a = _node("a", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    b = _node("b", created_at=datetime(2026, 2, 1, tzinfo=UTC))
    c = _node("c", created_at=datetime(2026, 3, 1, tzinfo=UTC))
    entity_edges = [_edge("a", "b", LinkType.ENTITY), _edge("b", "c", LinkType.ENTITY)]
    store = _FakeStore([a, b, c], [], entity_edges=entity_edges)
    win = memory_service.build_window(store, "u1")  # type: ignore[arg-type]
    temporal_pairs = {
        (link.src_node_id, link.dst_node_id) for link in win.links if link.link_type == "temporal"
    }
    assert temporal_pairs == {("a", "b"), ("b", "c")}


def test_temporal_edge_derivation_suppressed_by_an_asserted_temporal_edge_either_direction() -> (
    None
):
    """An LLM-asserted TEMPORAL edge is materialised newer→older (the just-merged
    candidate points at its chronologically earlier target) — the OPPOSITE order from
    a derived edge (older→newer). ``make_edge_id`` is order-dependent, so the two would
    mint DIFFERENT ids for the same pair and NOT collapse via by-id dedup — this must
    not render as two contradictory temporal arrows between "older" and "newer".

    Regression for the realistic direction: earlier coverage only exercised an asserted
    edge in the (coincidental) older→newer order, which the by-id dedup handled by
    accident and missed that the real merge.py direction is reversed.
    """
    older = _node("older", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    newer = _node("newer", created_at=datetime(2026, 2, 1, tzinfo=UTC))
    # Realistic direction: the newer node is the just-merged candidate, asserting a
    # temporal edge back at the older target — src=newer, dst=older.
    asserted_temporal = TypedLink(
        id=make_edge_id("newer", "older", LinkType.TEMPORAL),
        src_node_id="newer",
        dst_node_id="older",
        link_type=LinkType.TEMPORAL,
        created_at=NOW,
        weight=0.9,
    )
    entity_edge = _edge("older", "newer", LinkType.ENTITY)
    store = _FakeStore([older, newer], [asserted_temporal], entity_edges=[entity_edge])
    win = memory_service.build_window(store, "u1")  # type: ignore[arg-type]
    temporal_links = [link for link in win.links if link.link_type == "temporal"]
    assert len(temporal_links) == 1
    assert (temporal_links[0].src_node_id, temporal_links[0].dst_node_id) == ("newer", "older")
    assert temporal_links[0].weight == 0.9  # noqa: PLR2004 — the asserted edge's data won, kept


def test_temporal_edge_ordered_by_earliest_written_at_not_created_at() -> None:
    """D-K12-B: temporal order follows the fact's ASSERTED time — the EARLIEST
    ``provenance.written_at`` — not the DB row's ``created_at``. A consolidated /
    backfilled node's ``created_at`` can postdate the event it describes, which would
    give a wrong before/after if ``created_at`` drove the ordering.

    "a" is created AFTER "b" (a's ``created_at`` is later) but its provenance trail was
    first WRITTEN before "b"'s — so the correct temporal edge is a→b, the OPPOSITE of
    what ``created_at`` ordering would produce (b→a). "a" also carries a SECOND,
    later-written provenance entry, proving the anchor is the trail's EARLIEST
    ``written_at`` (a min over the whole trail), not just its first or last entry.
    """
    prov_a = [
        NodeProvenance(source=WriteSource.SYSTEM, written_at=datetime(2026, 4, 1, tzinfo=UTC)),
        NodeProvenance(source=WriteSource.SYSTEM, written_at=datetime(2026, 1, 1, tzinfo=UTC)),
    ]
    prov_b = [
        NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=datetime(2026, 2, 1, tzinfo=UTC))
    ]
    a = _node("a", provenance=prov_a, created_at=datetime(2026, 3, 1, tzinfo=UTC))
    b = _node("b", provenance=prov_b, created_at=datetime(2026, 2, 5, tzinfo=UTC))
    entity_edge = _edge("a", "b", LinkType.ENTITY)
    store = _FakeStore([a, b], [], entity_edges=[entity_edge])
    win = memory_service.build_window(store, "u1")  # type: ignore[arg-type]
    temporal_links = [link for link in win.links if link.link_type == "temporal"]
    assert len(temporal_links) == 1
    assert (temporal_links[0].src_node_id, temporal_links[0].dst_node_id) == ("a", "b")


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
