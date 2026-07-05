"""A10 deleted-executor bell — widen ``notifications_kind_check`` (A10-D-7).

Spec A10. The fire bridge's deleted-executor degrade pauses the schedule and writes a
durable P6 bell entry; the entry's kind ``schedule_executor_missing`` must pass the
``notifications.kind`` CHECK, which previously allowed only
``('run_terminal', 'persona_ready')`` (migration 028). Drop-and-re-add with IF EXISTS —
the exact widening shape migration 006 used on ``memory_chunks_kind_check``. The
canonical ``persona_api.db.models.notifications`` CheckConstraint is widened in lockstep.

The downgrade narrows the CHECK back and will (correctly) fail if
``schedule_executor_missing`` rows exist — narrowing under live data is a deliberate
decision, not something a migration should silently force.

> Re-parented at merge-back: authored off ``035_memory_seed_index``; A10 merged
> before A5, so it lands as 037 onto K8's 036 (A5 takes 038 when it merges).

Revises: 036_episodic_pyramid
"""

from __future__ import annotations

from alembic import op

revision = "037_notifications_schedule_kind"
down_revision = "036_episodic_pyramid"
branch_labels = None
depends_on = None

_OLD_KINDS = "('run_terminal', 'persona_ready')"
_NEW_KINDS = "('run_terminal', 'persona_ready', 'schedule_executor_missing')"


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
