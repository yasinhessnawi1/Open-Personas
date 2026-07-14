"""Add ``schedule_tombstones`` — the durable user-intent record (R9-037).

The owner reported that schedules they EDIT or DELETE get silently re-created/
re-scheduled by the persona's own autonomous machinery (confirmed root cause:
``persona_api.initiative.handler.ensure_initiative_schedule`` — the A5 per-persona
scan-schedule ensure — is a bare ``NOT EXISTS`` check with zero history awareness,
called both by the hourly/on-restart ``InitiativeProvisioner`` sweep AND by the
initiative dial chat-verb; either one silently resurrects a schedule the user just
deleted). This migration adds the durable record the fix gates creation against:

- ``schedule_tombstones`` — one row per user delete/edit of a schedule, RLS-scoped
  by ``owner_id``, carrying the schedule's identity (``schedule_id`` — exact-id
  match) AND a normalized content identity (``target_job_type`` + ``title_key`` —
  for seams whose ids are freshly-derived per attempt, so id-only matching would
  never catch a re-creation), plus the ``action`` (``deleted``/``edited``) and a
  JSON ``reason`` essence (old cadence / subject) for audit display. Mirrors the
  ``initiative_declines`` shape (Spec A5, migration 038) — the established house
  pattern for "a durable, owner-scoped record of user intent that a later
  autonomous pass must consult before acting again."

RLS-scoped by ``owner_id`` with ENABLE + FORCE + a ``user_isolation`` policy
created ENTIRELY in this migration (the 009/011/012/015/021/032/038 split-home
template; deliberately NOT in ``db/rls._POLICIES`` since it post-dates
``001_initial``). Idempotent + split-home: ``Table.create(checkfirst=True)``
no-ops on a fresh DB (``001_initial``'s ``metadata.create_all`` already builds it
from the canonical model, which now declares this table).

Revision ID: 048_schedule_tombstones
Revises: 047_messages_superseded
Create Date: 2026-07-14
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import schedule_tombstones

revision = "048_schedule_tombstones"
down_revision = "047_messages_superseded"
branch_labels = None
depends_on = None

# Direct owner-scoped RLS predicate (mirrors day_spend / schedules / initiative_declines).
# The missing-ok ``current_setting(..., true)`` form fails CLOSED: an unset GUC yields
# NULL, which matches no row.
_CUR = "current_setting('app.current_user_id', true)"
_RLS_PREDICATE = f"owner_id = {_CUR}"

_TABLE = "schedule_tombstones"


def upgrade() -> None:
    bind = op.get_bind()
    schedule_tombstones.create(bind, checkfirst=True)
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS user_isolation ON {_TABLE}")
    op.execute(
        f"CREATE POLICY user_isolation ON {_TABLE} "
        f"USING ({_RLS_PREDICATE}) WITH CHECK ({_RLS_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS user_isolation ON {_TABLE}")
    bind = op.get_bind()
    schedule_tombstones.drop(bind, checkfirst=True)
