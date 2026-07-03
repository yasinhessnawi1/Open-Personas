"""Unit tests for the K0 graph domain primitives (T1)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest
from persona.graph.models import (
    NODE_ID_INDEX_WIDTH,
    CanonicalEntity,
    ConceptNode,
    EntityAlias,
    LinkType,
    NodeKind,
    NodeProvenance,
    TypedLink,
    _compute_node_hash,
    make_edge_id,
    make_entity_id,
    make_node_id,
)
from persona.schema.chunks import PersonaChunk, WriteSource
from pydantic import ValidationError

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)


def _prov(**kw: object) -> NodeProvenance:
    base: dict[str, object] = {"source": WriteSource.PERSONA_SELF, "written_at": NOW}
    base.update(kw)
    return NodeProvenance(**base)  # type: ignore[arg-type]


def _node(**kw: object) -> ConceptNode:
    base: dict[str, object] = {
        "id": "u1::node::00000001",
        "node_kind": NodeKind.PREFERENCE,
        "concept_name": "learning style",
        "content": "prefers worked examples over abstract theory",
        "provenance": (_prov(),),
        "created_at": NOW,
    }
    base.update(kw)
    return ConceptNode(**base)  # type: ignore[arg-type]


# ----- enums ---------------------------------------------------------------


def test_node_kind_has_the_spec_kinds() -> None:
    # The seven K0 kinds + the K6 ``self`` marker + the K7 ``procedural`` kind
    # (Spec K7, K7-D-12 — learned interaction patterns; additive, no migration since
    # ``node_kind`` is free TEXT).
    assert {k.value for k in NodeKind} == {
        "concept",
        "fact",
        "preference",
        "trait",
        "goal",
        "circumstance",
        "entity",
        "self",
        "procedural",
    }


def test_link_type_has_the_four_typed_relationships() -> None:
    assert {lt.value for lt in LinkType} == {"semantic", "entity", "temporal", "causal"}


# ----- ConceptNode ---------------------------------------------------------


def test_concept_node_computes_content_hash_when_absent() -> None:
    node = _node()
    assert node.content_hash == _compute_node_hash(node.concept_name, node.content, node.metadata)


def test_concept_node_accepts_matching_supplied_hash() -> None:
    expected = _compute_node_hash(
        "learning style", "prefers worked examples over abstract theory", {}
    )
    node = _node(content_hash=expected)
    assert node.content_hash == expected


def test_concept_node_rejects_mismatched_content_hash() -> None:
    with pytest.raises(ValidationError, match="content_hash mismatch"):
        _node(content_hash="deadbeef")


def test_concept_node_hash_is_metadata_order_independent() -> None:
    a = _node(metadata={"a": "1", "b": "2"})
    b = _node(metadata={"b": "2", "a": "1"})
    assert a.content_hash == b.content_hash


def test_concept_node_is_frozen() -> None:
    node = _node()
    with pytest.raises(ValidationError):
        node.content = "mutated"  # type: ignore[misc]


def test_concept_node_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        _node(surrogate=42)  # surrogate is a storage concern, never on the model


def test_concept_node_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError, match="naive datetime"):
        _node(created_at=datetime(2026, 6, 21, 12, 0))  # noqa: DTZ001 — deliberate


def test_concept_node_normalises_created_at_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    node = _node(created_at=datetime(2026, 6, 21, 14, 0, tzinfo=plus_two))
    assert node.created_at == NOW
    assert node.created_at.tzinfo == UTC


def test_concept_node_requires_at_least_one_provenance_entry() -> None:
    with pytest.raises(ValidationError):
        _node(provenance=())


def test_concept_node_accumulation_trail_holds_multiple_entries() -> None:
    trail = (_prov(reason="created"), _prov(reason="extended"))
    node = _node(provenance=trail)
    assert len(node.provenance) == 2
    assert node.provenance[1].reason == "extended"


def test_concept_node_metadata_defaults_empty_and_wellbeing_optional() -> None:
    node = _node()
    assert node.metadata == {}
    assert node.wellbeing_category is None
    assert node.distance is None


def test_concept_node_carries_wellbeing_category_when_tagged() -> None:
    assert _node(wellbeing_category="mental_health").wellbeing_category == "mental_health"


def test_concept_node_is_a_sibling_not_a_subclass_of_persona_chunk() -> None:
    # D-K0-5: the type system reads the isolation — a graph node is not a chunk.
    assert not issubclass(ConceptNode, PersonaChunk)
    assert not isinstance(_node(), PersonaChunk)


# ----- NodeProvenance ------------------------------------------------------


def test_node_provenance_rejects_naive_written_at() -> None:
    with pytest.raises(ValidationError, match="naive datetime"):
        NodeProvenance(source=WriteSource.SYSTEM, written_at=datetime(2026, 6, 21, 12, 0))  # noqa: DTZ001


def test_node_provenance_carries_grounding_and_interaction() -> None:
    prov = _prov(persona_id="tutor", interaction_id="conv-7", grounding="user said so")
    assert prov.persona_id == "tutor"
    assert prov.interaction_id == "conv-7"
    assert prov.grounding == "user said so"


def test_node_provenance_superseded_content_defaults_none_and_records_prior() -> None:
    assert _prov().superseded_content is None
    assert _prov(superseded_content="worked at X").superseded_content == "worked at X"


def test_node_provenance_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        NodeProvenance(source=WriteSource.SYSTEM, written_at=NOW, bogus=1)  # type: ignore[call-arg]


# ----- TypedLink -----------------------------------------------------------


def test_typed_link_valid_construction() -> None:
    link = TypedLink(
        id=make_edge_id("u1::node::00000001", "u1::node::00000002", LinkType.SEMANTIC),
        src_node_id="u1::node::00000001",
        dst_node_id="u1::node::00000002",
        link_type=LinkType.SEMANTIC,
        weight=0.87,
        created_at=NOW,
    )
    assert link.link_type is LinkType.SEMANTIC
    assert link.weight == 0.87


def test_typed_link_rejects_self_loop() -> None:
    with pytest.raises(ValidationError, match="cannot connect a node to itself"):
        TypedLink(
            id="x",
            src_node_id="u1::node::00000001",
            dst_node_id="u1::node::00000001",
            link_type=LinkType.ENTITY,
            created_at=NOW,
        )


def test_typed_link_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError, match="naive datetime"):
        TypedLink(
            id="x",
            src_node_id="a",
            dst_node_id="b",
            link_type=LinkType.CAUSAL,
            created_at=datetime(2026, 6, 21, 12, 0),  # noqa: DTZ001
        )


def test_typed_link_forbids_extra_fields() -> None:
    with pytest.raises(ValidationError):
        TypedLink(
            id="x",
            src_node_id="a",
            dst_node_id="b",
            link_type=LinkType.TEMPORAL,
            created_at=NOW,
            bogus=1,  # type: ignore[call-arg]
        )


# ----- EntityAlias / CanonicalEntity --------------------------------------


def test_entity_alias_confidence_bounds() -> None:
    assert EntityAlias(surface="the GP", confidence=0.9).confidence == 0.9
    assert EntityAlias(surface="the GP").confidence is None
    with pytest.raises(ValidationError):
        EntityAlias(surface="x", confidence=1.5)


def test_canonical_entity_defaults_empty_aliases() -> None:
    ent = CanonicalEntity(id="u1::entity::00000001", canonical_name="Dr. Hansen", created_at=NOW)
    assert ent.aliases == ()


def test_canonical_entity_threads_aliases() -> None:
    ent = CanonicalEntity(
        id="u1::entity::00000001",
        canonical_name="Dr. Hansen",
        aliases=(EntityAlias(surface="my doctor"), EntityAlias(surface="the GP", confidence=0.8)),
        created_at=NOW,
    )
    assert {a.surface for a in ent.aliases} == {"my doctor", "the GP"}


def test_canonical_entity_rejects_naive_created_at() -> None:
    with pytest.raises(ValidationError, match="naive datetime"):
        CanonicalEntity(id="e", canonical_name="X", created_at=datetime(2026, 6, 21, 12, 0))  # noqa: DTZ001


# ----- id helpers ----------------------------------------------------------


def test_make_node_id_format_and_width() -> None:
    assert make_node_id("u1", 1) == "u1::node::00000001"
    assert len(make_node_id("u1", 1).rsplit("::", 1)[-1]) == NODE_ID_INDEX_WIDTH


def test_make_entity_id_format() -> None:
    assert make_entity_id("u1", 42) == "u1::entity::00000042"


@pytest.mark.parametrize("factory", [make_node_id, make_entity_id])
def test_id_helpers_reject_negative_index(factory: object) -> None:
    with pytest.raises(ValueError, match="non-negative"):
        factory("u1", -1)  # type: ignore[operator]


def test_make_edge_id_is_deterministic_and_idempotent() -> None:
    a = make_edge_id("n1", "n2", LinkType.SEMANTIC)
    b = make_edge_id("n1", "n2", LinkType.SEMANTIC)
    assert a == b == "n1::semantic::n2"


def test_make_edge_id_distinguishes_link_type_and_direction() -> None:
    assert make_edge_id("n1", "n2", LinkType.SEMANTIC) != make_edge_id("n1", "n2", LinkType.CAUSAL)
    assert make_edge_id("n1", "n2", LinkType.ENTITY) != make_edge_id("n2", "n1", LinkType.ENTITY)
