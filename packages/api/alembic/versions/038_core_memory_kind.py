"""K9 core-memory block — widen ``memory_chunks_kind_check`` with ``'core_memory'`` (K9-D-10).

Spec K9. The always-in-context core-memory block is stored as a ``memory_chunks`` row with
``kind='core_memory'`` — one append-only versioned summary per persona (K9-D-10 / the
approved DDL-preview): it reuses the chunk transport (persona-scoped RLS already covers it,
kind-agnostic), the D-07-4 versioning chain (refresh appends a new version, never destroys
the prior summary — §0 provenance-preserving), and is **excluded from recall by
construction** (K9's fusion legs query only ``'episodic'`` / ``'episodic_gist'``, never
``'core_memory'``). Drop-and-re-add with IF EXISTS — the exact widening shape migrations 006
and 036 used. The canonical ``persona_api.db.models.memory_chunks`` CheckConstraint is
widened in lockstep.

The downgrade is total and reversible (the 036 discipline): it DELETEs the derived
``core_memory`` rows (regenerable from the untouched originals on the next engine pass) so
the restored six-kind CHECK holds. RLS is unchanged; community/Chroma is schemaless (the
kind is metadata there — zero parallel implementation, K8-D-2).

> Re-parented at merge-back: authored off ``037_notifications_schedule_kind`` (this
> worktree's head; K8's 036 + A10's 037 upstream). The orchestrator renumbers in landing
> order — main carries A5's 038 + A6/A7/N6 placeholders at 039/040, so this takes the next
> free number when K9 merges. The six-kind ``_WITHOUT`` baseline correctly assumes 036's
> ``'episodic_gist'`` is already upstream.

Revises: 037_notifications_schedule_kind
"""

from __future__ import annotations

from alembic import op

revision = "038_core_memory_kind"
down_revision = "037_notifications_schedule_kind"
branch_labels = None
depends_on = None

_WITHOUT = "'identity', 'self_facts', 'worldview', 'episodic', 'document', 'episodic_gist'"
_WITH_CORE = (
    "'identity', 'self_facts', 'worldview', 'episodic', 'document', 'episodic_gist', 'core_memory'"
)


def upgrade() -> None:
    op.execute("ALTER TABLE memory_chunks DROP CONSTRAINT IF EXISTS memory_chunks_kind_check")
    op.execute(
        f"ALTER TABLE memory_chunks ADD CONSTRAINT memory_chunks_kind_check "
        f"CHECK (kind IN ({_WITH_CORE}))"
    )


def downgrade() -> None:
    # The core block is a derived artifact (regenerable from untouched originals); it would
    # violate the restored six-kind CHECK. Raw chunks + gists are untouched.
    op.execute("DELETE FROM memory_chunks WHERE kind = 'core_memory'")
    op.execute("ALTER TABLE memory_chunks DROP CONSTRAINT IF EXISTS memory_chunks_kind_check")
    op.execute(
        f"ALTER TABLE memory_chunks ADD CONSTRAINT memory_chunks_kind_check "
        f"CHECK (kind IN ({_WITHOUT}))"
    )
