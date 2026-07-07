"""Add the per-owner autonomy pause (Spec A6, A6-D-8, Ruling 1).

Presence-based (like ``suspended_personas``): a row means this owner's autonomy is paused;
resume DELETEs it (the who/when history lives in ``audit_log`` via ``autonomy.owner_pause`` /
``autonomy.owner_resume``). Owner-LEVEL (PK ``owner_id``) so it covers personas created WHILE
paused — the completeness the fan-out alternative would leak.

RLS-scoped by ``owner_id`` (ENABLE + FORCE + a ``user_isolation`` policy created ENTIRELY here,
the post-001 split-home template, like 038). Idempotent split-home: ``Table.create(checkfirst=
True)`` no-ops on a fresh DB (001's ``metadata.create_all`` already built it from the model).

The ``is_owner_autonomy_paused`` predicate SELF-SCOPES its read (sets the owner GUC via
``rls_connection``) so a background origination gate outside the owner's RLS context cannot
false-negative into leaking origination — the pause contract's teeth (A6-D-8).

Revision ID: 039_owner_autonomy_pause
Revises: 038_initiative

PLACEHOLDER NUMBERING (renumber at merge-back, R-19-1): authored off this worktree's head
``038_initiative``. A7/N6/A11 contend the next slots in flight — the orchestrator linearizes
off main's REAL head in landing order and re-points ``down_revision``.
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import owner_autonomy_pause

revision = "039_owner_autonomy_pause"
down_revision = "038_initiative"
branch_labels = None
depends_on = None

# Direct owner-scoped RLS predicate (mirrors 038 / day_spend / schedules). The missing-ok
# ``current_setting(..., true)`` form fails CLOSED: an unset GUC yields NULL, which matches no
# row — which is exactly why the predicate impl self-scopes its read (never trusts the ambient
# GUC), so a genuinely-paused owner is never mis-read as un-paused by a background gate.
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
    owner_autonomy_pause.create(op.get_bind(), checkfirst=True)
    _enable_rls("owner_autonomy_pause")


def downgrade() -> None:
    op.execute("DROP POLICY IF EXISTS user_isolation ON owner_autonomy_pause")
    owner_autonomy_pause.drop(op.get_bind(), checkfirst=True)
