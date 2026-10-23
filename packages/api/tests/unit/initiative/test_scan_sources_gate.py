"""R9-249 review (HIGH-1): the scan's graph reader and the subject rule follow SOURCES.

The production shape of B3: K2 tags the disordered-eating node, but beside it extracts
an UNTAGGED event node ("The user is attending Mia's dinner party on Thursday") with the
same source conversation. Before this fix the untagged node reached the scan and passed
the subject rule, so a model citing only it raised the disclosure's context unprompted.
Unit level here (fake store, stub gate); the SQL halves and the real pipeline are proven
on Postgres in ``tests/integration/test_r9_249_wellbeing_gate.py``.
"""

# ruff: noqa: ARG002, ANN401 (duck-typed store and gate fakes)
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from persona.graph.models import (
    ConceptNode,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    make_edge_id,
)
from persona.initiative import MAX_CITED_NODE_READS, CitationKind, GroundingCitation
from persona.schema.chunks import WriteSource
from persona_api.initiative.pipeline_wiring import ApiWellbeingSubjectCheck
from persona_api.initiative.readers import ApiScanGraphReader

_NOW = datetime(2026, 9, 26, 5, 0, tzinfo=UTC)


def _node(node_id: str, *, source: str | None, wellbeing: str | None = None) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.FACT,
        concept_name=node_id,
        content=f"content of {node_id}",
        wellbeing_category=wellbeing,
        provenance=(
            NodeProvenance(
                source=WriteSource.PERSONA_SELF,
                interaction_id=source,
                interaction_kind="conversation" if source else None,
                written_at=_NOW,
            ),
        ),
        created_at=_NOW,
    )


def _edge(src: str, dst: str) -> TypedLink:
    return TypedLink(
        id=make_edge_id(src, dst, LinkType.TEMPORAL),
        src_node_id=src,
        dst_node_id=dst,
        link_type=LinkType.TEMPORAL,
        provenance=NodeProvenance(source=WriteSource.SYSTEM, written_at=_NOW),
        created_at=_NOW,
    )


_FLAGGED = _node("n-flagged", source="conv-d", wellbeing="disordered_eating")
_DINNER = _node("n-dinner", source="conv-d")  # untagged, learned from the disclosure
_TEACHER = _node("n-teacher", source="conv-c")
_LOOSE = _node("n-loose", source=None)


class _Store:
    def __init__(self, pool: list[ConceptNode], links: dict[str, list[ConceptNode]]) -> None:
        self._pool = pool
        self._links = links
        self._all = {n.id: n for n in [*pool, _FLAGGED, *(t for ts in links.values() for t in ts)]}
        self.got: list[str] = []

    def recent_nodes(self, owner_id: str, *, limit: int) -> list[ConceptNode]:
        return self._pool

    def neighbors(
        self, owner_id: str, node_id: str, **_: Any
    ) -> list[tuple[TypedLink, ConceptNode]]:
        return [(_edge(node_id, t.id), t) for t in self._links.get(node_id, [])]

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        return [_FLAGGED]

    def get_node(self, owner_id: str, node_id: str) -> ConceptNode | None:
        self.got.append(node_id)
        return self._all.get(node_id)


class _Gate:
    """Gates ``gated`` refs; records every call (the reader must ask ONCE per scan)."""

    def __init__(self, gated: set[str]) -> None:
        self._gated = gated
        self.calls: list[tuple[set[str], bool]] = []

    def gated(
        self,
        owner_id: str,
        refs: Any,
        *,
        include_unsynthesised: bool,
        flagged: Any = None,
    ) -> set[str]:
        wanted = set(refs)
        self.calls.append((wanted, include_unsynthesised))
        return self._gated & wanted


def test_the_pool_drops_nodes_and_lens_targets_learned_from_a_gated_conversation() -> None:
    store = _Store([_DINNER, _TEACHER, _LOOSE], links={"n-teacher": [_DINNER, _LOOSE]})
    gate = _Gate({"conv-d"})
    pool = ApiScanGraphReader(store, gate).noticing_pool("u", limit=30)  # type: ignore[arg-type]
    assert [n.id for n in pool] == ["n-teacher", "n-loose"]
    assert [link.target_id for link in pool[0].links] == ["n-loose"]
    assert gate.calls == [({"conv-d", "conv-c"}, True)]  # one question per scan, scan mode


def test_the_subject_rule_gates_an_untagged_node_learned_from_a_disclosure() -> None:
    store = _Store([], links={})
    check = ApiWellbeingSubjectCheck(store, engine=None)  # type: ignore[arg-type]
    cited = (
        GroundingCitation(kind=CitationKind.NODE, ref="n-dinner"),
        GroundingCitation(kind=CitationKind.NODE, ref="n-teacher"),
    )
    store._all.update({"n-dinner": _DINNER, "n-teacher": _TEACHER})  # noqa: SLF001
    gated = check.tagged_refs("u", cited, include_unsynthesised=False)
    assert gated == {"n-dinner"}  # learned from conv-d, which a flagged node came from
    one = (GroundingCitation(kind=CitationKind.NODE, ref="n-teacher"),)
    assert check.tagged_refs("u", one, include_unsynthesised=False) == set()
    flagged = (GroundingCitation(kind=CitationKind.NODE, ref="n-flagged"),)
    assert check.tagged_refs("u", flagged, include_unsynthesised=False) == {"n-flagged"}
    conv = (GroundingCitation(kind=CitationKind.CONVERSATION, ref="conv-d"),)
    assert check.tagged_refs("u", conv, include_unsynthesised=False) == {"conv-d"}


def test_the_subject_rule_reads_a_bounded_number_of_nodes_and_fails_closed_past_it() -> None:
    store = _Store([], links={})
    check = ApiWellbeingSubjectCheck(store, engine=None)  # type: ignore[arg-type]
    many = tuple(
        GroundingCitation(kind=CitationKind.NODE, ref=f"n-{i}")
        for i in range(MAX_CITED_NODE_READS + 1)
    )
    assert check.tagged_refs("u", many, include_unsynthesised=False) == {c.ref for c in many}
    assert store.got == []  # no read past the bound: the whole subject is gated instead
    few = many[:MAX_CITED_NODE_READS]
    assert check.tagged_refs("u", few, include_unsynthesised=False) == set()
    assert sorted(store.got) == sorted(c.ref for c in few)
    assert MAX_CITED_NODE_READS == 3
