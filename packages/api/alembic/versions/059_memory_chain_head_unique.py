"""One current version per chain, enforced by the database (R9-205).

``TypedStore.write`` reads a chain, computes version N+1 and writes it, with nothing around the
three steps, so two writers that read the same chain both wrote N+1 and the chain then refused
every later write. ``uq_memory_chain_head`` makes the second writer fail instead, and the store
turns that into a re-read and an append on top of what won.

The predicate leaves out rows with no logical id. Identity and document rows belong to no chain
and there are many of them, so they stay out of an index that only exists for chains.

**This refuses rather than repairs.** The index cannot be built while any chain has two heads,
and a schema step is the wrong place to settle one: it has no dry run, it has no audit trail
(D-K13-7, D-K13-16), and it would be rewriting people's memory on the way through a deploy. So a
chain with two heads stops the upgrade with a message saying what to run, and the repair is
``persona repair``, an operator's command with both.

**A refusal changes nothing.** Everything here runs inside the one transaction Alembic opens, so
the database comes out as it went in: no index, the old revision, every row untouched, and
running the upgrade again after the repair simply works. ``CREATE UNIQUE INDEX`` is the guarantee.
The check above it only exists so the failure is a sentence rather than a duplicate key error
quoting someone's persona id.

Not ``CONCURRENTLY``, for a stronger reason than usual. A concurrent build runs outside the
transaction, and when it fails it leaves an INVALID index behind, which ``IF NOT EXISTS`` would
then take for a finished one. That is the one way this migration could report success with
nothing enforcing the invariant. The table is small and this first runs during a restore, with
nothing else connected, so the plain build costs nothing. The SHARE lock is taken before the check
so the check and the build read the same rows, under the same ``lock_timeout`` shape as 058.

Split-home (cf. 054 / 057 / 058): declared on the canonical ``persona_api.db.models``
``memory_chunks`` table, so on a fresh database ``001_initial``'s ``metadata.create_all`` builds
it and ``IF NOT EXISTS`` below is a no-op; on a deployed Postgres it genuinely creates it. A
community SQLite database has no ``memory_chunks`` table and never runs this chain.

Numbering: authored on ``058_jobs_live_task_leg_index``. If another migration claims 059 before
this merges, re-point ``down_revision`` at merge-back.

Revision ID: 059_memory_chain_head_unique
Revises: 058_jobs_live_task_leg_index
Create Date: 2026-11-06
"""

from __future__ import annotations

from alembic import context, op
from persona_api.errors import ChainHeadIndexInvalidError, ChainHeadsConflictError
from sqlalchemy import text

revision = "059_memory_chain_head_unique"
down_revision = "058_jobs_live_task_leg_index"
branch_labels = None
depends_on = None

_CONFLICTS_BY_KIND = text(
    """
    SELECT kind, count(*) FROM (
        SELECT kind FROM memory_chunks
         WHERE logical_id IS NOT NULL AND superseded_by IS NULL
         GROUP BY persona_id, kind, logical_id HAVING count(*) > 1
    ) AS conflicted
    GROUP BY kind ORDER BY kind
    """
)


def _refuse_if_any_chain_has_two_heads() -> None:
    """Stop with a sentence rather than a duplicate key error. Counts only, never row content."""
    if context.is_offline_mode():  # ``alembic upgrade --sql`` has no rows to look at
        return
    by_kind = [(str(row[0]), int(row[1])) for row in op.get_bind().execute(_CONFLICTS_BY_KIND)]
    if not by_kind:
        return
    chains = sum(count for _, count in by_kind)
    named = ", ".join(f"{kind} {count}" for kind, count in by_kind)
    msg = (
        f"{chains} memory version chains have two current versions ({named}), so the index "
        "that prevents this cannot be built. Nothing has been changed. To see them, run "
        '`persona repair --all-personas --database-url "$DATABASE_URL"`, and to settle them add '
        "`--resolve-raced --apply`, which keeps every version. Then run the upgrade again."
    )
    raise ChainHeadsConflictError(msg, context={"chains": str(chains), "by_kind": named})


_EXISTING_VALIDITY = text(
    """
    SELECT i.indisvalid FROM pg_index i
      JOIN pg_class c ON c.oid = i.indexrelid
      JOIN pg_namespace n ON n.oid = c.relnamespace
     WHERE c.relname = 'uq_memory_chain_head' AND n.nspname = current_schema()
    """
)


def _refuse_if_an_invalid_index_has_the_name() -> None:
    """An INVALID index of this name would be taken as built by ``IF NOT EXISTS``.

    A ``CREATE INDEX CONCURRENTLY`` that failed, by hand or by a future migration, leaves one,
    and it enforces nothing. Accepting it would report the upgrade done with the invariant off.
    """
    if context.is_offline_mode():
        return
    valid = op.get_bind().execute(_EXISTING_VALIDITY).scalar()
    if valid is None or valid:
        return
    msg = (
        "an index named uq_memory_chain_head already exists but is INVALID, which means an "
        "earlier build of it failed, and it enforces nothing. Nothing has been changed. Drop it "
        "with `DROP INDEX uq_memory_chain_head;` and run the upgrade again, which builds it "
        "properly or says what stops it."
    )
    raise ChainHeadIndexInvalidError(msg, context={"index": "uq_memory_chain_head"})


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("LOCK TABLE memory_chunks IN SHARE MODE")
    _refuse_if_an_invalid_index_has_the_name()
    _refuse_if_any_chain_has_two_heads()
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_memory_chain_head "
        "ON memory_chunks (persona_id, kind, logical_id) "
        "WHERE superseded_by IS NULL AND logical_id IS NOT NULL"
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS uq_memory_chain_head")
    op.execute("RESET lock_timeout")
