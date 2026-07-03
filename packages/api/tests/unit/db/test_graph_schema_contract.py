"""Core-view ↔ api-schema contract for the graph tables (Spec K0, T4 / D-K0-3).

persona-core's transport (``persona.graph._schema``) defines its own view of the
three graph tables; the api ``db.models`` owns the canonical (migrated) schema.
This asserts the two never drift — the split-home guard, mirroring the Spec 07
``test_core_transport_view_matches_migrated_schema``. Pure in-memory; no DB.

Also locks the two cross-cutting invariants the graph tables must keep: they are
present in the canonical metadata but EXCLUDED from the community SQLite build
(they carry pgvector/tsvector columns).
"""

from __future__ import annotations

import pytest
from persona.graph._schema import EMBEDDING_DIM as CORE_DIM
from persona.graph._schema import graph_consolidation_markers as core_markers
from persona.graph._schema import graph_edges as core_edges
from persona.graph._schema import graph_entities as core_entities
from persona.graph._schema import graph_node_entities as core_node_entities
from persona.graph._schema import graph_node_versions as core_versions
from persona.graph._schema import graph_nodes as core_nodes
from persona_api.db.community import build_community_metadata
from persona_api.db.models import EMBEDDING_DIM as API_DIM
from persona_api.db.models import (
    graph_consolidation_markers,
    graph_edges,
    graph_entities,
    graph_node_entities,
    graph_node_versions,
    graph_nodes,
    metadata,
)

_PAIRS = [
    ("graph_nodes", core_nodes, graph_nodes),
    ("graph_edges", core_edges, graph_edges),
    ("graph_entities", core_entities, graph_entities),
    ("graph_node_entities", core_node_entities, graph_node_entities),
    # Spec K7 additions (K7-D-1 / K7-D-4).
    ("graph_node_versions", core_versions, graph_node_versions),
    ("graph_consolidation_markers", core_markers, graph_consolidation_markers),
]
_GRAPH_TABLES = {
    "graph_nodes",
    "graph_edges",
    "graph_entities",
    "graph_node_entities",
    "graph_node_versions",
    "graph_consolidation_markers",
}


@pytest.mark.parametrize(("name", "core_t", "api_t"), _PAIRS, ids=[p[0] for p in _PAIRS])
def test_core_view_columns_match_api_schema(name: str, core_t: object, api_t: object) -> None:
    core_cols = {c.name for c in core_t.c}  # type: ignore[attr-defined]
    api_cols = {c.name for c in api_t.c}  # type: ignore[attr-defined]
    assert core_cols == api_cols, (
        f"{name}: core view diverged from api schema — "
        f"core-only={core_cols - api_cols}, api-only={api_cols - core_cols}"
    )


@pytest.mark.parametrize(("name", "core_t", "api_t"), _PAIRS, ids=[p[0] for p in _PAIRS])
def test_core_view_column_types_match_api_schema(name: str, core_t: object, api_t: object) -> None:
    core_types = {c.name: str(c.type) for c in core_t.c}  # type: ignore[attr-defined]
    api_types = {c.name: str(c.type) for c in api_t.c}  # type: ignore[attr-defined]
    assert core_types == api_types, f"{name}: column type drift core↔api"


def test_embedding_dim_agreement() -> None:
    assert CORE_DIM == API_DIM == 384
    assert core_nodes.c.embedding.type.dim == graph_nodes.c.embedding.type.dim == 384
    assert core_entities.c.name_embedding.type.dim == 384
    assert graph_entities.c.name_embedding.type.dim == 384
    # The version row preserves the node's embedding byte-exact (K7-D-1).
    assert core_versions.c.embedding.type.dim == graph_node_versions.c.embedding.type.dim == 384


def test_graph_tables_present_in_canonical_metadata() -> None:
    assert set(metadata.tables) >= _GRAPH_TABLES


def test_graph_tables_excluded_from_community_sqlite() -> None:
    # They carry pgvector Vector + tsvector columns (or FK them) — Postgres-only.
    community = set(build_community_metadata().tables)
    assert community.isdisjoint(_GRAPH_TABLES)
