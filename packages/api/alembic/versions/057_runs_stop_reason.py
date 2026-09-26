"""A run records why it was stopped early (R9-158).

A task leg that is stopped before it ends on its own was written as a bare
``cancelled``, whichever thing stopped it: the user pausing or cancelling the task,
a box bound (steps, wall clock, the per-leg spend), the deploy drain, or the approval
gate. The task page then showed "cancelled" beside a task the user had only paused,
which is half of the contradiction R9-158 recorded.

``runs.stop_reason TEXT NULL`` + ``runs_stop_reason_check`` over the seven reasons
(``paused``, ``cancelled``, ``budget``, ``wall_clock``, ``steps``, ``drain``,
``approval``; the vocabulary is ``persona_api.services.run_record.RunStopReason``).
NULL means the run ended on its own, and it is what every row written before this
migration keeps: there is no honest way to reconstruct the reason, so no backfill.
No index: nothing filters on it.

Locking (R9-158 review): ``lock_timeout`` is 5 s, so on a busy table the migration fails
fast and can be retried instead of queueing every run write behind itself. It is set for
the session at the start and ``RESET`` at the end, upgrade and downgrade alike, so it does
not leak into later migrations in the same run. The column is nullable with no default, a
catalog-only change. The CHECK is added ``NOT VALID`` in the migration's transaction
(a brief exclusive lock, no scan of existing rows); that transaction is committed, and
``VALIDATE CONSTRAINT`` then runs outside it (``op.get_context().autocommit_block()``),
scanning the existing rows under a lock that does not block reads or writes. Validating
inside the same transaction would gain nothing: the ``ADD``'s exclusive lock is held until
commit, so the scan would block writes exactly as a plain ``ADD CONSTRAINT`` does.

Split-home (cf. 054 / 056): the column and the CHECK are declared on the canonical
``persona_api.db.models`` ``runs`` table, so on a fresh database ``001_initial``'s
``metadata.create_all`` already builds them and the guarded statements below are
no-ops (``IF NOT EXISTS`` on the column; DROP-then-ADD on the named constraint); on a
deployed Postgres they genuinely add. RLS is inherited from the table's existing
``user_isolation`` policy: no policy change. A community SQLite database gains the
column through ``reconcile_community_schema`` on boot.

Numbering: authored on ``056_call_end_reason_vocabulary``, the head at the time
(``uv run alembic heads``). Every local branch and ``origin/main`` was checked with
``git ls-tree`` for an in-flight 057, 06x or ``XXX_`` placeholder, and none existed.
If another migration claims 057 before this merges, re-point ``down_revision`` at
merge-back.

Revision ID: 057_runs_stop_reason
Revises: 056_call_end_reason_vocabulary
Create Date: 2026-09-26
"""

from __future__ import annotations

from alembic import op

revision = "057_runs_stop_reason"
down_revision = "056_call_end_reason_vocabulary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS stop_reason TEXT")
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_stop_reason_check")
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT runs_stop_reason_check CHECK ("
        "stop_reason IS NULL OR stop_reason IN ('paused', 'cancelled', 'budget', "
        "'wall_clock', 'steps', 'drain', 'approval')) NOT VALID"
    )
    # Commit the NOT VALID add (releasing its exclusive lock), then scan outside it.
    with op.get_context().autocommit_block():
        op.execute("ALTER TABLE runs VALIDATE CONSTRAINT runs_stop_reason_check")
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS runs_stop_reason_check")
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS stop_reason")
    op.execute("RESET lock_timeout")
