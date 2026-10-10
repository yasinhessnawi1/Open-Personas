"""Index the in-flight jobs by type, for the serial-type claim (D-FV0-31).

The document index (``document_ingest``) is a SERIAL job type: the claim skips such a job
while another of its type is claimed or running, so the process embeds one slice at a
time (``persona_api/jobs/queue.py`` ``_CLAIM_SQL``). That check runs on every claim and
asks "is a job of this type in flight?". ``idx_jobs_inflight_by_owner`` answers the same
question by owner, not by type.

``idx_jobs_inflight_by_type`` is a partial index on ``type``
``WHERE state IN ('claimed', 'running')``: only in-flight jobs, a handful at any moment,
so it stays tiny however many finished jobs the hot table holds. A pure index: no table,
column or data changes, and the downgrade drops it.

Split-home (cf. 054 / 057 / 058): declared on the canonical ``persona_api.db.models``
``jobs`` table, so on a fresh database ``001_initial``'s ``metadata.create_all`` builds it
and ``IF NOT EXISTS`` below is a no-op; on a deployed Postgres it genuinely creates it.

Locking, as 058. Not ``CONCURRENTLY``: Alembic runs each migration in a transaction. A
partial index still reads the whole ``jobs`` heap under a SHARE lock that blocks job writes
until it is built; the archive sweep keeps the hot table short, so the build is brief, and
``lock_timeout`` is 5 s so that on a busy table the migration fails fast and can be retried
rather than queueing every job write behind it (set for the session at the start,
``RESET`` at the end, upgrade and downgrade alike). A community SQLite database is untouched
(an index is a speed-up, not a column it needs).

Numbering: T0 (document parsers in production) lands before FV0, whose own migration
becomes 060.

Revision ID: 059_jobs_inflight_by_type
Revises: 058_jobs_live_task_leg_index
Create Date: 2026-10-05
"""

from __future__ import annotations

from alembic import op

revision = "059_jobs_inflight_by_type"
down_revision = "058_jobs_live_task_leg_index"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_jobs_inflight_by_type ON jobs (type) "
        "WHERE state IN ('claimed', 'running')"
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute("DROP INDEX IF EXISTS idx_jobs_inflight_by_type")
    op.execute("RESET lock_timeout")
