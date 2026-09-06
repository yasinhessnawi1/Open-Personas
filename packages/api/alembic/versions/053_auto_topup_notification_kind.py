"""Spec M5 (B2) — widen ``notifications_kind_check`` for the auto-top-up bell.

An auto-top-up that needs the cardholder (``REQUIRES_ACTION`` after a 3DS challenge, or
``NO_CUSTOMER`` when nothing is saved) must reach the user: the charge cannot complete
without them, and silence means a balance that runs out with no explanation (§1c.6).
That durable bell entry carries kind ``auto_topup``, which has to pass the
``notifications.kind`` CHECK.

Drop-and-re-add with IF EXISTS, the exact widening shape migrations 006/037/049 used.
The canonical ``persona_api.db.models.notifications`` CheckConstraint is widened in
lockstep, so the ORM metadata and the live schema never disagree.

The downgrade narrows the CHECK back and will (correctly) fail if ``auto_topup`` rows
exist — narrowing under live data is a deliberate decision, not something a migration
should silently force.

Revision ID: 053_auto_topup_notification_kind
Revises: 052_auto_topup_opt_in
Create Date: 2026-09-02
"""

from __future__ import annotations

from alembic import op

revision = "053_auto_topup_notification_kind"
down_revision = "052_auto_topup_opt_in"
branch_labels = None
depends_on = None

_OLD_KINDS = (
    "('run_terminal', 'persona_ready', 'schedule_executor_missing', "
    "'schedule_fired', 'event_trigger_dropped', 'schedule_deleted_task_paused')"
)
_NEW_KINDS = (
    "('run_terminal', 'persona_ready', 'schedule_executor_missing', "
    "'schedule_fired', 'event_trigger_dropped', 'schedule_deleted_task_paused', "
    "'auto_topup')"
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
