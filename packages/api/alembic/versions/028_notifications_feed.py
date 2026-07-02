"""Add the ``notifications`` durable cross-device feed table (Spec P6, P6-D-11).

The server-authored home for bell notifications (run-terminal, persona-ready) so
they survive reload + sync across devices — the promotion of Spec 35's
``localStorage``-capped-30 client feed. Copy is stored locale-neutral
(``message_key`` + ``params`` JSONB; the web localises at render, P6-D-5).

RLS: owner-scoped, like ``calls`` / ``conversations``. ENABLE + FORCE + a
``user_isolation`` policy ``USING/WITH CHECK (owner_id = current_setting(
'app.current_user_id'))``, created ENTIRELY in this migration (the 009/011/012/
015/021 split-home template; deliberately NOT in ``db/rls._POLICIES``). FORCE so
the ``persona_app`` non-superuser role is itself subject to the policy.

Idempotency (P6-D-11): ``UNIQUE (owner_id, kind, ref_id)`` — a server-authored
write retried across the run persist-final / persist-error / restart-sweep paths
is a no-op via ``ON CONFLICT DO NOTHING`` (no duplicate bell entry).

Idempotent DDL: ``Table.create(checkfirst=True)`` (``001_initial`` builds it on a
fresh DB from the canonical models, so this is a no-op there) + guarded
``DROP POLICY IF EXISTS`` before ``CREATE POLICY``.

Revision ID: 028_notifications_feed
Revises: 027_mcp_oauth

NOTE (P6-D-13): the ``026`` number is a PLACEHOLDER local to this worktree
(head here is ``025``). At merge-back R-19-1 linearizes it off main's REAL head
(``027_mcp_oauth`` and the K6/R5/R7 ``028``-era train), so this becomes ``028+``
with ``down_revision`` recomputed. Do NOT hardcode this number downstream.
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import notifications

revision = "028_notifications_feed"
down_revision = "027_mcp_oauth"
branch_labels = None
depends_on = None

# owner-scoped RLS predicate (mirrors calls / conversations).
_CUR = "current_setting('app.current_user_id', true)"
_RLS_PREDICATE = f"owner_id = {_CUR}"


def upgrade() -> None:
    bind = op.get_bind()
    notifications.create(bind, checkfirst=True)
    op.execute("ALTER TABLE notifications ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notifications FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS user_isolation ON notifications")
    op.execute(
        f"CREATE POLICY user_isolation ON notifications USING ({_RLS_PREDICATE}) "
        f"WITH CHECK ({_RLS_PREDICATE})"
    )


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS user_isolation ON notifications")
    op.execute("ALTER TABLE notifications NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE notifications DISABLE ROW LEVEL SECURITY")
    op.execute("DROP TABLE IF EXISTS notifications")
