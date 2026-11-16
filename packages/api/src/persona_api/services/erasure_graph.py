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
from bisect import bisect_left
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from persona.graph.maintenance_lock import (
    take_graph_maintenance_lock,
    try_graph_maintenance_lock,
)
from persona.graph.models import LinkType, _compute_node_hash, make_edge_id, make_self_node_id
from sqlalchemy import (
    BigInteger,
    Text,
    any_,
    cast,
    delete,
    exists,
    literal,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB

from persona_api.db.models import graph_edges, graph_entities, graph_node_entities
from persona_api.db.models import graph_node_versions as versions_t
from persona_api.db.models import graph_nodes as nodes_t

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy.engine import Connection, Engine

__all__ = [
    "GraphMaintenanceBusyError",
    "GraphPlan",
    "erase_persona_graph",
    "delete_mirrors",
    "erased_personas_in_graph",
    "merge_record",
    "names_only_the_doomed_gave",
    "mirror_id_for",
    "mirror_row_deletes",
    "mirrors_to_remove",
    "orphaned_mirrors",
    "source_keys",
    "unsourced_mirrors",
    "split_alias_list",
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


def names_only_the_doomed_gave(doomed: Iterable[str], surviving: Iterable[str]) -> list[str]:
    """The doomed members' names that no surviving member of the canonical also gave.

    An alias is one name however many members carried it; a member that stays still gives it.
    """
    kept = set(surviving)
    return sorted({n for n in doomed if n and n not in kept})


def alias_list(metadata: Mapping[str, Any]) -> list[str]:
    """The canonical's accreted alias names (``[]`` if absent or unreadable)."""
    try:
        aliases = json.loads(metadata.get(_ALIAS_KEY, "[]"))
    except (TypeError, ValueError):
        return []
    return [a for a in aliases if isinstance(a, str)] if isinstance(aliases, list) else []


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


def split_alias_list(listed: str, known: Iterable[str], *, doomed: Iterable[str] = ()) -> list[str]:
    """The names in an alias list ``_accrete_aliases`` wrote (review HIGH-3).

    Since HIGH-3 the list is JSON. Before it, the names were joined by ",", and a name may hold
    a comma, so splitting on "," cut such a name into fragments: a doomed "Bergen, Norway" was
    never removed, and a surviving "Oslo, Norway" lost its "Oslo" to a doomed "Oslo". A legacy
    list is therefore tokenised against the names known to be in it (the doomed names and the
    canonical's alias list), preferring the reading with the fewest pieces no known name
    accounts for; such a piece runs to the next comma.

    When two readings tie on that, the one holding more ``doomed`` names as whole tokens wins
    (round 5 LOW-2): on a legacy row privacy wins. Known {a, b, "a,b"} with "a" doomed reads
    "a,b" as a and b, so "a" goes. The ambiguity this leaves is inherent: had the list been a
    survivor's single name "a,b", that survivor's name is cut to "b" in this reason.
    """
    if listed.startswith("["):
        try:
            parsed = json.loads(listed)
        except ValueError:
            parsed = None
        if isinstance(parsed, list) and all(isinstance(n, str) for n in parsed):
            return [n for n in parsed if n]
    names = {k for k in known if k}
    gone = set(doomed)
    end = len(listed)
    # A token starts at 0 or just after a comma, and ends at a comma or the end (round 5
    # LOW-3): only those positions are evaluated, and a known name only up to the longest.
    stops = [j for j, c in enumerate(listed) if c == ","] + [end]
    starts = [0] + [j + 1 for j in stops[:-1]]
    longest = max((len(n) for n in names), default=0)
    best: dict[int, tuple[int, int, int]] = {end: (0, 0, 0), end + 1: (0, 0, 0)}
    step: dict[int, tuple[int, str | None]] = {}
    for i in reversed(starts):
        options: list[tuple[tuple[int, int, int], int, str | None]] = []
        first = bisect_left(stops, i)
        for stop in stops[first:]:
            if stop - i > longest:
                break
            candidate = listed[i:stop]
            if candidate in names:
                rest = best[stop + 1]
                whole = 1 if candidate in gone else 0
                options.append(((rest[0], rest[1] - whole, rest[2] + 1), stop, candidate))
        stop = stops[first]
        rest = best[stop + 1]
        if stop == i:
            options.append((rest, stop, None))  # an empty piece: nothing to keep
        else:
            options.append(((rest[0] + 1, rest[1], rest[2] + 1), stop, listed[i:stop]))
        key, stop, chosen = min(options, key=lambda o: o[0])
        best[i] = key
        step[i] = (stop, chosen)
    tokens: list[str] = []
    i = 0
    while i < end:
        stop, kept = step[i]
        if kept is not None:
            tokens.append(kept)
        i = stop + 1
    return tokens


def without_alias_names(
    trail: Iterable[Any], names: Iterable[str], *, known: Iterable[str] = ()
) -> Trail | None:
    """A trail whose consolidation entries no longer list ``names`` (review H-3).

    ``_accrete_aliases`` records ``consolidation:{run} aliases:[...]`` on the canonical, so a
    member's name survives there after it leaves ``_k7_aliases``. ``known`` is the other names
    a legacy, comma-joined list may hold (see ``split_alias_list``). An entry left with no
    names goes; one that keeps names is rewritten as JSON. ``None`` when nothing changes.
    """
    gone = set(names)
    vocabulary = [*gone, *known]
    trail = list(trail)
    out: Trail = []
    for entry in trail:
        reason = str(entry.get("reason") or "") if isinstance(entry, Mapping) else ""
        if not _is_consolidation(entry) or _ALIASES_MARK not in reason:
            out.append(entry)
            continue
        head, _, listed = reason.partition(_ALIASES_MARK)
        listed_names = split_alias_list(listed, vocabulary, doomed=gone)
        kept = [n for n in listed_names if n not in gone]
        if len(kept) == len(listed_names):
            out.append(entry)
        elif kept:
            out.append({**entry, "reason": f"{head}{_ALIASES_MARK}{json.dumps(kept)}"})
    return None if out == trail else out


def _any_of(column: Any, values: Iterable[Any], element: Any) -> Any:  # noqa: ANN401 - SQL
    """``column = ANY(:array)``: one array parameter, however many values (round 7 LOW-3)."""
    return column == any_(literal(list(values), ARRAY(element)))


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
        .order_by(nodes_t.c.id)
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
        # A mirror whose source the record names is found from the seeds after the node step
        # (round 5 HIGH-2); only one it does not name is removed by id here, while still open.
        sourced = record.get("mirror_sources") or {}
        mirrored = [str(e) for e in record.get("mirrored_edges", []) if str(e) not in sourced]
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
    for canonical_id, given in names_by_canonical.items():
        survivors = conn.execute(
            select(nodes_t.c.concept_name).where(
                nodes_t.c.owner_id == owner_id,
                nodes_t.c.merged_into == canonical_id,
                nodes_t.c.id.not_in(sorted(doomed)),
            )
        ).scalars()
        names = names_only_the_doomed_gave(given, survivors)
        if not names:
            continue
        canonical = conn.execute(
            select(nodes_t.c.id, nodes_t.c.concept_name, nodes_t.c.content, nodes_t.c.metadata)
            .where(nodes_t.c.owner_id == owner_id, nodes_t.c.id == canonical_id)
            .with_for_update()
        ).first()
        metadata = without_aliases(canonical.metadata or {}, names) if canonical else None
        if canonical is not None and metadata is not None:
            _set_metadata(conn, owner_id, canonical, metadata)
            cleaned += 1
        known = alias_list(canonical.metadata or {}) if canonical else []
        _drop_alias_names_from_trails(conn, owner_id, canonical_id, names, known=known)
    return cleaned


def _drop_alias_names_from_trails(
    conn: Connection,
    owner_id: str,
    canonical_id: str,
    names: list[str],
    *,
    known: Iterable[str] = (),
) -> None:
    """The names also leave the canonical's consolidation entries, and its snapshots'.

    ``known`` is the canonical's alias list before this erasure rewrote it (review HIGH-3).
    """
    for table, key in ((nodes_t, nodes_t.c.id), (versions_t, versions_t.c.version_key)):
        node_column = table.c.id if table is nodes_t else table.c.node_id
        rows = conn.execute(
            select(key, table.c.provenance)
            .where(table.c.owner_id == owner_id, node_column == canonical_id)
            .order_by(key)
            .with_for_update()
        ).all()
        for row_key, trail in rows:
            rewritten = without_alias_names(trail or [], names, known=known)
            if rewritten is not None:
                conn.execute(
                    update(table)
                    .where(table.c.owner_id == owner_id, key == row_key)
                    .values(provenance=rewritten)
                )


def _nodes(conn: Connection, owner_id: str, persona_id: str, mirrors: _Mirrors) -> dict[str, int]:
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
        # waits, instead of appending to a trail this is about to overwrite (review L1). In id
        # order, so two erasures locking overlapping rows take them in the same order. Other
        # writers (a guarded write holds a persona row, then a node or edge) lock in their own
        # order, so a deadlock or serialization failure is still possible: Postgres aborts one
        # side, and the erasure is retried (the job with backoff, the sweep on its next pass;
        # test_persona_erase_job.py and test_persona_erase_sweep.py pin both).
        .order_by(nodes_t.c.id)
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
        _seed_incident(conn, owner_id, sorted(doomed), mirrors)
        unmerged_members = _unmerge_members_of(conn, owner_id, sorted(doomed))
        cleaned = _give_back(conn, owner_id, [r for r in rows if r.id in doomed], doomed)
        conn.execute(
            delete(nodes_t).where(
                nodes_t.c.owner_id == owner_id, _any_of(nodes_t.c.id, sorted(doomed), Text)
            )
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
        .order_by(versions_t.c.version_key)
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


def unsourced_mirrors(record: Mapping[str, Any]) -> set[str]:
    """A record's mirrors with no listed source: found by recomputing their id (round 5 LOW-1).

    Every mirror of a record from before LOW-3, and any entry a newer record lists without a
    source.
    """
    sourced = record.get("mirror_sources") or {}
    return {str(m) for m in record.get("mirrored_edges", []) if str(m) not in sourced}


def source_keys(raw: object) -> list[int]:
    """A record's sources for one mirror: a list since round 5 MEDIUM-2, one key before it."""
    if isinstance(raw, bool):
        return []
    if isinstance(raw, int):
        return [raw]
    if isinstance(raw, list):
        return [k for k in raw if isinstance(k, int) and not isinstance(k, bool)]
    return []


def mirrors_to_remove(
    seed_keys: Iterable[int],
    mirror_sources: Iterable[tuple[str, str, int]],
    mirror_rows: Mapping[tuple[str, str], Iterable[int]],
    existing: Iterable[int] | None = None,
) -> list[int]:
    """The edge keys of every mirror row whose sources are all going (LOW-3, round 5 MEDIUM-2).

    ``mirror_sources`` is every merge record's ``(mirror id, run id, source edge_key)``; one
    mirror can have several (members of one run that shared it), from several records.
    ``mirror_rows`` is the edge keys of each run's mirror rows, open or closed (a mirror on a
    canonical that was itself merged later is closed, and is the source of the next mirror).
    ``existing`` is the edge keys that existed before this erasure (all, when ``None``).

    A mirror goes once one of its sources is going (a seed, or a mirror row found going) and
    none survives: each is going or was already gone. Followed to the end of the chain.
    """
    going = set(seed_keys)
    present = None if existing is None else set(existing)
    by_mirror: dict[tuple[str, str], set[int]] = {}
    for mirror_id, run_id, source_key in mirror_sources:
        by_mirror.setdefault((mirror_id, run_id), set()).add(source_key)
    found: set[int] = set()
    grew = True
    while grew:
        grew = False
        for mirror, sources in by_mirror.items():
            rows = set(mirror_rows.get(mirror, ())) - found
            if not rows or not sources & going:
                continue
            if all(k in going or (present is not None and k not in present) for k in sources):
                found |= rows
                going |= rows
                grew = True
    return sorted(found)


def orphaned_mirrors(
    records: Mapping[str, Mapping[str, Any]],
    mirror_rows: Mapping[tuple[str, str], Iterable[int]],
    existing: Iterable[int],
) -> tuple[list[int], dict[str, dict[str, Any]]]:
    """Mirrors no live source backs, and the records' stale listings (round 6 M-1).

    ``records`` is every surviving member's merge record, ``mirror_rows`` the owner's mirror
    rows by ``(mirror id, run id)``, ``existing`` the edge keys that are still rows. A mirror
    some record lists sources for, none of which is a row any more, has lost every source: its
    rows go (whatever removed the sources: an erasure, a forget, a stale co-member's listing).
    Each record drops the source keys that are no longer rows, and a mirror id once it has no
    sources left; a mirror it lists without sources (before LOW-3) is left as it is.
    Returns the edge keys to delete and ``{member id: its rewritten record}``.
    """
    alive = set(existing)
    listed: dict[tuple[str, str], set[int]] = {}
    for record in records.values():
        run_id = str(record.get("run_id"))
        for mirror, raw in (record.get("mirror_sources") or {}).items():
            listed.setdefault((str(mirror), run_id), set()).update(source_keys(raw))
    # To a fixpoint (round 7 LOW-1): a mirror row that goes can be another mirror's only
    # source, down a chain, so its key leaves ``alive`` and the test runs again.
    doomed: set[int] = set()
    while True:
        more = {
            key
            for mirror, sources in listed.items()
            if sources and not sources & alive
            for key in mirror_rows.get(mirror, ())
        } - doomed
        if not more:
            break
        doomed |= more
        alive -= more
    rewrites: dict[str, dict[str, Any]] = {}
    for member_id, record in records.items():
        sources = record.get("mirror_sources") or {}
        kept = {m: [k for k in source_keys(raw) if k in alive] for m, raw in sources.items()}
        emptied = {m for m, keys in kept.items() if not keys}
        if all(source_keys(sources[m]) == keys for m, keys in kept.items()):
            continue
        rewrites[member_id] = {
            **record,
            "mirror_sources": {m: keys for m, keys in kept.items() if keys},
            "mirrored_edges": [
                e for e in record.get("mirrored_edges", []) if str(e) not in emptied
            ],
        }
    return sorted(doomed), rewrites


def _prune_orphans(conn: Connection, owner_id: str, mirrors: _Mirrors) -> dict[str, int]:
    """Apply :func:`orphaned_mirrors` to the owner's graph, after the rest of the step.

    The mirror rows are the ones ``_remove_mirrors`` read, less those it deleted (nothing in
    between deletes a mirror row), and only the records it rewrites are locked (round 7 LOW-3).
    """
    members = conn.execute(
        select(nodes_t.c.id, nodes_t.c.metadata).where(
            nodes_t.c.owner_id == owner_id, nodes_t.c.merged_into.is_not(None)
        )
    ).all()
    records = {m.id: r for m in members if (r := merge_record(m.metadata)) is not None}
    rows = {g: keys - mirrors.removed for g, keys in mirrors.rows.items() if keys - mirrors.removed}
    named = sorted(
        {
            k
            for r in records.values()
            for raw in (r.get("mirror_sources") or {}).values()
            for k in source_keys(raw)
        }
    )
    existing = (
        set(
            conn.execute(
                select(graph_edges.c.edge_key).where(
                    graph_edges.c.owner_id == owner_id,
                    _any_of(graph_edges.c.edge_key, named, BigInteger),
                )
            ).scalars()
        )
        if named
        else set()
    )
    doomed, rewrites = orphaned_mirrors(records, rows, existing)
    deleted = sum(
        conn.execute(stmt).rowcount for stmt in mirror_row_deletes(owner_id, doomed, rows)
    )
    if rewrites:
        locked = conn.execute(
            select(nodes_t.c.id, nodes_t.c.concept_name, nodes_t.c.content, nodes_t.c.metadata)
            .where(nodes_t.c.owner_id == owner_id, _any_of(nodes_t.c.id, sorted(rewrites), Text))
            .order_by(nodes_t.c.id)
            .with_for_update()
        ).all()
        for member in locked:
            record = rewrites[member.id]
            metadata = {**(member.metadata or {}), _MERGE_KEY: json.dumps(record, sort_keys=True)}
            _set_metadata(conn, owner_id, member, metadata)
    return {"orphan_mirrors_deleted": deleted, "merge_records_pruned": len(rewrites)}


def mirror_row_deletes(
    owner_id: str,
    keys: Iterable[int],
    mirror_rows: Mapping[tuple[str, str], Iterable[int]],
) -> list[Any]:
    """One DELETE per run for these mirror rows, matching that run's tag (round 7 LOW-2).

    Each row goes only while it still carries ``{"source": "system", "reason":
    "consolidation:<run>"}``, as :func:`delete_mirrors` does by id: another run's mirror, or
    an edge that became someone's own, is never taken by its key alone.
    """
    wanted = set(keys)
    by_run: dict[str, list[int]] = {}
    for (_mirror, run_id), row_keys in mirror_rows.items():
        hit = sorted(wanted & set(row_keys))
        if hit:
            by_run.setdefault(run_id, []).extend(hit)
    return [
        delete(graph_edges).where(
            graph_edges.c.owner_id == owner_id,
            _any_of(graph_edges.c.edge_key, sorted(run_keys), BigInteger),
            cast(graph_edges.c.provenance, JSONB).contains(
                {"source": "system", "reason": f"consolidation:{run_id}"}
            ),
        )
        for run_id, run_keys in sorted(by_run.items())
    ]


def _mirror_rows(conn: Connection, owner_id: str) -> dict[tuple[str, str], set[int]]:
    """Every consolidation mirror row of the owner: ``{(edge id, run id): {edge_key}}``."""
    rows = conn.execute(
        select(graph_edges.c.id, graph_edges.c.edge_key, graph_edges.c.provenance).where(
            graph_edges.c.owner_id == owner_id,
            cast(graph_edges.c.provenance, JSONB).contains({"source": "system"}),
            cast(graph_edges.c.provenance, JSONB)["reason"].astext.like("consolidation:%"),
        )
    ).all()
    out: dict[tuple[str, str], set[int]] = {}
    for edge_id, edge_key, provenance in rows:
        reason = str((provenance or {}).get("reason") or "")
        if reason.startswith("consolidation:"):
            run_id = reason.removeprefix("consolidation:")
            out.setdefault((str(edge_id), run_id), set()).add(int(edge_key))
    return out


@dataclass
class _Mirrors:
    """What the erasure needs to find mirrors, read before any node goes (round 5 HIGH-2).

    A doomed node's merge record and its incident edges go with it (the edges by cascade), so
    the seed edge keys and every record's sources are collected first. ``seeds`` grows in
    ``_nodes`` by the edges incident to each doomed node.
    """

    seeds: set[int]
    sources: list[tuple[str, str, int]]
    legacy: dict[str, dict[str, Any]]
    asserted: list[Any]
    existing: set[int]
    # Set by _remove_mirrors: the mirror rows it read, and the keys it deleted (round 7 LOW-3).
    rows: dict[tuple[str, str], set[int]] = field(default_factory=dict)
    removed: set[int] = field(default_factory=set)


def _read_mirrors(conn: Connection, owner_id: str, persona_id: str) -> _Mirrors:
    asserted = conn.execute(
        select(
            graph_edges.c.edge_key,
            graph_edges.c.src_node_id,
            graph_edges.c.dst_node_id,
            graph_edges.c.link_type,
        )
        .where(graph_edges.c.owner_id == owner_id, _asserted(graph_edges.c.provenance, persona_id))
        .order_by(graph_edges.c.edge_key)
    ).all()
    merged = conn.execute(
        select(nodes_t.c.id, nodes_t.c.metadata).where(
            nodes_t.c.owner_id == owner_id, nodes_t.c.merged_into.is_not(None)
        )
    ).all()
    records = {m.id: merge_record(m.metadata) for m in merged}
    sources = [
        (str(mirror), str(record.get("run_id")), key)
        for record in records.values()
        if record is not None
        for mirror, raw in (record.get("mirror_sources") or {}).items()
        for key in source_keys(raw)
    ]
    named = sorted({key for _, _, key in sources})
    existing = (
        {
            int(k)
            for k in conn.execute(
                select(graph_edges.c.edge_key).where(
                    graph_edges.c.owner_id == owner_id,
                    _any_of(graph_edges.c.edge_key, named, BigInteger),
                )
            ).scalars()
        }
        if named
        else set()
    )
    return _Mirrors(
        seeds={int(e.edge_key) for e in asserted},
        sources=sources,
        legacy={i: r for i, r in records.items() if r is not None and unsourced_mirrors(r)},
        asserted=list(asserted),
        existing=existing,
    )


def _seed_incident(conn: Connection, owner_id: str, doomed: list[str], mirrors: _Mirrors) -> None:
    """Every edge incident to a doomed node is a seed: it goes with the node (round 5 HIGH-2)."""
    rows = conn.execute(
        select(graph_edges.c.edge_key).where(
            graph_edges.c.owner_id == owner_id,
            or_(
                _any_of(graph_edges.c.src_node_id, doomed, Text),
                _any_of(graph_edges.c.dst_node_id, doomed, Text),
            ),
        )
    ).scalars()
    mirrors.seeds.update(int(k) for k in rows)


def _remove_mirrors(conn: Connection, owner_id: str, mirrors: _Mirrors) -> int:
    """The mirrors descended from the seed edges, after the node step (review M-3, HIGH-2).

    The persona's edge goes by the edge rule, and a doomed node's edges by cascade, but
    consolidation copied them onto canonicals as SYSTEM mirrors, which would survive
    unattributed. A record written since LOW-3 names each mirror's source row, so the mirror
    rows are found by that, chains and history rows included, and deleted by edge_key; an
    older record is matched by recomputing the open mirror's id. Either way a row goes only
    while it carries the merge's provenance.
    """
    rows = _mirror_rows(conn, owner_id)
    mirrors.rows = rows
    # A mirror row is an edge too: present before this erasure, unless it went with a node.
    existing = mirrors.existing | {k for keys in rows.values() for k in keys}
    keys = mirrors_to_remove(mirrors.seeds, mirrors.sources, rows, existing)
    deleted = 0
    deleted += sum(conn.execute(stmt).rowcount for stmt in mirror_row_deletes(owner_id, keys, rows))
    mirrors.removed = set(keys)
    for edge in mirrors.asserted:
        for member_id in {edge.src_node_id, edge.dst_node_id} & set(mirrors.legacy):
            record = mirrors.legacy[member_id]
            canonical = record.get("canonical")
            if not isinstance(canonical, str):
                continue
            mirror = mirror_id_for(
                edge.src_node_id,
                edge.dst_node_id,
                str(edge.link_type),
                member_id=member_id,
                canonical_id=canonical,
            )
            if mirror is not None and mirror in unsourced_mirrors(record):
                deleted += conn.execute(
                    delete_mirrors(owner_id, [mirror], run_id=str(record.get("run_id")))
                ).rowcount
    return deleted


def _edges_and_entities(
    conn: Connection, owner_id: str, persona_id: str, found: _Mirrors
) -> dict[str, int]:
    mirrors = _remove_mirrors(conn, owner_id, found)
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
      AND (CAST(:since AS timestamptz) IS NULL OR e.completed_at >= CAST(:since AS timestamptz))
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


def erased_personas_in_graph(
    engine: Engine, *, since: datetime | None = None
) -> list[tuple[str, str]]:
    """``(owner, persona)`` for each completed erasure the graph still names. Postgres only.

    ``since`` limits it to erasures completed from then on (review MEDIUM-3: the sweep looks
    at the last day's every pass, and at all of them once a day); ``None`` looks at all. A
    persona whose row exists is never returned, so a live persona is never touched.
    """
    if engine.dialect.name != "postgresql":
        return []
    with engine.connect() as conn:
        return [(str(o), str(p)) for o, p in conn.execute(_LEFTOVERS, {"since": since})]


class GraphMaintenanceBusyError(RuntimeError):
    """A consolidation run holds the owner's graph lock, and the caller would not wait."""


def erase_persona_graph(
    engine: Engine, *, owner_id: str, persona_id: str, wait: bool = True
) -> dict[str, int]:
    """Take the persona out of its owner's graph, in one transaction; return the counts.

    Nodes first, so the edges and entity links of deleted nodes go by cascade before the
    entities are judged by what still links to them. A no-op on an engine with no graph. An
    in-RAM turbovec index (opt-in) does not regain an unmerged member until it is rebuilt; the
    pgvector default reads ``merged_into`` from the table.
    """
    if engine.dialect.name != "postgresql":
        return {}
    with engine.begin() as conn:
        # First, before any row lock: a consolidation run of this owner finishes before this
        # starts, and none starts until this commits (D-R9-284-graph-mutex; why no deadlock
        # cycle passes through it: persona.graph.maintenance_lock). The persona row itself was
        # deleted earlier, in its own transaction, without this lock.
        # ``wait=False`` (the sweep, on the worker's event loop, round 6 M-3) does not wait
        # for a running consolidation: it raises, and the sweep tries the persona again.
        if wait:
            take_graph_maintenance_lock(conn, owner_id)
        elif not try_graph_maintenance_lock(conn, owner_id):
            raise GraphMaintenanceBusyError(owner_id)
        mirrors = _read_mirrors(conn, owner_id, persona_id)
        return {
            **_nodes(conn, owner_id, persona_id, mirrors),
            **_versions(conn, owner_id, persona_id),
            **_edges_and_entities(conn, owner_id, persona_id, mirrors),
            # Last: whatever lost its every source, here or by another path (round 6 M-1).
            **_prune_orphans(conn, owner_id, mirrors),
        }
