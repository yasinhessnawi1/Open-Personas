"""Per-tenant MCP runtime: ``mcp_runtime_instances`` + RLS (Spec N6, N6-D-7).

One row per (owner, server) image-runtime instance the per-tenant Fly runtime
spawns (N6-D-1). Durable so ``ensure``/reap is idempotent across API restarts and
the not-connected signal (N6-D-6) reads state. **Never a secret** — the injected
credential lives in the Machine env (N6-D-2); the row records where it runs + state.

RLS mirrors the ``009`` split-home pattern (the policy lives here, not in
``db.rls._POLICIES``, so ``001``'s downgrade never ALTERs a later table):
``user_isolation`` USING/WITH CHECK ``owner_id = current_setting('app.current_user_id')``
— a tenant only ever touches its own instance rows (the reaper deliberately uses the
RLS-bypassing engine to scan cross-tenant, N6-D-7a condition 3). ENABLE + FORCE + the
idempotent ``DROP POLICY IF EXISTS`` discipline.

Idempotent: the table is in the canonical ``MetaData`` so ``create(checkfirst=True)``
is a no-op on a fresh ``001`` ``create_all``; the policy is dropped-if-exists before
create. Manual (``alembic upgrade head``), never auto-on-startup.

**PLACEHOLDER NUMBER (N6-D-7a):** the revision id + ``down_revision`` are a placeholder
off this worktree's real head (``037_notifications_schedule_kind``). At merge-back the
orchestrator RENUMBERS onto the then-current head (local global head is already
``038_initiative``; A7 holds a provisional ``039``; N6 lands ``040+`` per merge order).
Do NOT treat ``037`` as the permanent parent.

Revision ID: 040_mcp_runtime_instances
Revises: 037_notifications_schedule_kind
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import mcp_runtime_instances

revision = "040_mcp_runtime_instances"
down_revision = "037_notifications_schedule_kind"
branch_labels = None
depends_on = None

_CUR = "current_setting('app.current_user_id', true)"
_PREDICATE = f"owner_id = {_CUR}"


def upgrade() -> None:
    bind = op.get_bind()
    mcp_runtime_instances.create(bind, checkfirst=True)
    op.execute("ALTER TABLE mcp_runtime_instances ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mcp_runtime_instances FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS user_isolation ON mcp_runtime_instances")
    op.execute(
        f"CREATE POLICY user_isolation ON mcp_runtime_instances "
        f"USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("DROP POLICY IF EXISTS user_isolation ON mcp_runtime_instances")
    op.execute("ALTER TABLE mcp_runtime_instances NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE mcp_runtime_instances DISABLE ROW LEVEL SECURITY")
    mcp_runtime_instances.drop(bind, checkfirst=True)
