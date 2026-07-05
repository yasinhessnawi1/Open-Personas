"""Add the initiative restraint stores + the persona dial (Spec A5, A5-D-X-migration).

Three additive pieces:

- ``initiative_declines`` — the durable dismissal record (A5-D-4): a LIVE decline
  (``revived_at IS NULL``) suppresses its opportunity for ALL of the user's
  personas until an EXPLICIT user revival — never a timer (the Clippy/Alexa
  snooze-that-expires lesson, research §5 rule 1).
- ``initiative_notices`` — the user-level opportunity ledger (A5-D-3/D-6 +
  Phase-1 ruling 4): duplicate-suppression arbitration (the partial unique makes
  scan-order races harmless), cadence-cap counting (at delivery), the batch-hold
  buffer (flush-on-next-scan), and the disposition audit A6 later renders.
- ``personas.initiative_dial`` (+ ``initiative_dial_updated_at``) — the A5-D-5
  dial, server-default backfilled to the RATIFIED ``'propose_only'``.

Both new tables are RLS-scoped by ``owner_id`` with ENABLE + FORCE + a
``user_isolation`` policy created ENTIRELY in this migration (the
009/011/012/015/021/032 split-home template; deliberately NOT in
``db/rls._POLICIES`` since they post-date ``001_initial``). Idempotent +
split-home: ``Table.create(checkfirst=True)`` no-ops on a fresh DB (001's
``metadata.create_all`` already built them from the canonical models);
``ADD COLUMN IF NOT EXISTS`` per the 020/023/025/029/034 precedent.

Revision ID: 038_initiative
Revises: 037_notifications_schedule_kind

PLACEHOLDER NUMBERING (renumber at merge-back, R-19-1): authored off this
worktree's head ``037_notifications_schedule_kind``. K5 claimed 035 and K8 contends 036
in flight — the orchestrator linearizes off main's REAL head in landing order
(expect ~037-era) and re-points ``down_revision``; do NOT treat 036 as final.
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import initiative_declines, initiative_notices

revision = "038_initiative"
down_revision = "037_notifications_schedule_kind"
branch_labels = None
depends_on = None

# Direct owner-scoped RLS predicate (mirrors day_spend / schedules). The missing-ok
# ``current_setting(..., true)`` form fails CLOSED: an unset GUC yields NULL, which
# matches no row.
_CUR = "current_setting('app.current_user_id', true)"
_RLS_PREDICATE = f"owner_id = {_CUR}"

_TABLES = ("initiative_declines", "initiative_notices")


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
    initiative_declines.create(bind, checkfirst=True)
    initiative_notices.create(bind, checkfirst=True)
    for table in _TABLES:
        _enable_rls(table)

    # The dial column-pair (A5-D-5): NOT NULL + server default backfills every
    # existing persona to the ratified propose-only; updated_at NULL = never changed.
    op.execute(
        "ALTER TABLE personas ADD COLUMN IF NOT EXISTS "
        "initiative_dial TEXT NOT NULL DEFAULT 'propose_only'"
    )
    op.execute(
        "ALTER TABLE personas ADD COLUMN IF NOT EXISTS initiative_dial_updated_at TIMESTAMPTZ"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE personas DROP COLUMN IF EXISTS initiative_dial_updated_at")
    op.execute("ALTER TABLE personas DROP COLUMN IF EXISTS initiative_dial")
    for table in _TABLES:
        op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table}")
    bind = op.get_bind()
    initiative_notices.drop(bind, checkfirst=True)
    initiative_declines.drop(bind, checkfirst=True)
