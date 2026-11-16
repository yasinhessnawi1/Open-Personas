"""Record an erasure for every persona the graph names that no longer exists (R9-284).

Before R9-284 deleting a persona left the graph alone: its nodes, snapshots, edges and entity
aliases still name it. The write guard (D-R9-284-write-guard) holds every persona a node's trail
names, so a user's own later write to such a node, a ``correct_node`` say, is refused while the
trail names a persona with no row. The sweep's graph backstop removes a persona from the graph,
but only one the erasure ledger records as erased.

This writes that record: one completed persona erasure for each (owner, persona) the owner's
graph names where the persona has no row and the owner has no erasure of it. The next sweep
finds it (it completed within the backstop's every-sweep window) and runs the graph step, which
is the same step a live erasure runs. Pure SQL, ids and nothing else; content is never read.

Idempotent: a pair that already has an erasure row is skipped, and the id is derived from the
pair, so a re-run inserts nothing. Postgres only (the graph is Postgres-only). The production
host had no such persona when this was written (68 nodes, 22 edges, 16 personas), so there it
inserts nothing; it is here for any other database migrated from before R9-284.

Cost: one pass over each graph table (nodes, versions, edges, entities) and the personas and
erasures it checks against; no index helps a scan of every trail entry, and once is cheap.

It must see every row. Under row level security a role that is neither a superuser nor
BYPASSRLS would see none of another owner's graph or personas, and would silently insert nothing
(review round 5 LOW-4); such a run raises instead, before it inserts anything.

Revision ID: 063_graph_erasure_backfill
Revises: 062_memory_owner_guard
Create Date: 2026-11-16
"""

from __future__ import annotations

from alembic import op

revision = "063_graph_erasure_backfill"
down_revision = "062_memory_owner_guard"
branch_labels = None
depends_on = None


#: The tables the backfill reads or writes; it refuses to run where RLS would hide their rows.
_TABLES = (
    "graph_nodes",
    "graph_node_versions",
    "graph_edges",
    "graph_entities",
    "personas",
    "erasures",
)

ROLE_CHECK = f"""
DO $check$
BEGIN
    IF NOT (SELECT rolsuper OR rolbypassrls FROM pg_roles WHERE rolname = current_user)
       AND EXISTS (
           SELECT 1 FROM pg_class c
           JOIN pg_namespace ns ON ns.oid = c.relnamespace
           WHERE ns.nspname = current_schema()
             AND c.relname IN ({", ".join(f"'{t}'" for t in _TABLES)})
             AND c.relrowsecurity
             AND (c.relforcerowsecurity OR pg_get_userbyid(c.relowner) <> current_user)
       )
    THEN
        RAISE EXCEPTION USING MESSAGE = '063_graph_erasure_backfill: role ' || current_user
            || ' would not see every row under row level security; run the migration as a'
            || ' superuser or a BYPASSRLS role';
    END IF;
END
$check$
"""


def _elements(column: str) -> str:
    """The entries of a JSONB list column, or none when it is not a list."""
    return (
        f"jsonb_array_elements(CASE WHEN jsonb_typeof({column}::jsonb) = 'array' "
        f"THEN {column}::jsonb ELSE '[]'::jsonb END)"
    )


BACKFILL = f"""
WITH named AS (
    SELECT n.owner_id, e ->> 'persona_id' AS persona_id
    FROM graph_nodes n, {_elements("n.provenance")} AS e
    UNION
    SELECT v.owner_id, e ->> 'persona_id'
    FROM graph_node_versions v, {_elements("v.provenance")} AS e
    UNION
    SELECT g.owner_id, g.provenance::jsonb ->> 'persona_id'
    FROM graph_edges g WHERE jsonb_typeof(g.provenance::jsonb) = 'object'
    UNION
    SELECT x.owner_id, x.provenance::jsonb ->> 'persona_id'
    FROM graph_entities x WHERE jsonb_typeof(x.provenance::jsonb) = 'object'
    UNION
    SELECT x.owner_id, a ->> 'persona_id'
    FROM graph_entities x, {_elements("x.aliases")} AS a
)
INSERT INTO erasures (
    id, owner_id, subject_kind, subject_id, state, captured, counts,
    requested_at, updated_at, completed_at
)
SELECT
    'erasure_' || md5('graph-backfill:' || named.owner_id || ':' || named.persona_id),
    named.owner_id, 'persona', named.persona_id, 'complete', '{{}}', '{{}}',
    now(), now(), now()
FROM named
-- Minted ids only. The ledger refuses any other (the erase job's payload and
-- begin_persona_erasure check is_persona_id), and every persona row's id is minted by
-- new_persona_id (persona_service), the only writer of personas; a trail names a persona through
-- its row. So a non-minted id can only be a hand-edited trail, which no erasure could act on.
WHERE named.persona_id ~ '^persona_[0-9a-f]{{32}}$'
  AND NOT EXISTS (SELECT 1 FROM personas p WHERE p.id = named.persona_id)
  AND NOT EXISTS (
      SELECT 1 FROM erasures r
      WHERE r.subject_kind = 'persona'
        AND r.owner_id = named.owner_id
        AND r.subject_id = named.persona_id
  )
ON CONFLICT (id) DO NOTHING
"""


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(ROLE_CHECK)
    op.execute(BACKFILL)


def downgrade() -> None:
    """Nothing to undo: the rows record erasures that are real, and the sweep acts on them."""
