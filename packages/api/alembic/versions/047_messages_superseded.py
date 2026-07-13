"""Add ``superseded_at TIMESTAMPTZ NULL`` to ``messages`` — regenerate/edit marker (R9-025 leg C).

The operator pass on the shipped "retry" (R9-025 wave 2a) found it was a client-side
echo-resend: the SAME preceding user message got re-sent as a brand-new turn, so the
model saw its own just-superseded reply still sitting in context and produced a
confused answer. This migration adds the supersede primitive a REAL regenerate (and a
new edit-the-last-user-message-and-rerun action) needs:

``messages.superseded_at TIMESTAMPTZ NULL``:

- Nullable, no backfill — ``NULL`` = "in force" (every historical row, and every row
  written by every OTHER existing path, reads ``NULL`` after this migration — the
  ``tier_used`` / ``originated`` / ``streaming_status`` nullable-additive precedent).
  A non-NULL timestamp means a LATER row replaced this one (a regenerated assistant
  reply, or an edited user message) and it must never re-enter (a) a future model
  prompt or (b) the web message listing.
- **The row is never deleted or content-mutated** — only flagged. This is the
  additive invariant the feature is built on: existing rows stay untouched, a
  superseded row simply stops being read by the two consumers that must honour it
  (``chat_service._load_conversation`` for context, ``chat_service.get_conversation``
  for the listing), and a future tree/branch-history feature can still read it.
- **TIMESTAMPTZ, not a boolean.** Matches the ``consent_updated_at`` /
  ``initiative_dial_updated_at`` precedent elsewhere in this table set — a NULL/non-
  NULL flag AND a "since when" audit trail from the one column, no extra migration if
  a future feature wants the timestamp.
- **No RLS change.** ``messages`` RLS is scoped via ``conversation_id`` (through
  ``conversations.owner_id``, per ``db/rls.py``) — a new nullable column inherits that
  scoping; the supersede UPDATE (``MessagesTurnSink.supersede_message``) runs through
  the SAME RLS-scoped engine as every other messages write.

Split-home (cf. migrations 020 / 023 / 025 / 029 / 034 / 045 / 046): the column is
declared on the canonical ``persona_api.db.models.messages`` table, so on a fresh DB —
including the community SQLite edition — ``001_initial``'s ``metadata.create_all``
already builds it and the guarded ``ADD COLUMN IF NOT EXISTS`` below is a harmless
no-op; on a previously-deployed Postgres it actually adds the column. Both agree.

Revision ID: 047_messages_superseded
Revises: 046_turn_logs_cost_basis
Create Date: 2026-07-13
"""

from __future__ import annotations

from alembic import op

revision = "047_messages_superseded"
down_revision = "046_turn_logs_cost_basis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``ADD COLUMN IF NOT EXISTS`` per the 020 / 023 / 025 / 029 / 034 / 045 / 046
    # precedent: ``001_initial`` builds the schema via ``MetaData.create_all`` from
    # the *live* models (which now declare ``superseded_at``), so on a fresh DB the
    # column already exists when this runs (harmless no-op); on a DB that ran 001
    # before R9-025 leg C it is genuinely added. NULL default = "in force" — no data
    # migration (every existing row is, correctly, still in force).
    op.execute("ALTER TABLE messages ADD COLUMN IF NOT EXISTS superseded_at TIMESTAMPTZ")


def downgrade() -> None:
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS superseded_at")
