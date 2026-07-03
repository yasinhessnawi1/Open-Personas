"""Spec A8 — schedules & calendar: per-user ``users.timezone`` (+ later ``schedules.revision``).

A8 finishes *time*: one enriched, timezone-correct schedule mechanism behind two
twin interfaces. This is the spec's single migration (A8-D-10 — one-spec-one-
migration; the orchestrator linearizes once at merge-back). It is built up across
the spec's Group-A tasks against this one unmerged file:

- **T1 (this change): ``users.timezone TEXT NULL``** — the per-user IANA zone that
  falls back to ``PERSONA_DEFAULT_TIMEZONE`` when unset (A8-D-9). Nullable, no
  backfill (NULL = "use the config default"); no RLS change (``users`` is not
  RLS-scoped); no CHECK (IANA validity is enforced at the write boundary, 422).
- **T3: ``schedules.revision INTEGER NOT NULL DEFAULT 0``** — the optimistic-CAS
  guard for the mid-flight edit race (A8-D-7).
- **T5 (later, same file): nullable quiet-hours window columns** (A8-D-6), if T5's
  design keeps them on ``users``.

Split-home (cf. migrations 020 / 023 / 025 / 029): the same columns are declared on
the canonical ``persona_api.db.models`` tables, so on a fresh DB — including the
community SQLite edition — ``001_initial``'s ``metadata.create_all`` already builds
them and the guarded ``ADD COLUMN IF NOT EXISTS`` below is a harmless no-op; on a
previously-deployed Postgres it actually adds the column. Both agree.

Revision ID: 030_schedules_calendar
Revises: 029_users_identity_names
Create Date: 2026-07-03

PLACEHOLDER down_revision (R-19-1 chain numbering): this worktree branched off main
@ ``3852740`` whose real head is ``029_users_identity_names``. R5's close-out is
imminent and carries two migrations that may claim 030/031 first; the orchestrator
linearizes the single-head chain at merge-back, so the revision number +
``down_revision`` are RECOMPUTED then (expect ~``032``). Do NOT rely on ``030``
surviving verbatim.
"""

from __future__ import annotations

from alembic import op

revision = "034_schedules_calendar"
down_revision = "033_graph_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # T1 — per-user timezone. ``ADD COLUMN IF NOT EXISTS`` per the 020 / 023 / 025 /
    # 029 precedent: ``001_initial`` builds the schema from the live models (which now
    # declare ``users.timezone``), so on a fresh DB the column already exists when this
    # runs (harmless no-op); on a DB that ran 001 before A8 it is genuinely added. NULL
    # default = "use PERSONA_DEFAULT_TIMEZONE" — no data migration.
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS timezone TEXT")

    # T3 — the optimistic-CAS guard for the mid-flight edit race (A8-D-7). NOT NULL
    # DEFAULT 0: existing rows backfill to 0, and every store mutation bumps it via a
    # compare-and-swap so an edit landing between a fire and its re-arm is detected
    # rather than silently lost. Same split-home no-op-on-fresh-DB posture as above.
    op.execute("ALTER TABLE schedules ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 0")

    # T5 — per-user quiet hours (A8-D-6), local minutes-of-day [start, end) in the user's
    # timezone; both NULL = OFF (off-until-set). Scheduling into the window warns + offers
    # the nearest edge (never a silent shift); A5 reads the same definition.
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS quiet_hours_start INTEGER")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS quiet_hours_end INTEGER")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS quiet_hours_end")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS quiet_hours_start")
    op.execute("ALTER TABLE schedules DROP COLUMN IF EXISTS revision")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS timezone")
