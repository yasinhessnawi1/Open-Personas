"""Add real-cost + idempotency columns to ``credit_transactions`` (Spec M3, T1a).

M3 makes every paid surface recover its real provider cost through one billing
seam, deducted via ``persona.credits`` and recorded on the ledger. The ledger
row needs to be self-describing for M4 tier-sizing / margin analytics and safe
against at-least-once double-charging:

- ``cost_cents DOUBLE PRECISION NULL`` — the true provider cost pre-markup. With
  ``PERSONA_CREDIT_MARKUP`` the credit ``delta`` no longer equals the provider
  cost, so the raw cost must live in its own column (mirrors
  ``turn_logs.cost_cents``, also Float — an INTEGER would round sub-cent surfaces
  like chat/embeds/tool-calls to 0 and destroy the analytics, D-M3-12 / Ruling A).
- ``cost_basis TEXT NULL`` — the provenance vocabulary (``actual_openrouter`` |
  ``estimate_static`` | ``estimate_catalog`` | ``provider_meter`` | ``infra_flat``
  | ``unpriced``). TEXT, not ENUM — app-owned + already evolving (same posture as
  ``turn_logs.cost_basis`` at 046 and ``users.preferred_model`` at 045).
- ``billing_key TEXT NULL`` + a **partial-unique** index — the idempotency anchor
  for background/task deducts (T4b/T5). ``ON CONFLICT (billing_key) DO NOTHING``
  makes a re-delivered op a clean no-op; the index is partial
  (``WHERE billing_key IS NOT NULL``) so the vast majority of rows (chat, direct
  deducts) leave it NULL and are unaffected (D-M3-R5).

Split-home (cf. 020 / 023 / 025 / 029 / 034 / 045 / 046): the three columns and
the partial-unique index are declared on the canonical
``persona_api.db.models.credit_transactions`` table, so on a fresh DB
``001_initial``'s ``metadata.create_all`` already builds them and the guarded
``ADD COLUMN IF NOT EXISTS`` / ``CREATE UNIQUE INDEX IF NOT EXISTS`` below are
harmless no-ops; on a previously-deployed Postgres they genuinely add. No RLS
change (``credit_transactions`` is already RLS-scoped via ``user_id``). No
backfill — every existing row reads ``NULL`` for all three (a legacy pre-M3 row).

Revision ID: 050_credit_tx_cost_columns
Revises: 049_schedule_task_paused_kind
Create Date: 2026-07-14
"""

from __future__ import annotations

from alembic import op

revision = "050_credit_tx_cost_columns"
down_revision = "049_schedule_task_paused_kind"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE credit_transactions ADD COLUMN IF NOT EXISTS cost_cents DOUBLE PRECISION"
    )
    op.execute("ALTER TABLE credit_transactions ADD COLUMN IF NOT EXISTS cost_basis TEXT")
    op.execute("ALTER TABLE credit_transactions ADD COLUMN IF NOT EXISTS billing_key TEXT")
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_tx_billing_key "
        "ON credit_transactions (billing_key) WHERE billing_key IS NOT NULL"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_credit_tx_billing_key")
    op.execute("ALTER TABLE credit_transactions DROP COLUMN IF EXISTS billing_key")
    op.execute("ALTER TABLE credit_transactions DROP COLUMN IF EXISTS cost_basis")
    op.execute("ALTER TABLE credit_transactions DROP COLUMN IF EXISTS cost_cents")
