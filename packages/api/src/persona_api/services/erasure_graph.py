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
  if no node links to it any more; an entity's aliases lose the surfaces the persona gave
  (review H-1; an alias written before aliases carried a persona stays, unattributed).

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

from persona.graph.models import LinkType, _compute_node_hash, make_edge_id, make_self_node_id
from sqlalchemy import cast, delete, exists, or_, select, text, update
from sqlalchemy.dialects.postgresql import JSONB

from persona_api.db.models import graph_edges, graph_entities, graph_node_entities
from persona_api.db.models import graph_node_versions as versions_t
from persona_api.db.models import graph_nodes as nodes_t

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

__all__ = [
    "GraphPlan",
    "erase_persona_graph",
    "delete_mirrors",
    "erased_personas_in_graph",
    "merge_record",
    "mirror_id_for",
    "plan_trails",
    "unmerged",
    "without_alias_names",
    "without_aliases",
    "without_persona_aliases",
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


_ALIASES_MARK = " aliases:"


def without_alias_names(trail: Iterable[Any], names: Iterable[str]) -> Trail | None:
    """A trail whose consolidation entries no longer list ``names`` (review H-3).

    ``_accrete_aliases`` records ``consolidation:{run} aliases:{a,b}`` on the canonical, so a
    member's name survives there after it leaves ``_k7_aliases``. An entry left with no names
    goes. ``None`` when nothing changes.
    """
    gone = set(names)
    trail = list(trail)
    out: Trail = []
    for entry in trail:
        reason = str(entry.get("reason") or "") if isinstance(entry, Mapping) else ""
        if not _is_consolidation(entry) or _ALIASES_MARK not in reason:
            out.append(entry)
            continue
        head, _, listed = reason.partition(_ALIASES_MARK)
        kept = [n for n in listed.split(",") if n and n not in gone]
        if kept:
            out.append({**entry, "reason": f"{head}{_ALIASES_MARK}{','.join(kept)}"})
    return None if out == trail else out


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


def delete_mirrors(owner_id: str, edge_ids: list[str], *, run_id: str) -> Any:  # noqa: ANN401
    """Delete the open edges a merge mirrored, and only while they are still the merge's.

    An edge id is ``src::type::dst``, so another persona (or the user) asserting the same fact
    later upserts the same open row and makes it theirs; deleting by id alone would take that
    with the mirror (review M-4). So the row must still carry the consolidation's SYSTEM
    provenance with this run's tag.
    """
    return delete(graph_edges).where(
        graph_edges.c.owner_id == owner_id,
        graph_edges.c.id.in_(edge_ids),
        graph_edges.c.invalid_at.is_(None),
        cast(graph_edges.c.provenance, JSONB).contains(
            {"source": "system", "reason": f"consolidation:{run_id}"}
        ),
    )


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
            conn.execute(delete_mirrors(owner_id, mirrored, run_id=str(record.get("run_id"))))
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
        _drop_alias_names_from_trails(conn, owner_id, canonical_id, names)
    return cleaned


def _drop_alias_names_from_trails(
    conn: Connection, owner_id: str, canonical_id: str, names: list[str]
) -> None:
    """The names also leave the canonical's consolidation entries, and its snapshots'."""
    for table, key in ((nodes_t, nodes_t.c.id), (versions_t, versions_t.c.version_key)):
        node_column = table.c.id if table is nodes_t else table.c.node_id
        rows = conn.execute(
            select(key, table.c.provenance)
            .where(table.c.owner_id == owner_id, node_column == canonical_id)
            .order_by(key)
            .with_for_update()
        ).all()
        for row_key, trail in rows:
            rewritten = without_alias_names(trail or [], names)
            if rewritten is not None:
                conn.execute(
                    update(table)
                    .where(table.c.owner_id == owner_id, key == row_key)
                    .values(provenance=rewritten)
                )


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
    # The persona's entries leave the trails first, so what the doomed members give back is
    # taken from the trails as they now are, and not overwritten after.
    for node_id, trail in plan.rewrites.items():
        conn.execute(
            update(nodes_t)
            .where(nodes_t.c.owner_id == owner_id, nodes_t.c.id == node_id)
            .values(provenance=trail)
        )
    cleaned = unmerged_members = 0
    if doomed:
        unmerged_members = _unmerge_members_of(conn, owner_id, sorted(doomed))
        cleaned = _give_back(conn, owner_id, [r for r in rows if r.id in doomed], doomed)
        conn.execute(
            delete(nodes_t).where(nodes_t.c.owner_id == owner_id, nodes_t.c.id.in_(sorted(doomed)))
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


def mirror_id_for(
    src: str, dst: str, link_type: str, *, member_id: str, canonical_id: str
) -> str | None:
    """The id consolidation gave the mirror of a member's edge, as ``_mirror_assertion_edges``.

    The member's end is re-pointed onto the canonical; a self-loop is never mirrored.
    """
    if src == member_id:
        src = canonical_id
    elif dst == member_id:
        dst = canonical_id
    else:
        return None
    return None if src == dst else make_edge_id(src, dst, LinkType(link_type))


def _mirrors_of_asserted(conn: Connection, owner_id: str, persona_id: str) -> int:
    """The mirrors on a canonical of edges the persona asserted on a member that stays.

    The persona's edge goes by the edge rule, but consolidation copied it onto the canonical as
    a SYSTEM mirror, which would survive unattributed (review M-3). Deleted only while it is
    still that merge's mirror.
    """
    asserted = conn.execute(
        select(graph_edges.c.src_node_id, graph_edges.c.dst_node_id, graph_edges.c.link_type)
        .where(graph_edges.c.owner_id == owner_id, _asserted(graph_edges.c.provenance, persona_id))
        .order_by(graph_edges.c.id)
    ).all()
    ends = sorted({n for e in asserted for n in (e.src_node_id, e.dst_node_id)})
    members = conn.execute(
        select(nodes_t.c.id, nodes_t.c.metadata).where(
            nodes_t.c.owner_id == owner_id,
            nodes_t.c.id.in_(ends),
            nodes_t.c.merged_into.is_not(None),
        )
    ).all()
    records = {m.id: merge_record(m.metadata) for m in members}
    deleted = 0
    for edge in asserted:
        for member_id in {edge.src_node_id, edge.dst_node_id} & set(records):
            record = records[member_id]
            canonical = record.get("canonical") if record else None
            if record is None or not isinstance(canonical, str):
                continue
            mirror = mirror_id_for(
                edge.src_node_id,
                edge.dst_node_id,
                str(edge.link_type),
                member_id=member_id,
                canonical_id=canonical,
            )
            if mirror is not None:
                deleted += conn.execute(
                    delete_mirrors(owner_id, [mirror], run_id=str(record.get("run_id")))
                ).rowcount
    return deleted


def _edges_and_entities(conn: Connection, owner_id: str, persona_id: str) -> dict[str, int]:
    mirrors = _mirrors_of_asserted(conn, owner_id, persona_id)
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
        "mirrors_of_asserted_deleted": mirrors,
        "entities_deleted": entities,
        "entities_unattributed": unattributed,
        "entity_aliases_removed": _strip_entity_aliases(conn, owner_id, persona_id),
    }


def without_persona_aliases(aliases: Iterable[Any], persona_id: str) -> list[Any] | None:
    """An entity's alias list without the surfaces this persona contributed (review H-1).

    ``None`` when nothing changes. An alias written before aliases carried a persona has no
    ``persona_id`` and stays: whose it was is not known, and the erasure does not guess.
    """
    aliases = list(aliases)
    kept = [a for a in aliases if not _is_persona(a, persona_id)]
    return None if len(kept) == len(aliases) else kept


def _strip_entity_aliases(conn: Connection, owner_id: str, persona_id: str) -> int:
    rows = conn.execute(
        select(graph_entities.c.id, graph_entities.c.aliases)
        .where(
            graph_entities.c.owner_id == owner_id,
            _naming(graph_entities.c.aliases, persona_id),
        )
        .order_by(graph_entities.c.id)
        .with_for_update()
    ).all()
    removed = 0
    for entity_id, aliases in rows:
        kept = without_persona_aliases(aliases or [], persona_id)
        if kept is None:
            continue
        removed += len(aliases) - len(kept)
        conn.execute(
            update(graph_entities)
            .where(graph_entities.c.owner_id == owner_id, graph_entities.c.id == entity_id)
            .values(aliases=kept)
        )
    return removed


# Completed persona erasures the graph still names, for the sweep's backstop (review H-2).
_LEFTOVERS = text("""
    SELECT e.owner_id, e.subject_id
    FROM erasures e
    WHERE e.subject_kind = 'persona' AND e.state = 'complete'
      AND NOT EXISTS (SELECT 1 FROM personas p WHERE p.id = e.subject_id)
      AND (
        EXISTS (SELECT 1 FROM graph_nodes n WHERE n.owner_id = e.owner_id
                AND n.provenance::jsonb
                    @> jsonb_build_array(jsonb_build_object('persona_id', e.subject_id)))
        OR EXISTS (SELECT 1 FROM graph_node_versions v WHERE v.owner_id = e.owner_id
                AND v.provenance::jsonb
                    @> jsonb_build_array(jsonb_build_object('persona_id', e.subject_id)))
        OR EXISTS (SELECT 1 FROM graph_edges g WHERE g.owner_id = e.owner_id
                AND g.provenance::jsonb @> jsonb_build_object('persona_id', e.subject_id))
        OR EXISTS (SELECT 1 FROM graph_entities x WHERE x.owner_id = e.owner_id
                AND (x.provenance::jsonb @> jsonb_build_object('persona_id', e.subject_id)
                     OR x.aliases::jsonb
                        @> jsonb_build_array(jsonb_build_object('persona_id', e.subject_id))))
      )
    GROUP BY e.owner_id, e.subject_id
    ORDER BY e.owner_id, e.subject_id
""")


def erased_personas_in_graph(engine: Engine) -> list[tuple[str, str]]:
    """``(owner, persona)`` for every completed erasure the graph still names. Postgres only.

    A persona whose row exists is never returned, so a live persona is never touched.
    """
    if engine.dialect.name != "postgresql":
        return []
    with engine.connect() as conn:
        return [(str(o), str(p)) for o, p in conn.execute(_LEFTOVERS)]


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
