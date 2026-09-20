"""Widen the ``calls.end_reason`` vocabulary to the reasons the runner really has (R9-203).

``AgentSession`` initialised the reason to ``'disconnect'`` and the only other
assignment anywhere set ``'error'``, so every call that did not crash was stored
as a clean disconnect: the ones killed by a redeploy, the ones the credit cutoff
ended, and the genuine hangups all read the same. A field with one producer and
one value is the dark-field shape, and it is the field that would have made the
wall clock billing bug visible months earlier.

The runner now sets ``'user_hangup'`` (the caller left the room, seen on the
LiveKit ``participant_disconnected`` event), ``'exhausted'`` (the credit cutoff
ended the call) and ``'shutdown'`` (the worker was drained or redeployed, so the
session task was cancelled). Only the last two are new to the vocabulary;
``'user_hangup'`` was already allowed and simply had no producer.

The CHECK is REPLACED rather than altered: Postgres has no "widen a check" verb,
and a guarded DROP + ADD is idempotent on a fresh database (where
``001_initial`` already builds the constraint from the canonical models with the
new text) and on a deployed one alike. No row is invalidated: the vocabulary
only grows, so every stored value still passes.

Revision ID: 056_call_end_reason_vocabulary
Revises: 055_free_model_daily_usage
Create Date: 2026-09-20
"""

from __future__ import annotations

from alembic import op

revision = "056_call_end_reason_vocabulary"
down_revision = "055_free_model_daily_usage"
branch_labels = None
depends_on = None

_NEW = (
    "end_reason IS NULL OR end_reason IN "
    "('user_hangup', 'switched', 'exhausted', 'shutdown', 'error', 'disconnect')"
)
_OLD = "end_reason IS NULL OR end_reason IN ('user_hangup', 'switched', 'error', 'disconnect')"


def upgrade() -> None:
    op.execute("ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_end_reason_check")
    op.execute(f"ALTER TABLE calls ADD CONSTRAINT calls_end_reason_check CHECK ({_NEW})")


def downgrade() -> None:
    # Rows carrying one of the widened reasons would fail the narrow CHECK, so
    # they are folded back to the value the narrow vocabulary would have stored
    # for them anyway: a plain disconnect.
    op.execute(
        "UPDATE calls SET end_reason = 'disconnect' WHERE end_reason IN ('exhausted', 'shutdown')"
    )
    op.execute("ALTER TABLE calls DROP CONSTRAINT IF EXISTS calls_end_reason_check")
    op.execute(f"ALTER TABLE calls ADD CONSTRAINT calls_end_reason_check CHECK ({_OLD})")
