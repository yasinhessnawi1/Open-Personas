"""Index a task's live legs by the task id in the job payload (R9-158).

"Does this task have a leg still to run?" is asked by the revival sweep's stranded check
(``tasks/revival_sweep.py`` ``_live_job_for``) and, from R9-158 on, by the task detail
route, which the task page polls every few seconds while a task is being worked so it can
show "Starting" between a Resume and the worker claiming the leg. Both filter ``jobs`` on
``type = 'task_leg'``, a live ``state`` and ``payload ->> 'task_id'``. The existing
indexes serve the claim, the lease sweep and the owner filter; none reaches the payload,
so each check scanned the owner's jobs row by row.

``idx_jobs_live_task_leg`` is a partial expression index on ``(payload ->> 'task_id')``
``WHERE type = 'task_leg' AND state IN ('queued', 'claimed', 'running')``: only live task
legs, so it stays small however many finished jobs the hot table holds. The queries in
``persona_api/tasks/live_legs.py`` spell the expression exactly this way, with the key, the
job type and the states as SQL literals, so the planner can use the index even under a
generic plan. (SQLAlchemy's own ``payload["task_id"].as_string()`` renders
``CAST((payload ->> $1) AS VARCHAR)``, which a generic plan could never match; the review of
R9-158 found this docstring claiming the opposite.) ``test_migration_task_state_truth.py``
asserts the index is chosen under ``plan_cache_mode = force_generic_plan``.

Split-home (cf. 054 / 057): declared on the canonical ``persona_api.db.models`` ``jobs``
table, so on a fresh database ``001_initial``'s ``metadata.create_all`` builds it and
``IF NOT EXISTS`` below is a no-op; on a deployed Postgres it genuinely creates it.

Locking. Not ``CONCURRENTLY``: Alembic runs each migration in a transaction. A partial
index still reads the WHOLE ``jobs`` heap to find the rows its predicate admits, under a
SHARE lock that blocks every job write (enqueue, claim, finish) until it is built; the
partial predicate only keeps the index itself small. The hot table is kept short by the
archive sweep, so the build is brief, and ``lock_timeout`` is 5 s so that on a busy table
the migration fails fast and can be retried rather than queueing every job write behind it
(set for the session at the start, ``RESET`` at the end, upgrade and downgrade alike, so it
does not leak into later migrations in the same run).
A community SQLite database is untouched on upgrade (an index is a speed-up, not a column
it needs), and the index is Postgres-only (``->>`` needs SQLite 3.38).

Numbering: authored on ``057_runs_stop_reason`` (same branch, R9-158). If another
migration claims 058 before this merges, re-point ``down_revision`` at merge-back.

Revision ID: 058_jobs_live_task_leg_index
Revises: 057_runs_stop_reason
Create Date: 2026-09-26
"""

from __future__ import annotations

from alembic import op

revision = "058_jobs_live_task_leg_index"
down_revision = "057_runs_stop_reason"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_live_task_leg ON jobs ((payload ->> 'task_id')) "
        "WHERE type = 'task_leg' AND state IN ('queued', 'claimed', 'running')"
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS idx_jobs_live_task_leg")
    op.execute("RESET lock_timeout")
