"""Unit + contract tests for the K0 graph ports (T2).

The stub implementations double as a structural conformance check: each is
assigned to its protocol-typed variable, so ``mypy --strict`` verifies the
signatures match the port. ``runtime_checkable`` isinstance checks the runtime
surface; the Pydantic DTO tests lock the K2 write-path boundary shapes.
"""
# ruff: noqa: ARG002 — protocol-conformance stubs deliberately ignore their args

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.graph.models import (
    CanonicalEntity,
    ConceptNode,
    EntityAlias,
    LinkType,
    NodeKind,
    NodeProvenance,
    NodeVersion,
    TypedLink,
)
from persona.graph.protocol import (
    EntityCandidate,
    EntityRegistry,
    GraphIndex,
    GraphStore,
    KnowledgeCandidate,
    MergeAction,
    MergeOutcome,
    ProposedLink,
    ResolutionDecision,
    ResolutionVerdict,
    UpdateIntent,
)
from persona.schema.chunks import WriteSource
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
PROV = NodeProvenance(source=WriteSource.PERSONA_SELF, written_at=NOW)


# ----- boundary DTOs -------------------------------------------------------


def test_knowledge_candidate_minimal_and_defaults() -> None:
    cand = KnowledgeCandidate(
        concept_name="learning style",
        content="prefers worked examples",
        node_kind=NodeKind.PREFERENCE,
        provenance=PROV,
    )
    assert cand.entity_ids == ()
    assert cand.proposed_links == ()
    assert cand.wellbeing_category is None
    assert cand.update_intent is UpdateIntent.NONE
    assert cand.target_node_id is None
    # K7 additive contract slots default to unset (K7-D-8) — K2 payloads stay
    # byte-compatible.
    assert cand.valid_at is None
    assert cand.close_link_ids == ()


def test_knowledge_candidate_carries_k2_write_path_fields() -> None:
    cand = KnowledgeCandidate(
        concept_name="job",
        content="left the fintech job",
        node_kind=NodeKind.CIRCUMSTANCE,
        entity_ids=("u1::entity::00000001",),
        proposed_links=(
            ProposedLink(
                target_node_id="u1::node::00000009", link_type=LinkType.CAUSAL, reason="burnout"
            ),
        ),
        wellbeing_category="work_stress",
        provenance=PROV,
        update_intent=UpdateIntent.CONTRADICT,
        target_node_id="u1::node::00000003",
    )
    assert cand.update_intent is UpdateIntent.CONTRADICT
    assert cand.proposed_links[0].link_type is LinkType.CAUSAL
    assert cand.wellbeing_category == "work_stress"


def test_knowledge_candidate_is_frozen_and_forbids_extra() -> None:
    cand = KnowledgeCandidate(
        concept_name="x", content="y", node_kind=NodeKind.FACT, provenance=PROV
    )
    with pytest.raises(ValidationError):
        cand.content = "z"
    with pytest.raises(ValidationError):
        KnowledgeCandidate(
            concept_name="x",
            content="y",
            node_kind=NodeKind.FACT,
            provenance=PROV,
            bogus=1,  # type: ignore[call-arg]
        )


def test_merge_outcome_shape() -> None:
    out = MergeOutcome(action=MergeAction.EXTENDED, node_id="u1::node::00000001")
    assert out.action is MergeAction.EXTENDED
    assert out.created_link_ids == ()
    assert out.entity_ids == ()


def test_resolution_verdict_three_shapes() -> None:
    merge = ResolutionVerdict(
        decision=ResolutionDecision.MERGE, canonical_id="u1::entity::00000001"
    )
    sep = ResolutionVerdict(decision=ResolutionDecision.SEPARATE)
    amb = ResolutionVerdict(
        decision=ResolutionDecision.AMBIGUOUS,
        candidates=(
            EntityCandidate(
                entity_id="u1::entity::00000001", canonical_name="Dr. Hansen", score=0.86
            ),
        ),
    )
    assert merge.canonical_id == "u1::entity::00000001"
    assert sep.candidates == ()
    assert amb.candidates[0].score == 0.86


def test_boundary_enums_values() -> None:
    assert {x.value for x in UpdateIntent} == {"none", "update", "contradict"}
    # K7 widened MergeAction additively (K7-D-8): EVOLVED (a supersede) + UNCHANGED
    # (the observable idempotent no-op).
    assert {x.value for x in MergeAction} == {
        "created",
        "extended",
        "evolved",
        "unchanged",
    }
    assert {x.value for x in ResolutionDecision} == {"merge", "separate", "ambiguous"}


# ----- stub implementations (also the mypy --strict conformance proof) -----


class _StubIndex:
    def add(self, *, surrogate: int, vector: Sequence[float]) -> None: ...
    def replace(self, *, surrogate: int, vector: Sequence[float]) -> None: ...
    def remove(self, surrogate: int) -> bool:
        return False

    def contains(self, surrogate: int) -> bool:
        return False

    def search(
        self, *, query_vector: Sequence[float], top_k: int, allowlist: Sequence[int] | None = None
    ) -> list[tuple[int, float]]:
        return []

    def rebuild(self, items: Iterable[tuple[int, Sequence[float]]]) -> None: ...
    def persist(self) -> None: ...


class _StubRegistry:
    def resolve(self, owner_id: str, mention: str) -> ResolutionVerdict:
        return ResolutionVerdict(decision=ResolutionDecision.SEPARATE)

    def get_entity(self, owner_id: str, entity_id: str) -> CanonicalEntity | None:
        return None

    def create_entity(
        self,
        owner_id: str,
        *,
        canonical_name: str,
        aliases: tuple[EntityAlias, ...] = (),
        provenance: NodeProvenance | None = None,
    ) -> CanonicalEntity:
        return CanonicalEntity(
            id="u1::entity::00000001", canonical_name=canonical_name, created_at=NOW
        )

    def add_alias(self, owner_id: str, entity_id: str, alias: EntityAlias) -> None: ...


class _StubStore:
    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome:
        return MergeOutcome(action=MergeAction.CREATED, node_id="u1::node::00000001")

    def correct_node(
        self, owner_id: str, node_id: str, new_content: str, *, interaction_id: str | None = None
    ) -> None: ...

    def delete_node(self, owner_id: str, node_id: str) -> bool:
        return False

    def get_node(
        self, owner_id: str, node_id: str, *, as_of: datetime | None = None
    ) -> ConceptNode | None:
        return None

    def get_node_versions(self, owner_id: str, node_id: str) -> list[NodeVersion]:
        return []

    def get_self_node(self, owner_id: str) -> ConceptNode | None:
        return None

    def get_or_create_self_node(
        self, owner_id: str, *, display_name: str | None = None
    ) -> ConceptNode:
        raise NotImplementedError

    def search_dense(
        self, owner_id: str, query: str, top_k: int, *, allowlist: set[str] | None = None
    ) -> list[ConceptNode]:
        return []

    def search_fts(self, owner_id: str, query: str, top_k: int) -> list[ConceptNode]:
        return []

    def flagged_nodes(self, owner_id: str) -> list[ConceptNode]:
        return []

    def node_ids_for_owner(self, owner_id: str) -> list[str]:
        return []

    def count_nodes(self, owner_id: str) -> int:
        return 0

    def seed_nodes(self, owner_id: str, *, limit: int) -> list[ConceptNode]:
        return []

    def edges_among(self, owner_id: str, node_ids: Sequence[str]) -> list[TypedLink]:
        return []

    def entity_edges_among(self, owner_id: str, node_ids: Sequence[str]) -> list[TypedLink]:
        return []

    def recent_nodes(self, owner_id: str, *, limit: int) -> list[ConceptNode]:
        return []

    def neighbors(
        self,
        owner_id: str,
        node_id: str,
        *,
        link_types: set[LinkType] | None = None,
        limit: int,
    ) -> list[tuple[TypedLink, ConceptNode]]:
        return []

    def get_embeddings(self, owner_id: str, node_ids: Sequence[str]) -> dict[str, list[float]]:
        return {}

    def record_recall(self, owner_id: str, node_ids: Sequence[str]) -> None: ...

    def rebuild_index(self, owner_id: str) -> None: ...


# Structural conformance: mypy --strict fails here if a signature drifts.
_index: GraphIndex = _StubIndex()
_registry: EntityRegistry = _StubRegistry()
_store: GraphStore = _StubStore()


def test_stubs_satisfy_runtime_checkable_protocols() -> None:
    assert isinstance(_StubIndex(), GraphIndex)
    assert isinstance(_StubRegistry(), EntityRegistry)
    assert isinstance(_StubStore(), GraphStore)


def test_incomplete_impl_is_not_a_graph_store() -> None:
    class _Partial:
        def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome:
            return MergeOutcome(action=MergeAction.CREATED, node_id="x")

    assert not isinstance(_Partial(), GraphStore)


def test_protocols_are_runtime_checkable() -> None:
    # A plain object satisfies none of the ports.
    assert not isinstance(object(), GraphIndex)
    assert not isinstance(object(), EntityRegistry)
    assert not isinstance(object(), GraphStore)
