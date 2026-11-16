"""Seed a node's version row for a test (R9-284 round 12 LOW-5).

Production writes a version only inside ``PostgresGraphBackend.supersede_node``, under the
node CAS and in the same transaction as the overwrite. Tests that need a version row of a
node as it stands, without superseding it, copy the live row here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.graph._schema import graph_node_versions, graph_nodes
from sqlalchemy import insert, literal, select

if TYPE_CHECKING:
    from sqlalchemy import Engine

_COPIED = (
    "owner_id",
    "node_kind",
    "concept_name",
    "content",
    "metadata",
    "wellbeing_category",
    "embedding",
    "embedding_model",
    "content_hash",
    "provenance",
)


def snapshot_node_version(
    engine: Engine,
    owner_id: str,
    node_id: str,
    *,
    valid_at: datetime,
    invalid_at: datetime,
    invalidated_by: str | None,
) -> int | None:
    """Copy the node's current row into ``graph_node_versions``; its key, or None if absent."""
    target = graph_node_versions.c
    source = select(
        *(graph_nodes.c[name] for name in _COPIED),
        graph_nodes.c.id,
        literal(valid_at, target.valid_at.type),
        literal(invalid_at, target.invalid_at.type),
        literal(invalidated_by, target.invalidated_by.type),
        literal(datetime.now(UTC), target.invalidated_at.type),
    ).where(graph_nodes.c.id == node_id, graph_nodes.c.owner_id == owner_id)
    columns = [
        *_COPIED,
        "node_id",
        "valid_at",
        "invalid_at",
        "invalidated_by",
        "invalidated_at",
    ]
    statement = (
        insert(graph_node_versions)
        .from_select(columns, source)
        .returning(graph_node_versions.c.version_key)
    )
    with engine.begin() as conn:
        key = conn.execute(statement).scalar_one_or_none()
    return None if key is None else int(key)
