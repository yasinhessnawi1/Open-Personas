"""R9-037 task-bridge bell — widen ``notifications_kind_check`` (design point 3+5).

When a user deletes the SCHEDULE of a schedule-backed task, the task is PAUSED
(``schedule_delete_service.delete_schedule_with_intent``) and a durable P6 bell entry
tells the user why. The entry's kind ``schedule_deleted_task_paused`` must pass the
``notifications.kind`` CHECK, which previously allowed
``('run_terminal', 'persona_ready', 'schedule_executor_missing', 'schedule_fired',
'event_trigger_dropped')`` (migrations 028/037/039/042). Drop-and-re-add with IF
EXISTS — the exact widening shape migrations 006/037 used. The canonical
``persona_api.db.models.notifications`` CheckConstraint is widened in lockstep.

The downgrade narrows the CHECK back and will (correctly) fail if
``schedule_deleted_task_paused`` rows exist — narrowing under live data is a
deliberate decision, not something a migration should silently force.

Revision ID: 049_schedule_task_paused_kind
Revises: 048_schedule_tombstones
Create Date: 2026-07-14
"""

from __future__ import annotations

from alembic import op

revision = "049_schedule_task_paused_kind"
down_revision = "048_schedule_tombstones"
branch_labels = None
depends_on = None

_OLD_KINDS = (
    "('run_terminal', 'persona_ready', 'schedule_executor_missing', "
    "'schedule_fired', 'event_trigger_dropped')"
)
_NEW_KINDS = (
    "('run_terminal', 'persona_ready', 'schedule_executor_missing', "
    "'schedule_fired', 'event_trigger_dropped', 'schedule_deleted_task_paused')"
)


def upgrade() -> None:
    op.execute("ALTER TABLE notifications DROP CONSTRAINT IF EXISTS notifications_kind_check")
    op.execute(
        f"ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check "
        f"CHECK (kind IN {_NEW_KINDS})"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE notifications DROP CONSTRAINT IF EXISTS notifications_kind_check")
    op.execute(
        f"ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check "
        f"CHECK (kind IN {_OLD_KINDS})"
    )
