"""The graph half of erasing a persona (R9-284, D-R9-284-erase ruling 4).

The concept graph belongs to the user, not to a persona: nodes are owner scoped and carry a
provenance trail with one entry per contribution, each naming the persona it came through and
the grounding it rests on (which is content). So erasing a persona does not delete the user's
graph. It deletes what only that persona contributed and removes that persona from the rest:

- a node whose every entry is the persona is deleted (its edges, versions and entity links go
  with it by foreign key). Consolidation's own bookkeeping (a SYSTEM entry with no persona and
  a ``consolidation:`` reason, added when near-duplicates merge) is not independent
  provenance (review H3), so a node the persona alone fed is deleted however often it merged;
- a node with entries from anywhere else (another persona, the user's own edits, synthesis with
  no persona) keeps those entries and loses the persona's;
- the user's SELF node is never deleted (K7-D-7): where every entry is the persona's, they stay
  as dated stubs with nothing of the persona left in them, and consolidation's entries go;
- version snapshots get the same treatment, and a snapshot that was only the persona's goes;
- an edge the persona asserted goes; an entity it established loses that provenance, and goes
  if no node links to it any more.

Consolidation also moves things between nodes, and that is undone where it would strand them:

- a member merged INTO a node being deleted (another persona's or the user's own) is unmerged
  first, as ``ConsolidationPass.unmerge`` does: its closed edges reopen, ``merged_into`` and its
  merge record are cleared (review M1). Otherwise it would point at nothing and vanish from
  retrieval;
- a member being deleted that was merged into a node that stays takes back what it gave: the
  edges mirrored onto the canonical, the entity links copied to it, and its name in the
  canonical's aliases (on SELF too, review H3).

Postgres only: the legacy community edition has no graph. The planning is pure and unit tested;
the SQL runs in CI (tests/integration/test_erase_persona_graph_postgres.py).
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from persona.graph.models import _compute_node_hash, make_self_node_id
from sqlalchemy import cast, delete, exists, or_, select, update
from sqlalchemy.dialects.postgresql import JSONB

from persona_api.db.models import graph_edges, graph_entities, graph_node_entities
from persona_api.db.models import graph_node_versions as versions_t
from persona_api.db.models import graph_nodes as nodes_t

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

__all__ = [
    "GraphPlan",
    "erase_persona_graph",
    "merge_record",
    "plan_trails",
    "unmerged",
    "without_aliases",
]

Trail = list[dict[str, Any]]

# What a stub keeps of an entry: when it was written and by what kind of source.
_STUB_KEYS = ("source", "written_at")
# Consolidation's reserved metadata keys (persona.graph.consolidation).
_MERGE_KEY = "_k7_merge"
_ALIAS_KEY = "_k7_aliases"


@dataclass(frozen=True)
class GraphPlan:
    """What to do with a set of trails: which keys go, and which get a new trail."""

    doomed: list[str] = field(default_factory=list)
    rewrites: dict[str, Trail] = field(default_factory=dict)


def _is_persona(entry: object, persona_id: str) -> bool:
    return isinstance(entry, Mapping) and entry.get("persona_id") == persona_id


def _is_consolidation(entry: object) -> bool:
    """Consolidation's bookkeeping entry: not anyone's contribution (review H3)."""
    return (
        isinstance(entry, Mapping)
        and entry.get("persona_id") is None
        and str(entry.get("source")) == "system"
        and str(entry.get("reason") or "").startswith("consolidation:")
    )


def _stub(entry: Mapping[str, Any]) -> dict[str, Any]:
    return {key: entry[key] for key in _STUB_KEYS if key in entry}


def plan_trails(
    trails: Iterable[tuple[str, str, Trail]], *, persona_id: str, self_node_id: str
) -> GraphPlan:
    """Plan the persona out of ``(key, node_id, trail)`` rows: nodes, or version snapshots.

    A trail with nothing of the persona is left alone. One with an independent entry (anything
    but the persona's and consolidation's) keeps everything but the persona's. One with none is
    doomed, except the SELF node's, which keeps a stub of each of the persona's entries.
    """
    plan = GraphPlan()
    for key, node_id, trail in trails:
        if not any(_is_persona(entry, persona_id) for entry in trail):
            continue
        others = [entry for entry in trail if not _is_persona(entry, persona_id)]
        if any(not _is_consolidation(entry) for entry in others):
            plan.rewrites[key] = others
        elif node_id == self_node_id:
            plan.rewrites[key] = [_stub(e) for e in trail if _is_persona(e, persona_id)]
        else:
            plan.doomed.append(key)
    return plan


def merge_record(metadata: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """A consolidated member's reversal record, or ``None`` if it is not one."""
    raw = (metadata or {}).get(_MERGE_KEY)
    if not isinstance(raw, str):
        return None
    try:
        record = json.loads(raw)
    except ValueError:
        return None
    return record if isinstance(record, dict) else None


def without_aliases(metadata: Mapping[str, Any], names: Iterable[str]) -> dict[str, Any] | None:
    """The metadata with ``names`` gone from the alias list, or ``None`` if nothing changes."""
    try:
        aliases = json.loads(metadata.get(_ALIAS_KEY, "[]"))
    except ValueError:
        return None
    gone = set(names)
    kept = [a for a in aliases if a not in gone]
    if len(kept) == len(aliases):
        return None
    clean = {k: v for k, v in metadata.items() if k != _ALIAS_KEY}
    if kept:
        clean[_ALIAS_KEY] = json.dumps(kept, sort_keys=True)
    return clean


def unmerged(metadata: Mapping[str, Any] | None) -> tuple[str | None, dict[str, Any]]:
    """How to unmerge a member: the edge tag to reopen (``None`` if no record) and its metadata.

    The inverse ``ConsolidationPass.unmerge`` applies: the merge act closed the member's edges
    under ``consolidation:{run_id}``, and the record lives under its reserved key.
    """
    record = merge_record(metadata)
    tag = f"consolidation:{record['run_id']}" if record and record.get("run_id") else None
    return tag, {k: v for k, v in (metadata or {}).items() if k != _MERGE_KEY}


def _naming(column: Any, persona_id: str) -> Any:  # noqa: ANN401 - SQL expressions
    """A JSONB trail (a list of entries) with an entry from this persona."""
    return cast(column, JSONB).contains([{"persona_id": persona_id}])


def _set_metadata(conn: Connection, owner_id: str, node: Any, metadata: dict[str, Any]) -> None:  # noqa: ANN401 - a Row
    """Write new bookkeeping metadata, with the content hash it changes (content untouched)."""
    conn.execute(
        update(nodes_t)
        .where(nodes_t.c.owner_id == owner_id, nodes_t.c.id == node.id)
        .values(
            metadata=metadata,
            content_hash=_compute_node_hash(node.concept_name, node.content, metadata),
        )
    )


def _unmerge_members_of(conn: Connection, owner_id: str, doomed: list[str]) -> int:
    """Unmerge every member of a doomed node that is not doomed itself (review M1)."""
    members = conn.execute(
        select(nodes_t.c.id, nodes_t.c.concept_name, nodes_t.c.content, nodes_t.c.metadata)
        .where(
            nodes_t.c.owner_id == owner_id,
            nodes_t.c.merged_into.in_(doomed),
            nodes_t.c.id.not_in(doomed),
        )
        .with_for_update()
    ).all()
    for member in members:
        tag, metadata = unmerged(member.metadata)
        if tag is not None:
            conn.execute(
                update(graph_edges)
                .where(
                    graph_edges.c.owner_id == owner_id,
                    graph_edges.c.invalidated_by == tag,
                    or_(
                        graph_edges.c.src_node_id == member.id,
                        graph_edges.c.dst_node_id == member.id,
                    ),
                )
                .values(invalid_at=None, invalidated_by=None, invalidated_at=None)
            )
        _set_metadata(conn, owner_id, member, metadata)
        conn.execute(
            update(nodes_t)
            .where(nodes_t.c.owner_id == owner_id, nodes_t.c.id == member.id)
            .values(merged_into=None)
        )
    return len(members)


def _give_back(conn: Connection, owner_id: str, doomed_rows: list[Any], doomed: set[str]) -> int:
    """A doomed member's contributions to a canonical that stays: edges, entities, its name."""
    names_by_canonical: dict[str, list[str]] = {}
    for row in doomed_rows:
        record = merge_record(row.metadata)
        canonical = record.get("canonical") if record else None
        if record is None or not isinstance(canonical, str) or canonical in doomed:
            continue
        mirrored = [str(e) for e in record.get("mirrored_edges", [])]
        if mirrored:
            conn.execute(
                delete(graph_edges).where(
                    graph_edges.c.owner_id == owner_id,
                    graph_edges.c.id.in_(mirrored),
                    graph_edges.c.invalid_at.is_(None),
                )
            )
        added = [str(e) for e in record.get("added_entities", [])]
        if added:
            conn.execute(
                delete(graph_node_entities).where(
                    graph_node_entities.c.owner_id == owner_id,
                    graph_node_entities.c.node_id == canonical,
                    graph_node_entities.c.entity_id.in_(added),
                )
            )
        names_by_canonical.setdefault(canonical, []).append(row.concept_name)
    cleaned = 0
    for canonical_id, names in names_by_canonical.items():
        canonical = conn.execute(
            select(nodes_t.c.id, nodes_t.c.concept_name, nodes_t.c.content, nodes_t.c.metadata)
            .where(nodes_t.c.owner_id == owner_id, nodes_t.c.id == canonical_id)
            .with_for_update()
        ).first()
        metadata = without_aliases(canonical.metadata or {}, names) if canonical else None
        if canonical is not None and metadata is not None:
            _set_metadata(conn, owner_id, canonical, metadata)
            cleaned += 1
    return cleaned


def _nodes(conn: Connection, owner_id: str, persona_id: str) -> dict[str, int]:
    rows = conn.execute(
        select(
            nodes_t.c.id,
            nodes_t.c.provenance,
            nodes_t.c.concept_name,
            nodes_t.c.content,
            nodes_t.c.metadata,
        )
        .where(nodes_t.c.owner_id == owner_id, _naming(nodes_t.c.provenance, persona_id))
        # Locked until the rewrite commits: a concurrent extend (synthesis, consolidation)
        # waits, instead of appending to a trail this is about to overwrite (review L1).
        .with_for_update()
    ).all()
    plan = plan_trails(
        ((r.id, r.id, list(r.provenance)) for r in rows),
        persona_id=persona_id,
        self_node_id=make_self_node_id(owner_id),
    )
    doomed = set(plan.doomed)
    cleaned = unmerged_members = 0
    if doomed:
        unmerged_members = _unmerge_members_of(conn, owner_id, sorted(doomed))
        cleaned = _give_back(conn, owner_id, [r for r in rows if r.id in doomed], doomed)
        conn.execute(
            delete(nodes_t).where(nodes_t.c.owner_id == owner_id, nodes_t.c.id.in_(sorted(doomed)))
        )
    for node_id, trail in plan.rewrites.items():
        conn.execute(
            update(nodes_t)
            .where(nodes_t.c.owner_id == owner_id, nodes_t.c.id == node_id)
            .values(provenance=trail)
        )
    return {
        "nodes_deleted": len(doomed),
        "nodes_stripped": len(plan.rewrites),
        "canonicals_cleaned": cleaned,
        "members_unmerged": unmerged_members,
    }


def _versions(conn: Connection, owner_id: str, persona_id: str) -> dict[str, int]:
    rows = conn.execute(
        select(versions_t.c.version_key, versions_t.c.node_id, versions_t.c.provenance)
        .where(versions_t.c.owner_id == owner_id, _naming(versions_t.c.provenance, persona_id))
        .with_for_update()
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
    entities are judged by what still links to them. A no-op on an engine with no graph. An
    in-RAM turbovec index (opt-in) does not regain an unmerged member until it is rebuilt; the
    pgvector default reads ``merged_into`` from the table.
    """
    if engine.dialect.name != "postgresql":
        return {}
    with engine.begin() as conn:
        return {
            **_nodes(conn, owner_id, persona_id),
            **_versions(conn, owner_id, persona_id),
            **_edges_and_entities(conn, owner_id, persona_id),
        }
