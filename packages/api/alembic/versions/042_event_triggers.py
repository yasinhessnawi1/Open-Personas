"""Add the A7 event-trigger registry + the storm-drop bell kind (Spec A7, A7-D-5 / A7-D-8).

Two additive pieces:

- ``event_triggers`` — the per-owner RLS-scoped trigger registry the dispatcher matches at each
  event birth point (owner+kind+enabled indexed lookup, no model call). RLS ENABLE + FORCE + a
  ``user_isolation`` policy are created ENTIRELY here (the 009/011/012/015/021/032/038 split-home
  template). Idempotent + split-home: ``Table.create(checkfirst=True)`` no-ops on a fresh DB (001's
  ``metadata.create_all`` already built it from the canonical model); the RLS is (re)asserted.
- ``notifications_kind_check`` widened to admit ``'event_trigger_dropped'`` — the A7-D-8 storm-drop
  P6 bell (over-cap fires are surfaced, never silent). Drop-and-re-add with IF EXISTS, the exact
  shape migration 037 used; the canonical ``persona_api.db.models.notifications`` CheckConstraint is
  widened in lockstep. The downgrade narrows back and (correctly) fails if such rows exist.

Revision ID: 042_event_triggers
Revises: 041_mcp_runtime_instances

PLACEHOLDER NUMBERING (renumber at merge-back, R-19-1): authored off this worktree's head
``038_initiative``. N6/A6 also hold placeholders in flight — the orchestrator linearises off main's
REAL head in landing order and re-points ``down_revision``; do NOT treat 039 as final.
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import event_triggers

revision = "042_event_triggers"
down_revision = "041_mcp_runtime_instances"
branch_labels = None
depends_on = None

# Direct owner-scoped RLS predicate (mirrors 038 / schedules / day_spend). The missing-ok
# ``current_setting(..., true)`` form fails CLOSED: an unset GUC yields NULL, which matches no row.
_CUR = "current_setting('app.current_user_id', true)"
_RLS_PREDICATE = f"owner_id = {_CUR}"

# notifications.kind widening (A7-D-8 storm-drop bell) — mirrors migration 037's shape.
_KINDS_OLD = (
    # main's 039_schedule_notify_on_fire added 'schedule_fired' before this migration.
    "('run_terminal', 'persona_ready', 'schedule_executor_missing', 'schedule_fired')"
)
_KINDS_NEW = (
    "('run_terminal', 'persona_ready', 'schedule_executor_missing', 'schedule_fired', "
    "'event_trigger_dropped')"
)


def _set_notifications_kinds(kinds: str) -> None:
    op.execute("ALTER TABLE notifications DROP CONSTRAINT IF EXISTS notifications_kind_check")
    op.execute(
        f"ALTER TABLE notifications ADD CONSTRAINT notifications_kind_check CHECK (kind IN {kinds})"
    )


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table}")
    op.execute(
        f"CREATE POLICY user_isolation ON {table} "
        f"USING ({_RLS_PREDICATE}) WITH CHECK ({_RLS_PREDICATE})"
    )


def upgrade() -> None:
    bind = op.get_bind()
    event_triggers.create(bind, checkfirst=True)
    _enable_rls("event_triggers")
    _set_notifications_kinds(_KINDS_NEW)


def downgrade() -> None:
    _set_notifications_kinds(_KINDS_OLD)
    op.execute("DROP POLICY IF EXISTS user_isolation ON event_triggers")
    bind = op.get_bind()
    event_triggers.drop(bind, checkfirst=True)
