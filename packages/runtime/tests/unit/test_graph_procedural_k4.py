"""PROCEDURAL is retrievable + K4-gated like every other kind (Spec K7, T8 / K7-D-12).

Proven over the REAL ``make_graph_retrieval`` path (not a bare retriever — the K5-R-4
discipline): a ``NodeKind.PROCEDURAL`` node injects when K4 permits it, and is
subtracted exactly like any categorized node when K4's allowlist excludes it. Gating
is category/id-based and kind-agnostic, so ``procedural`` needs no special handling —
this test is the proof that it gets none.
"""

from __future__ import annotations

from datetime import UTC, datetime

from persona.graph.config import GraphSettings
from persona.graph.fusion import HybridResult
from persona.graph.models import ConceptNode, NodeKind, NodeProvenance
from persona.schema.chunks import WriteSource
from persona_runtime.graph_selection import make_graph_retrieval

_NOW = datetime(2026, 6, 25, tzinfo=UTC)


def _proc(node_id: str, content: str, *, wellbeing_category: str | None = None) -> ConceptNode:
    return ConceptNode(
        id=node_id,
        node_kind=NodeKind.PROCEDURAL,  # "how this user likes the persona to behave"
        concept_name=node_id,
        content=content,
        wellbeing_category=wellbeing_category,
        distance=0.05,  # highly relevant — would inject if allowed
        provenance=(NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=_NOW),),
        created_at=_NOW,
    )


class _AllowlistRetriever:
    def __init__(self, nodes: list[ConceptNode]) -> None:
        self._nodes = nodes

    def retrieve(
        self,
        owner_id: str,  # noqa: ARG002 — contract
        query: str,  # noqa: ARG002 — contract
        *,
        allowlist: set[str] | None = None,
        top_k: int | None = None,  # noqa: ARG002 — contract
    ) -> list[HybridResult]:
        nodes = self._nodes if allowlist is None else [n for n in self._nodes if n.id in allowlist]
        return [HybridResult(node=n, score=0.5, rank=i, dense_rank=i) for i, n in enumerate(nodes)]


def _settings() -> GraphSettings:
    return GraphSettings(inject_similarity_floor=0.66)


def test_procedural_node_injects_when_permitted() -> None:
    retrieve = make_graph_retrieval(
        retriever=_AllowlistRetriever([_proc("p1", "Prefers terse, bulleted replies.")]),
        owner_provider=lambda: "user-A",
        settings=_settings(),
        now=lambda: _NOW,
        allowlist_provider=lambda _ctx: {"p1"},
    )
    contents = {item.content for item in retrieve("how should you answer?").items}
    assert "Prefers terse, bulleted replies." in contents  # storable + retrievable


def test_procedural_node_is_k4_gated_like_any_kind() -> None:
    nodes = [
        _proc("safe", "Likes worked examples."),
        _proc("sensitive", "Avoid this topic.", wellbeing_category="mental_health"),
    ]
    retrieve = make_graph_retrieval(
        retriever=_AllowlistRetriever(nodes),
        owner_provider=lambda: "user-A",
        settings=_settings(),
        now=lambda: _NOW,
        allowlist_provider=lambda _ctx: {"safe"},  # K4 subtracts the sensitive PROCEDURAL node
    )
    contents = {item.content for item in retrieve("q").items}
    assert "Likes worked examples." in contents
    assert "Avoid this topic." not in contents  # gated exactly like a categorized CONCEPT/FACT
