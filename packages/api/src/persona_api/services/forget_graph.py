"""Forgetting a memory releases what consolidation copied from it, and its duplicates (R9-287).

When consolidation merges a node into a canonical it copies (mirrors) the node's open links
onto the canonical, links the canonical to the node's entities, and adds the node's name to the
canonical's aliases. The node's merge record lists all of it. A user's forget or delete of a
node removes the node and every node merged into it, directly or down a chain
(D-R9-287-forget-canonical: they are duplicates of the fact the user forgets), in the delete's
own transaction and under the owner's graph maintenance lock (core's ``NodeReleaser`` seam),
with the persona erasure's own functions:

* before the rows go: the doomed nodes are locked; their incident edges become the seeds (they
  go by cascade); every record's mirror sources are read; and ``_give_back`` returns the
  forgotten node's contributions to the canonical above it, if it was merged: a mirror its
  record lists without a source (removed by id, unless another live record lists it too), its
  entity links (a link a live co-member carries stays and passes to that member's record), and
  its names in the canonical's aliases, consolidation entries and versions (a name a surviving
  member also gave stays). The members' own canonicals are all doomed, so they give nothing
  back;
* the nodes go, as one statement (core);
* after: ``_remove_mirrors`` deletes the mirror rows every one of whose sources is now gone
  (one another live record still backs stays), and ``_prune_orphans`` drops the listings that
  name rows no longer there, so no record lists a mirror that is gone.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from persona.graph.postgres import NodeRelease
from sqlalchemy import BigInteger, Text, select

from persona_api.db.models import graph_edges
from persona_api.db.models import graph_nodes as nodes_t
from persona_api.services.erasure_graph import (
    _any_of,
    _give_back,
    _Mirrors,
    _prune_orphans,
    _remove_mirrors,
    _seed_incident,
    merge_record,
    source_keys,
    unsourced_mirrors,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection

__all__ = ["ForgetReleaser", "merged_below", "shared_legacy_mirrors"]

_MERGE_KEY = "_k7_merge"


def shared_legacy_mirrors(record: dict[str, Any], others: list[dict[str, Any]]) -> set[str]:
    """The record's source-less mirrors another live record lists too: those are kept.

    A mirror listed without a source is removed by id; one another record still lists (as a
    source-less mirror or with sources) is that record's too, and stays.
    """
    listed: set[str] = set()
    for other in others:
        listed |= {str(m) for m in other.get("mirrored_edges", [])}
        listed |= {str(m) for m in (other.get("mirror_sources") or {})}
    return unsourced_mirrors(record) & listed


def merged_below(conn: Connection, owner_id: str, node_id: str) -> list[str]:
    """Every node merged into ``node_id``, directly or down a chain, locked, in a stable order.

    A pending member is merged too (``merged_into`` is set while its act runs), so it goes and
    the next consolidation run has nothing to give back to life.
    """
    found: list[str] = []
    seen = {node_id}
    frontier = [node_id]
    while frontier:
        below = [
            str(m)
            for m in conn.execute(
                select(nodes_t.c.id)
                .where(
                    nodes_t.c.owner_id == owner_id,
                    _any_of(nodes_t.c.merged_into, frontier, Text),
                )
                .order_by(nodes_t.c.id)
                .with_for_update()
            ).scalars()
            if str(m) not in seen
        ]
        seen.update(below)
        found.extend(below)
        frontier = below
    return found


def _read_sources(conn: Connection, owner_id: str) -> tuple[_Mirrors, dict[str, dict[str, Any]]]:
    """Every merge record's mirror sources and the edge keys they name, read before the delete."""
    merged = conn.execute(
        select(nodes_t.c.id, nodes_t.c.metadata).where(
            nodes_t.c.owner_id == owner_id, nodes_t.c.merged_into.is_not(None)
        )
    ).all()
    records = {str(m.id): r for m in merged if (r := merge_record(m.metadata)) is not None}
    sources = [
        (str(mirror), str(record.get("run_id")), key)
        for record in records.values()
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
    found = _Mirrors(seeds=set(), sources=sources, legacy={}, asserted=[], existing=existing)
    return found, records


class ForgetReleaser:
    """The api's :class:`persona.graph.postgres.NodeReleaser` for a user's forget or delete."""

    def release(self, conn: Connection, owner_id: str, node_id: str) -> NodeRelease:
        row = conn.execute(
            select(nodes_t.c.id, nodes_t.c.concept_name, nodes_t.c.metadata)
            .where(nodes_t.c.owner_id == owner_id, nodes_t.c.id == node_id)
            .with_for_update()
        ).first()
        members = merged_below(conn, owner_id, node_id)
        mirrors, records = _read_sources(conn, owner_id)
        doomed = {node_id, *members}
        if row is not None:
            _seed_incident(conn, owner_id, sorted(doomed), mirrors)
            record = merge_record(row.metadata)
            if record is not None:
                others = [r for member, r in records.items() if member not in doomed]
                kept = shared_legacy_mirrors(record, others)
                if kept:
                    # Hand _give_back a record without the mirrors another record still lists.
                    record = {
                        **record,
                        "mirrored_edges": [
                            m for m in record.get("mirrored_edges", []) if str(m) not in kept
                        ],
                    }
                metadata = {**(row.metadata or {}), _MERGE_KEY: json.dumps(record, sort_keys=True)}
                given = SimpleNamespace(id=row.id, concept_name=row.concept_name, metadata=metadata)
                _give_back(conn, owner_id, [given], doomed)

        def after() -> dict[str, int]:
            removed = _remove_mirrors(conn, owner_id, mirrors)
            return {"mirrors_removed": removed, **_prune_orphans(conn, owner_id, mirrors)}

        return NodeRelease(members=tuple(members), after=after)
