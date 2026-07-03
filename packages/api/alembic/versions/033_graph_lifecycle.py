"""Graph memory consolidation & lifecycle (Spec K7, K7-D-X-migration).

Turns the knowledge graph from an append-only accumulator into a consolidated,
valid-time-versioned, evidence-salient long-term layer — additively, reversibly,
LLM-free on the write path (§0). ONE migration carries every schema change:

- ``graph_nodes``: ``salience`` / ``last_evidence_epoch`` (K7-D-6 evidence salience,
  ordinal-clock, never wall time), ``merged_into`` (K7-D-4 soft merge reference — NO
  FK, so K5's future delete-a-canonical does not cascade the cluster), ``updated_at``
  (K7-D-4 dirty-neighbourhood watermark, backfilled ``:= created_at``) + its index.
- ``graph_edges``: valid-time windows (``valid_at`` backfilled ``:= created_at``,
  ``invalid_at`` / ``invalidated_by`` / ``invalidated_at``); PK moves off ``id`` to a
  BIGINT ``edge_key`` surrogate so a window-closed fact and its re-asserted open
  successor coexist (same fact-``id``); a partial ``UNIQUE (id) WHERE invalid_at IS
  NULL`` keeps one open edge per fact (the idempotent-upsert conflict target); the
  ``invalid_at > valid_at`` CHECK; traversal indexes scoped to open edges.
- New ``graph_node_versions`` (K7-D-1): window-closed prior node states (content +
  embedding + trail snapshot) — the §0-honest fix for ``_evolve``'s in-place
  overwrite. Composite FK to ``(id, owner_id)`` ON DELETE CASCADE; direct owner_id
  RLS (the 011 template, ENABLE + FORCE, fail-closed).
- New ``graph_consolidation_markers`` (K7-D-4/-6): per-owner ordinal epoch +
  dirty-scan watermark; direct owner_id RLS.
- HNSW indexes recreated ``WITH (m = 16, ef_construction = 200)`` (K7-D-9). Tables
  are young/small → plain DROP/CREATE (a brief index-build lock, noted).

Split-home discipline (cf. 020 / 023 / 025 / 011): the canonical
``persona_api.db.models`` now declares every column/table above, so on a fresh DB
``001_initial``'s ``create_all`` already builds them and the guarded ``ADD COLUMN IF
NOT EXISTS`` / existence-checked structural changes below are harmless no-ops; on a
previously-deployed Postgres they actually apply. Both ends agree (the K0 contract
test extends to the new objects).

Revision ID: 030_graph_lifecycle
Revises: 029_users_identity_names

Renumbered at merge-back (R-19-1 chain numbering): authored as 030 off the
branch-point head ``029_users_identity_names``; landed after R7 claimed 032
(``032_dow_caps``), so K7 claims 033. A8 renumbers onto 034 next.
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import graph_consolidation_markers, graph_node_versions

revision = "033_graph_lifecycle"
down_revision = "032_dow_caps"
branch_labels = None
depends_on = None

_CUR = "current_setting('app.current_user_id', true)"
_NEW_RLS: tuple[tuple[str, str], ...] = (
    ("graph_node_versions", f"owner_id = {_CUR}"),
    ("graph_consolidation_markers", f"owner_id = {_CUR}"),
)


def upgrade() -> None:
    bind = op.get_bind()

    # --- graph_nodes: lifecycle columns (additive; guarded no-op on a fresh DB) ---
    op.execute(
        "ALTER TABLE graph_nodes ADD COLUMN IF NOT EXISTS salience REAL NOT NULL DEFAULT 1.0"
    )
    op.execute(
        "ALTER TABLE graph_nodes "
        "ADD COLUMN IF NOT EXISTS last_evidence_epoch BIGINT NOT NULL DEFAULT 0"
    )
    op.execute("ALTER TABLE graph_nodes ADD COLUMN IF NOT EXISTS merged_into TEXT")
    # updated_at is backfilled := created_at (explicit backfill — the Graphiti #1489
    # lesson), so it is added NULLABLE, backfilled, then defaulted + NOT NULL.
    op.execute("ALTER TABLE graph_nodes ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ")
    op.execute("UPDATE graph_nodes SET updated_at = created_at WHERE updated_at IS NULL")
    op.execute("ALTER TABLE graph_nodes ALTER COLUMN updated_at SET DEFAULT now()")
    op.execute("ALTER TABLE graph_nodes ALTER COLUMN updated_at SET NOT NULL")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_graph_nodes_updated_at ON graph_nodes (owner_id, updated_at)"
    )

    # --- graph_edges: valid-time windows + PK surrogate + partial unique ----------
    op.execute("ALTER TABLE graph_edges ADD COLUMN IF NOT EXISTS valid_at TIMESTAMPTZ")
    op.execute("UPDATE graph_edges SET valid_at = created_at WHERE valid_at IS NULL")
    op.execute("ALTER TABLE graph_edges ALTER COLUMN valid_at SET DEFAULT now()")
    op.execute("ALTER TABLE graph_edges ALTER COLUMN valid_at SET NOT NULL")
    op.execute("ALTER TABLE graph_edges ADD COLUMN IF NOT EXISTS invalid_at TIMESTAMPTZ")
    op.execute("ALTER TABLE graph_edges ADD COLUMN IF NOT EXISTS invalidated_by TEXT")
    op.execute("ALTER TABLE graph_edges ADD COLUMN IF NOT EXISTS invalidated_at TIMESTAMPTZ")
    # PK id → edge_key surrogate (only on a deployed DB where edge_key is absent; a
    # fresh 001 create_all already built graph_edges with the edge_key PK).
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'graph_edges' AND column_name = 'edge_key'
            ) THEN
                ALTER TABLE graph_edges DROP CONSTRAINT graph_edges_pkey;
                ALTER TABLE graph_edges ADD COLUMN edge_key BIGINT GENERATED ALWAYS AS IDENTITY;
                ALTER TABLE graph_edges ADD PRIMARY KEY (edge_key);
            END IF;
        END $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint WHERE conname = 'graph_edges_window_check'
            ) THEN
                ALTER TABLE graph_edges ADD CONSTRAINT graph_edges_window_check
                    CHECK (invalid_at IS NULL OR invalid_at > valid_at);
            END IF;
        END $$;
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_graph_edges_open_id "
        "ON graph_edges (id) WHERE invalid_at IS NULL"
    )
    # Recreate the traversal indexes scoped to OPEN edges (deployed DBs have the
    # non-partial form; DROP+CREATE converges both ends).
    op.execute("DROP INDEX IF EXISTS ix_graph_edges_src")
    op.execute("DROP INDEX IF EXISTS ix_graph_edges_dst")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_graph_edges_src "
        "ON graph_edges (owner_id, src_node_id, link_type) WHERE invalid_at IS NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_graph_edges_dst "
        "ON graph_edges (owner_id, dst_node_id, link_type) WHERE invalid_at IS NULL"
    )

    # --- new tables: node-version history + consolidation markers -----------------
    graph_node_versions.create(bind, checkfirst=True)
    graph_consolidation_markers.create(bind, checkfirst=True)
    for table, predicate in _NEW_RLS:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table}")
        op.execute(
            f"CREATE POLICY user_isolation ON {table} USING ({predicate}) WITH CHECK ({predicate})"
        )

    # --- HNSW recreate with explicit build params (K7-D-9) ------------------------
    op.execute("DROP INDEX IF EXISTS ix_graph_nodes_embedding_hnsw")
    op.execute(
        "CREATE INDEX ix_graph_nodes_embedding_hnsw ON graph_nodes "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 200)"
    )
    op.execute("DROP INDEX IF EXISTS ix_graph_entities_name_hnsw")
    op.execute(
        "CREATE INDEX ix_graph_entities_name_hnsw ON graph_entities "
        "USING hnsw (name_embedding vector_cosine_ops) WITH (m = 16, ef_construction = 200)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    # Restore plain HNSW indexes (no build params).
    op.execute("DROP INDEX IF EXISTS ix_graph_entities_name_hnsw")
    op.execute(
        "CREATE INDEX ix_graph_entities_name_hnsw ON graph_entities "
        "USING hnsw (name_embedding vector_cosine_ops)"
    )
    op.execute("DROP INDEX IF EXISTS ix_graph_nodes_embedding_hnsw")
    op.execute(
        "CREATE INDEX ix_graph_nodes_embedding_hnsw ON graph_nodes "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # Drop the new tables (their RLS drops with them).
    graph_consolidation_markers.drop(bind, checkfirst=True)
    graph_node_versions.drop(bind, checkfirst=True)

    # graph_edges: restore id as PK, drop windows.
    op.execute("DROP INDEX IF EXISTS ix_graph_edges_dst")
    op.execute("DROP INDEX IF EXISTS ix_graph_edges_src")
    op.execute("DROP INDEX IF EXISTS uq_graph_edges_open_id")
    op.execute("CREATE INDEX ix_graph_edges_src ON graph_edges (owner_id, src_node_id, link_type)")
    op.execute("CREATE INDEX ix_graph_edges_dst ON graph_edges (owner_id, dst_node_id, link_type)")
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'graph_edges_window_check') THEN "
        "ALTER TABLE graph_edges DROP CONSTRAINT graph_edges_window_check; END IF; END $$;"
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_name = 'graph_edges' AND column_name = 'edge_key'
            ) THEN
                -- restore one-open-edge-per-fact rows to a unique id before re-PKing
                DELETE FROM graph_edges WHERE invalid_at IS NOT NULL;
                ALTER TABLE graph_edges DROP CONSTRAINT graph_edges_pkey;
                ALTER TABLE graph_edges DROP COLUMN edge_key;
                ALTER TABLE graph_edges ADD PRIMARY KEY (id);
            END IF;
        END $$;
        """
    )
    op.execute("ALTER TABLE graph_edges DROP COLUMN IF EXISTS invalidated_at")
    op.execute("ALTER TABLE graph_edges DROP COLUMN IF EXISTS invalidated_by")
    op.execute("ALTER TABLE graph_edges DROP COLUMN IF EXISTS invalid_at")
    op.execute("ALTER TABLE graph_edges DROP COLUMN IF EXISTS valid_at")

    # graph_nodes: drop lifecycle columns.
    op.execute("DROP INDEX IF EXISTS ix_graph_nodes_updated_at")
    op.execute("ALTER TABLE graph_nodes DROP COLUMN IF EXISTS updated_at")
    op.execute("ALTER TABLE graph_nodes DROP COLUMN IF EXISTS merged_into")
    op.execute("ALTER TABLE graph_nodes DROP COLUMN IF EXISTS last_evidence_epoch")
    op.execute("ALTER TABLE graph_nodes DROP COLUMN IF EXISTS salience")
