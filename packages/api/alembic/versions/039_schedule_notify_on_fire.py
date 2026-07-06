"""Opt-in coalesced fire bell — ``schedules.notify_on_fire`` + widen the kind CHECK.

The reminder-bell feature: a schedule created through the user's reminder door opts in
to a coalesced P6 bell notification on each fire (one moving entry per schedule, keyed
``(owner, 'schedule_fired', schedule_id)`` and re-alerting on re-fire). Two DDL moves:

- add ``schedules.notify_on_fire BOOLEAN NOT NULL DEFAULT false`` — background/
  programmatic (A4-authored) schedules stay quiet; only the reminder door sets it true;
- widen ``notifications_kind_check`` to admit ``'schedule_fired'`` (the exact drop-and-
  re-add shape migrations 037 / 006 use), with the canonical
  ``persona_api.db.models.notifications`` CheckConstraint widened in lockstep.

The ``ADD COLUMN IF NOT EXISTS`` is the 025 / 034 precedent: ``001_initial`` runs
``metadata.create_all`` (current schema), so on a freshly-migrated DB the column already
exists and the guarded add is a harmless no-op; on an already-deployed DB it adds the
column (NOT NULL DEFAULT false backfills existing rows).

The downgrade is TOTAL (never errors under live data): ``'schedule_fired'`` rows are
ephemeral bell alerts, so it DELETEs them FIRST, THEN narrows the CHECK back, THEN drops
the column.

> Placeholder number — renumbers at merge-back onto this worktree's real head. Authored
> against the current kind set (``run_terminal``, ``persona_ready``,
> ``schedule_executor_missing``) + ``schedule_fired``; A7's parallel widen
> (``event_trigger_dropped``) is reconciled into the final constraint at merge-back.

Revises: 038_initiative
"""

from __future__ import annotations

from alembic import op

revision = "039_schedule_notify_on_fire"
down_revision = "038_initiative"
branch_labels = None
depends_on = None

_OLD_KINDS = "('run_terminal', 'persona_ready', 'schedule_executor_missing')"
_NEW_KINDS = (
    "('run_terminal', 'persona_ready', 'schedule_executor_missing', 'schedule_fired')"
)


def upgrade() -> None:
    # ADD COLUMN IF NOT EXISTS (025 / 034 precedent): 001_initial's create_all already
    # builds this on a fresh DB → no-op there; on a deployed DB it adds + backfills false.
    op.execute(
        "ALTER TABLE schedules ADD COLUMN IF NOT EXISTS notify_on_fire "
        "BOOLEAN NOT NULL DEFAULT false"
    )
    op.execute("ALTER TABLE notifications DROP CONSTRAINT IF EXISTS notifications_kind_check")
    op.execute(
        f"ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check "
        f"CHECK (kind IN {_NEW_KINDS})"
    )


def downgrade() -> None:
    # Total: 'schedule_fired' rows are ephemeral bell alerts — drop them so narrowing the
    # CHECK never fails under live data, THEN narrow, THEN drop the column.
    op.execute("DELETE FROM notifications WHERE kind = 'schedule_fired'")
    op.execute("ALTER TABLE notifications DROP CONSTRAINT IF EXISTS notifications_kind_check")
    op.execute(
        f"ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check "
        f"CHECK (kind IN {_OLD_KINDS})"
    )
    op.execute("ALTER TABLE schedules DROP COLUMN IF EXISTS notify_on_fire")
