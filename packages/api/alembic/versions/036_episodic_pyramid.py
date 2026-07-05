"""Episodic multi-resolution pyramid — lifecycle columns + gist kind (Spec K8, K8-D-13).

ONE migration carries K8's whole schema change, additively and reversibly:

- ``memory_chunks`` lifecycle columns (K8-D-2/3, all hash-excluded model state):
  ``strength`` (usage reinforcement, default 1), ``last_recalled_at`` (the decay
  clock's reset point), ``fidelity_band`` (the MATERIALIZED display band — written
  by the background tiering pass; a stale value between passes is harmless: it is
  display fidelity per K8-D-7/11, never correctness), ``pinned`` (the chunk-level
  constraint class — first-class so pin-toggling never trips the content-hash
  tamper check), ``member_ids`` (ordered raw-member drill pointers on gist rows).
  ``NOT NULL DEFAULT`` columns are metadata-only on PG11+ — instant at any N; the
  defaults ARE the backfill (existing episodic rows: strength 1, band 0 = FULL,
  unpinned — no data invention).
- ``memory_chunks_kind_check`` widened with ``'episodic_gist'`` (gists are ordinary
  PersonaChunks, K8-D-2). This migration is the kind's migration of record; the
  canonical ``persona_api.db.models`` declaration covers fresh DBs (001 create_all —
  the 033 split-home discipline, so every DDL statement here is guarded and a
  fresh-DB re-run is a harmless no-op).
- ``idx_memory_persona_kind_created`` — the ``recent()`` pushdown index
  (``ORDER BY (created_at, id) DESC LIMIT``; K8-D-6 moved insertion-order off ids).

RLS: NO policy change — gist rows carry a real ``persona_id`` and inherit the
``user_isolation`` persona-FK policy; the document aux policy stays non-overlapping
(its ``kind = 'document'`` gate). Proven non-vacuously in the RLS suite.

Downgrade is total (drop index, drop columns, restore the five-kind CHECK) and
FIRST DELETES ``kind='episodic_gist'`` rows: gists are derived artifacts,
regenerable from the untouched raw originals (§0) — they would violate the
restored CHECK, and removing them loses nothing the engine cannot rebuild. Raw
chunks are untouched by both directions.

Revision ID: 036_episodic_pyramid
Revises: 035_memory_seed_index

Re-parented at merge-back (orchestrator): authored in-worktree off the branch-point
head ``033_graph_lifecycle`` (branched at 616decd, before A8's 034 landed), now
linearized onto main's real head — A8 claimed 034, K5 claimed 035_memory_seed_index,
so K8 lands as 036 (A5 holds the next slot, 037). K8 targets ``memory_chunks``; K5's
035 targets ``graph_nodes`` — no DDL overlap.
"""

from __future__ import annotations

from alembic import op

revision = "036_episodic_pyramid"
down_revision = "035_memory_seed_index"
branch_labels = None
depends_on = None

_KINDS_WITH_GIST = "'identity', 'self_facts', 'worldview', 'episodic', 'document', 'episodic_gist'"
_KINDS_WITHOUT_GIST = "'identity', 'self_facts', 'worldview', 'episodic', 'document'"


def upgrade() -> None:
    # Lifecycle columns — guarded (fresh DBs already have them via create_all).
    op.execute(
        "ALTER TABLE memory_chunks ADD COLUMN IF NOT EXISTS strength INTEGER NOT NULL DEFAULT 1"
    )
    op.execute("ALTER TABLE memory_chunks ADD COLUMN IF NOT EXISTS last_recalled_at TIMESTAMPTZ")
    op.execute(
        "ALTER TABLE memory_chunks "
        "ADD COLUMN IF NOT EXISTS fidelity_band SMALLINT NOT NULL DEFAULT 0"
    )
    op.execute(
        "ALTER TABLE memory_chunks ADD COLUMN IF NOT EXISTS pinned BOOLEAN NOT NULL DEFAULT FALSE"
    )
    op.execute("ALTER TABLE memory_chunks ADD COLUMN IF NOT EXISTS member_ids TEXT[]")

    # Kind CHECK: widen with 'episodic_gist' (the 006 widening pattern).
    op.execute("ALTER TABLE memory_chunks DROP CONSTRAINT IF EXISTS memory_chunks_kind_check")
    op.execute(
        f"ALTER TABLE memory_chunks ADD CONSTRAINT memory_chunks_kind_check "
        f"CHECK (kind IN ({_KINDS_WITH_GIST}))"
    )

    # The recent() pushdown index.
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_memory_persona_kind_created "
        "ON memory_chunks (persona_id, kind, created_at DESC)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_memory_persona_kind_created")
    # Gists are derived artifacts (regenerable from untouched originals); they
    # would violate the restored five-kind CHECK. Raw chunks are untouched.
    op.execute("DELETE FROM memory_chunks WHERE kind = 'episodic_gist'")
    op.execute("ALTER TABLE memory_chunks DROP CONSTRAINT IF EXISTS memory_chunks_kind_check")
    op.execute(
        f"ALTER TABLE memory_chunks ADD CONSTRAINT memory_chunks_kind_check "
        f"CHECK (kind IN ({_KINDS_WITHOUT_GIST}))"
    )
    op.execute("ALTER TABLE memory_chunks DROP COLUMN IF EXISTS member_ids")
    op.execute("ALTER TABLE memory_chunks DROP COLUMN IF EXISTS pinned")
    op.execute("ALTER TABLE memory_chunks DROP COLUMN IF EXISTS fidelity_band")
    op.execute("ALTER TABLE memory_chunks DROP COLUMN IF EXISTS last_recalled_at")
    op.execute("ALTER TABLE memory_chunks DROP COLUMN IF EXISTS strength")
