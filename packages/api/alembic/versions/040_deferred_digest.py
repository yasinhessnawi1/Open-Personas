"""Add the deferred-digest sink (Spec A6, A6-D-10).

Captures the over-cap PROGRESS chatter that ``CadenceGate`` routes to ``DIGEST`` (which, pre-A6,
DROPPED silently — nothing implemented the ``DigestSink`` seam). A SECONDARY input to the
morning-review builder: the review's main sections compose from approvals/stuck/completions/
held-initiatives/upcoming regardless, so a missing/empty sink never blanks the review.

The morning build marks-delivered ATOMICALLY (``UPDATE ... SET delivered_at=now() WHERE
delivered_at IS NULL RETURNING``) and renders from the RETURNING set — no read-then-mark crash
window, no double-deliver, no silent drop.

RLS-scoped by ``owner_id`` (ENABLE + FORCE + ``user_isolation``, created ENTIRELY here per the
post-001 split-home template). Idempotent split-home: ``Table.create(checkfirst=True)`` no-ops
on a fresh DB (001's ``metadata.create_all`` already built it from the model).

Revision ID: 040_deferred_digest
Revises: 039_owner_autonomy_pause

PLACEHOLDER NUMBERING (renumber at merge-back, R-19-1): authored off ``039_owner_autonomy_pause``
in this worktree. The orchestrator linearizes off main's REAL head in landing order (with
A7/N6/A11) and re-points ``down_revision``.
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import deferred_digest

revision = "040_deferred_digest"
down_revision = "039_owner_autonomy_pause"
branch_labels = None
depends_on = None

_CUR = "current_setting('app.current_user_id', true)"
_RLS_PREDICATE = f"owner_id = {_CUR}"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table}")
    op.execute(
        f"CREATE POLICY user_isolation ON {table} "
        f"USING ({_RLS_PREDICATE}) WITH CHECK ({_RLS_PREDICATE})"
    )


def upgrade() -> None:
    deferred_digest.create(op.get_bind(), checkfirst=True)
    _enable_rls("deferred_digest")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS user_isolation ON deferred_digest")
    deferred_digest.drop(op.get_bind(), checkfirst=True)
