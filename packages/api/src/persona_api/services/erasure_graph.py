"""The graph half of erasing a persona (R9-284, D-R9-284-erase ruling 4).

The concept graph belongs to the user, not to a persona: nodes are owner scoped and carry a
provenance trail with one entry per contribution, each naming the persona it came through and
the grounding it rests on (which is content). So erasing a persona does not delete the user's
graph. It deletes what only that persona contributed and removes that persona from the rest:

- a node whose every entry is the persona is deleted (its edges, versions and entity links go
  with it by foreign key);
- a node with entries from anywhere else (another persona, the user's own edits, synthesis with
  no persona) keeps those entries and loses the persona's;
- the user's SELF node is never deleted (K7-D-7): where every entry is the persona's, the
  entries stay as dated stubs with nothing of the persona left in them;
- version snapshots get the same treatment, and a snapshot that was only the persona's goes;
- an edge the persona asserted goes; an entity the persona established loses that provenance,
  and goes if no node links to it any more.

Postgres only: the legacy community edition has no graph. The planning is pure and unit
tested; the SQL runs in CI (tests/integration/test_erase_persona_graph_postgres.py).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from persona.graph.models import make_self_node_id
from sqlalchemy import cast, delete, exists, select, update
from sqlalchemy.dialects.postgresql import JSONB

from persona_api.db.models import graph_edges, graph_entities, graph_node_entities
from persona_api.db.models import graph_node_versions as versions_t
from persona_api.db.models import graph_nodes as nodes_t

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

__all__ = ["GraphPlan", "erase_persona_graph", "plan_trails"]

Trail = list[dict[str, Any]]

# What a stub keeps of an entry: when it was written and by what kind of source.
_STUB_KEYS = ("source", "written_at")


@dataclass(frozen=True)
class GraphPlan:
    """What to do with a set of trails: which keys go, and which get a new trail."""

    doomed: list[str] = field(default_factory=list)
    rewrites: dict[str, Trail] = field(default_factory=dict)


def _is_persona(entry: object, persona_id: str) -> bool:
    return isinstance(entry, Mapping) and entry.get("persona_id") == persona_id


def _stub(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in _STUB_KEYS if key in entry}


def plan_trails(
    trails: Iterable[tuple[str, str, Trail]], *, persona_id: str, self_node_id: str
) -> GraphPlan:
    """Plan the persona out of ``(key, node_id, trail)`` rows: nodes, or version snapshots.

    A trail with nothing of the persona is left alone. One with other entries keeps them. One
    that was only the persona's is doomed, except the SELF node's, whose entries become stubs.
    """
    plan = GraphPlan()
    for key, node_id, trail in trails:
        if not any(_is_persona(entry, persona_id) for entry in trail):
            continue
        kept = [entry for entry in trail if not _is_persona(entry, persona_id)]
        if kept:
            plan.rewrites[key] = kept
        elif node_id == self_node_id:
            plan.rewrites[key] = [_stub(entry) for entry in trail]
        else:
            plan.doomed.append(key)
    return plan


def _naming(column: Any, persona_id: str) -> Any:  # noqa: ANN401 - SQL expressions
    """A JSONB trail (a list of entries) with an entry from this persona."""
    return cast(column, JSONB).contains([{"persona_id": persona_id}])


def _nodes(conn: Connection, owner_id: str, persona_id: str) -> dict[str, int]:
    rows = conn.execute(
        select(nodes_t.c.id, nodes_t.c.provenance).where(
            nodes_t.c.owner_id == owner_id, _naming(nodes_t.c.provenance, persona_id)
        )
    ).all()
    plan = plan_trails(
        ((r.id, r.id, list(r.provenance)) for r in rows),
        persona_id=persona_id,
        self_node_id=make_self_node_id(owner_id),
    )
    if plan.doomed:
        conn.execute(
            delete(nodes_t).where(nodes_t.c.owner_id == owner_id, nodes_t.c.id.in_(plan.doomed))
        )
    for node_id, trail in plan.rewrites.items():
        conn.execute(
            update(nodes_t)
            .where(nodes_t.c.owner_id == owner_id, nodes_t.c.id == node_id)
            .values(provenance=trail)
        )
    return {"nodes_deleted": len(plan.doomed), "nodes_stripped": len(plan.rewrites)}


def _versions(conn: Connection, owner_id: str, persona_id: str) -> dict[str, int]:
    rows = conn.execute(
        select(versions_t.c.version_key, versions_t.c.node_id, versions_t.c.provenance).where(
            versions_t.c.owner_id == owner_id, _naming(versions_t.c.provenance, persona_id)
        )
    ).all()
    plan = plan_trails(
        ((str(r.version_key), r.node_id, list(r.provenance)) for r in rows),
        persona_id=persona_id,
        self_node_id=make_self_node_id(owner_id),
    )
    keys = {str(r.version_key): r.version_key for r in rows}
    if plan.doomed:
        conn.execute(
            delete(versions_t).where(
                versions_t.c.owner_id == owner_id,
                versions_t.c.version_key.in_([keys[k] for k in plan.doomed]),
            )
        )
    for key, trail in plan.rewrites.items():
        conn.execute(
            update(versions_t)
            .where(versions_t.c.owner_id == owner_id, versions_t.c.version_key == keys[key])
            .values(provenance=trail)
        )
    return {"versions_deleted": len(plan.doomed), "versions_stripped": len(plan.rewrites)}


def _asserted(column: Any, persona_id: str) -> Any:  # noqa: ANN401 - SQL expressions
    """A single JSONB entry (an edge's or an entity's) from this persona."""
    return cast(column, JSONB).contains({"persona_id": persona_id})


def _edges_and_entities(conn: Connection, owner_id: str, persona_id: str) -> dict[str, int]:
    edges = conn.execute(
        delete(graph_edges).where(
            graph_edges.c.owner_id == owner_id, _asserted(graph_edges.c.provenance, persona_id)
        )
    ).rowcount
    established = (
        graph_entities.c.owner_id == owner_id,
        _asserted(graph_entities.c.provenance, persona_id),
    )
    linked = exists().where(
        graph_node_entities.c.owner_id == owner_id,
        graph_node_entities.c.entity_id == graph_entities.c.id,
    )
    entities = conn.execute(delete(graph_entities).where(*established, ~linked)).rowcount
    unattributed = conn.execute(
        update(graph_entities).where(*established).values(provenance=None)
    ).rowcount
    return {
        "edges_deleted": edges,
        "entities_deleted": entities,
        "entities_unattributed": unattributed,
    }


def erase_persona_graph(engine: Engine, *, owner_id: str, persona_id: str) -> dict[str, int]:
    """Take the persona out of its owner's graph, in one transaction; return the counts.

    Nodes first, so the edges and entity links of deleted nodes go by cascade before the
    entities are judged by what still links to them. A no-op on an engine with no graph.
    """
    if engine.dialect.name != "postgresql":
        return {}
    with engine.begin() as conn:
        return {
            **_nodes(conn, owner_id, persona_id),
            **_versions(conn, owner_id, persona_id),
            **_edges_and_entities(conn, owner_id, persona_id),
        }
