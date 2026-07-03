"""Unit tests for the Spec K7 T1 model + config additions (K7-D-1/-6/-8/-9/-12).

Pure in-memory: the new frozen models (``NodeVersion``, ``TypedLink`` windows),
the widened enums/DTOs (``MergeAction``, ``MergeOutcome``, ``KnowledgeCandidate``
slots), the ``GraphProtectedNodeError`` type, and the ``GraphSettings`` K7 knobs +
per-kind salience helpers. Schema/DB behaviour is covered by the integration tests.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from persona.graph.config import GraphSettings
from persona.graph.errors import GraphError, GraphProtectedNodeError
from persona.graph.models import LinkType, NodeKind, NodeProvenance, NodeVersion, TypedLink
from persona.graph.protocol import KnowledgeCandidate, MergeAction, MergeOutcome
from persona.schema.chunks import WriteSource
from pydantic import ValidationError

NOW = datetime(2026, 6, 21, 12, 0, tzinfo=UTC)
LATER = NOW + timedelta(days=30)


def _prov(**kw: object) -> NodeProvenance:
    base: dict[str, object] = {"source": WriteSource.PERSONA_SELF, "written_at": NOW}
    base.update(kw)
    return NodeProvenance(**base)  # type: ignore[arg-type]


# ----- NodeKind.PROCEDURAL (K7-D-12) ---------------------------------------


def test_procedural_kind_exists() -> None:
    assert NodeKind.PROCEDURAL.value == "procedural"


# ----- TypedLink valid-time windows (K7-D-1) -------------------------------


def test_typed_link_defaults_leave_window_open_and_unset() -> None:
    link = TypedLink(
        id="a::semantic::b",
        src_node_id="a",
        dst_node_id="b",
        link_type=LinkType.SEMANTIC,
        created_at=NOW,
    )
    # Old constructors stay byte-compatible: the window fields default to unset.
    assert link.valid_at is None
    assert link.invalid_at is None
    assert link.invalidated_by is None
    assert link.invalidated_at is None


def test_typed_link_window_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        TypedLink(
            id="a::temporal::b",
            src_node_id="a",
            dst_node_id="b",
            link_type=LinkType.TEMPORAL,
            created_at=NOW,
            valid_at=LATER,
            invalid_at=NOW,  # closes before it opens
        )


def test_typed_link_window_ordered_against_created_at_when_valid_at_unset() -> None:
    # valid_at unset ⇒ the window starts at created_at; invalid_at must be after it.
    with pytest.raises(ValidationError):
        TypedLink(
            id="a::temporal::b",
            src_node_id="a",
            dst_node_id="b",
            link_type=LinkType.TEMPORAL,
            created_at=LATER,
            invalid_at=NOW,
        )


def test_typed_link_naive_window_datetimes_rejected() -> None:
    with pytest.raises(ValidationError):
        TypedLink(
            id="a::causal::b",
            src_node_id="a",
            dst_node_id="b",
            link_type=LinkType.CAUSAL,
            created_at=NOW,
            valid_at=datetime(2026, 6, 21, 12, 0),  # noqa: DTZ001 — deliberately naive
        )


# ----- NodeVersion (K7-D-1) ------------------------------------------------


def test_node_version_round_trips_and_recomputes_nothing() -> None:
    v = NodeVersion(
        version_id="42",
        node_id="u1::node::00000001",
        node_kind=NodeKind.CIRCUMSTANCE,
        concept_name="home city",
        content="lives in Bergen",
        content_hash="deadbeef",
        provenance=(_prov(),),
        valid_at=NOW,
        invalid_at=LATER,
        invalidated_by="u1::interaction::9",
    )
    assert v.content == "lives in Bergen"
    assert v.valid_at == NOW
    assert v.invalid_at == LATER
    assert v.invalidated_at is None


def test_node_version_window_must_be_ordered() -> None:
    with pytest.raises(ValidationError):
        NodeVersion(
            version_id="1",
            node_id="n",
            node_kind=NodeKind.FACT,
            concept_name="c",
            content="x",
            content_hash="h",
            provenance=(_prov(),),
            valid_at=LATER,
            invalid_at=NOW,
        )


def test_node_version_is_frozen_and_forbids_extra() -> None:
    v = NodeVersion(
        version_id="1",
        node_id="n",
        node_kind=NodeKind.FACT,
        concept_name="c",
        content="x",
        content_hash="h",
        provenance=(_prov(),),
        valid_at=NOW,
        invalid_at=LATER,
    )
    with pytest.raises(ValidationError):
        v.content = "z"
    with pytest.raises(ValidationError):
        NodeVersion(
            version_id="1",
            node_id="n",
            node_kind=NodeKind.FACT,
            concept_name="c",
            content="x",
            content_hash="h",
            provenance=(_prov(),),
            valid_at=NOW,
            invalid_at=LATER,
            bogus=1,  # type: ignore[call-arg]
        )


# ----- MergeAction / MergeOutcome widening (K7-D-8) ------------------------


def test_merge_action_widened_additively() -> None:
    assert MergeAction.EVOLVED.value == "evolved"
    assert MergeAction.UNCHANGED.value == "unchanged"


def test_merge_outcome_superseded_version_id_defaults_none() -> None:
    out = MergeOutcome(action=MergeAction.CREATED, node_id="n")
    assert out.superseded_version_id is None
    out2 = MergeOutcome(action=MergeAction.EVOLVED, node_id="n", superseded_version_id="7")
    assert out2.superseded_version_id == "7"


# ----- KnowledgeCandidate additive slots (K7-D-8) --------------------------


def test_candidate_slots_default_and_accept_values() -> None:
    cand = KnowledgeCandidate(
        concept_name="c",
        content="x",
        node_kind=NodeKind.FACT,
        provenance=_prov(),
        valid_at=LATER,
        close_link_ids=("a::temporal::b",),
    )
    assert cand.valid_at == LATER
    assert cand.close_link_ids == ("a::temporal::b",)


# ----- GraphProtectedNodeError (K7-D-7) ------------------------------------


def test_protected_node_error_is_a_graph_error() -> None:
    err = GraphProtectedNodeError("nope", context={"node_id": "u1::self", "op": "delete"})
    assert isinstance(err, GraphError)
    assert err.context["op"] == "delete"


# ----- GraphSettings K7 knobs (K7-D-4/-6/-9) -------------------------------


def test_k7_settings_defaults() -> None:
    s = GraphSettings()
    assert s.consolidation_enabled is True
    assert s.consolidation_min_cluster_size == 2
    assert s.salience_default == 1.0
    assert s.salience_floor == 0.0
    assert s.salience_cap == 10.0
    assert s.hnsw_m == 16
    assert s.hnsw_ef_construction == 200
    assert s.hnsw_iterative_scan == "relaxed_order"


def test_salience_floor_cannot_exceed_cap() -> None:
    with pytest.raises(ValidationError):
        GraphSettings(salience_floor=5.0, salience_cap=1.0)


def test_per_kind_disuse_helpers() -> None:
    s = GraphSettings()
    # TRAIT never fades by disuse; CIRCUMSTANCE fastest; others fall back to base.
    assert s.disuse_delta_for("trait") == 0.0
    assert s.disuse_delta_for("circumstance") == 0.25
    assert s.disuse_delta_for("fact") == s.salience_disuse_delta
    assert s.disuse_grace_for("trait") == 0
    assert s.disuse_grace_for("fact") == s.salience_disuse_grace_epochs


def test_consolidation_min_cluster_size_must_exceed_one() -> None:
    with pytest.raises(ValidationError):
        GraphSettings(consolidation_min_cluster_size=1)
