"""A run belongs to a task; a task knows its kind (Spec W1, T1; D-W1-1 / D-W1-2).

Two additive, nullable-safe changes that close the R9-100 asymmetry at the schema:

- ``runs.task_id TEXT NULL`` + ``fk_runs_task`` (``REFERENCES tasks(id) ON DELETE SET
  NULL``) + ``idx_runs_task``. A task could already enumerate its runs
  (``tasks.run_ids``); now a run names its task. Legacy runs keep ``NULL`` and are
  archive (D-W1-15: no backfill into invented tasks). ``SET NULL`` keeps a run row
  viewable after its task is deleted.
- ``tasks.kind TEXT NOT NULL DEFAULT 'standing'`` + ``tasks_kind_check``
  (``standing`` | ``ad_hoc``). Every pre-W1 row reads ``standing`` (correct: all of
  them were A4/A8-confirmed contracts); T2's one-off dispatch writes ``ad_hoc``.

Split-home (cf. 047 / 052): both columns, the FK, the index and the CHECK are declared
on the canonical ``persona_api.db.models`` tables, so on a fresh DB ``001_initial``'s
``metadata.create_all`` already builds them and the guarded statements below are no-ops
(``IF NOT EXISTS`` on the column and index; DROP-then-ADD on the named constraints, the
049 shape); on a previously-deployed Postgres they genuinely add. RLS is inherited from
each table's existing ``user_isolation`` policy: no policy change.

Numbering (D-W1-25): authored as a placeholder off ``052_auto_topup_opt_in`` while
Spec M5 held 053 in its own worktree; renumbered to ``054_runs_task_id`` off M5's
``053_auto_topup_notification_kind`` when the branch rebased onto main.

Revision ID: 054_runs_task_id
Revises: 053_auto_topup_notification_kind
Create Date: 2026-09-06
"""

from __future__ import annotations

from alembic import op

revision = "054_runs_task_id"
down_revision = "053_auto_topup_notification_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # runs → tasks (nullable; SET NULL on task delete).
    op.execute("ALTER TABLE runs ADD COLUMN IF NOT EXISTS task_id TEXT")
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS fk_runs_task")
    op.execute(
        "ALTER TABLE runs ADD CONSTRAINT fk_runs_task "
        "FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE SET NULL"
    )
    op.execute("CREATE INDEX IF NOT EXISTS idx_runs_task ON runs (task_id)")
    # tasks.kind (standing | ad_hoc), default standing for every existing row.
    op.execute("ALTER TABLE tasks ADD COLUMN IF NOT EXISTS kind TEXT NOT NULL DEFAULT 'standing'")
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_kind_check")
    op.execute(
        "ALTER TABLE tasks ADD CONSTRAINT tasks_kind_check CHECK (kind IN ('standing','ad_hoc'))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE tasks DROP CONSTRAINT IF EXISTS tasks_kind_check")
    op.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS kind")
    op.execute("DROP INDEX IF EXISTS idx_runs_task")
    op.execute("ALTER TABLE runs DROP CONSTRAINT IF EXISTS fk_runs_task")
    op.execute("ALTER TABLE runs DROP COLUMN IF EXISTS task_id")
