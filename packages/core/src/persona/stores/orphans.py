"""Find and remove what a deleted persona or conversation left behind (R9-284).

Deleting a persona used to delete only its row, so everything without a foreign key to it stayed:
every memory row, the document chunks of its conversations, its initiative notices, and the daily
scan schedule that went on firing for nobody. Migration 062 stops new orphans, and a database
restored from a backup still has the old ones.

An owner is missing or it is not, so nothing here is a judgement call and nothing needs a model.
Each removal is one transaction: a single ``DELETE`` that asks again whether the owner is missing,
so a scan that has gone stale cannot take a live row, and an ``audit_log`` row with ids and counts
in the same commit. It reads ids and counts only and never selects content, because what it prints
lands in deploy logs. Postgres only: a Chroma folder has no table of owners to check against.

Evidence comes before what was written from it (K8-D-14), so a sweep that stops part way never
leaves a summary behind with its sources gone.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

__all__ = ["OrphanGroup", "OrphanSweep"]

#: Evidence first, then what was derived from it, then what merely referred to the persona.
_ORDER: tuple[str, ...] = (
    "identity",
    "self_facts",
    "worldview",
    "episodic",
    "document",
    "episodic_gist",
    "core_memory",
    "initiative_notice",
    "initiative_schedule",
)
_RANK = {kind: rank for rank, kind in enumerate(_ORDER)}

_MEMORY_ORPHAN = (
    "CASE WHEN m.kind = 'document' "
    "THEN NOT EXISTS (SELECT 1 FROM conversations c WHERE c.id = m.persona_id) "
    "ELSE NOT EXISTS (SELECT 1 FROM personas p WHERE p.id = m.persona_id) END"
)
_SCHEDULE_PREFIX = "initsched:"
_SCHEDULE_PERSONA = f"substr(s.id, {len(_SCHEDULE_PREFIX) + 1})"
_SCHEDULE_ORPHAN = (
    f"s.id LIKE '{_SCHEDULE_PREFIX}%' "
    f"AND NOT EXISTS (SELECT 1 FROM personas p WHERE p.id = {_SCHEDULE_PERSONA})"
)
_NOTICE_ORPHAN = "NOT EXISTS (SELECT 1 FROM personas p WHERE p.id = n.persona_id)"

_FIND = (
    text(
        "SELECT 'memory_chunks' AS tbl, m.persona_id AS owner, m.kind AS kind, count(*) AS n "  # noqa: S608
        f"FROM memory_chunks m WHERE {_MEMORY_ORPHAN} GROUP BY m.persona_id, m.kind"
    ),
    text(
        "SELECT 'initiative_notices' AS tbl, n.persona_id AS owner, "  # noqa: S608
        "'initiative_notice' AS kind, count(*) AS n "
        f"FROM initiative_notices n WHERE {_NOTICE_ORPHAN} GROUP BY n.persona_id"
    ),
    text(
        f"SELECT 'schedules' AS tbl, {_SCHEDULE_PERSONA} AS owner, "  # noqa: S608
        "'initiative_schedule' AS kind, count(*) AS n "
        f"FROM schedules s WHERE {_SCHEDULE_ORPHAN} GROUP BY {_SCHEDULE_PERSONA}"
    ),
)

_REMOVE = {
    "memory_chunks": text(
        f"DELETE FROM memory_chunks m WHERE m.persona_id = :owner AND m.kind = :kind "  # noqa: S608
        f"AND {_MEMORY_ORPHAN}"
    ),
    "initiative_notices": text(
        f"DELETE FROM initiative_notices n WHERE n.persona_id = :owner AND {_NOTICE_ORPHAN}"  # noqa: S608
    ),
    "schedules": text(
        f"DELETE FROM schedules s WHERE {_SCHEDULE_PERSONA} = :owner AND {_SCHEDULE_ORPHAN}"  # noqa: S608
    ),
}

_AUDIT = text(
    "INSERT INTO audit_log (user_id, action, target, metadata) "
    "VALUES (:written_by, 'repair.orphaned', :owner, CAST(:metadata AS jsonb))"
)


@dataclass(frozen=True)
class OrphanGroup:
    """Rows of one kind that belong to one owner that no longer exists."""

    table: str
    owner_id: str
    kind: str
    rows: int


class OrphanSweep:
    """Reads and removes orphans. Needs a role that sees every tenant."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def find(self) -> list[OrphanGroup]:
        """Every group with no owner, evidence before what was derived from it."""
        with self._engine.connect() as conn:
            rows = [row for statement in _FIND for row in conn.execute(statement).all()]
        groups = [OrphanGroup(r.tbl, str(r.owner), str(r.kind), int(r.n)) for r in rows]
        return sorted(groups, key=lambda g: (_RANK.get(g.kind, len(_RANK)), g.owner_id))

    def remove(self, group: OrphanGroup, *, written_by: str) -> int:
        """Delete one group if its owner is still missing, and audit it. Returns the rows gone."""
        with self._engine.begin() as conn:
            removed = conn.execute(
                _REMOVE[group.table], {"owner": group.owner_id, "kind": group.kind}
            ).rowcount
            if removed:
                conn.execute(
                    _AUDIT,
                    {
                        "written_by": written_by,
                        "owner": group.owner_id,
                        "metadata": json.dumps(
                            {"table": group.table, "kind": group.kind, "rows": str(removed)}
                        ),
                    },
                )
        return removed
