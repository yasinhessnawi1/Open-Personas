"""Pro opt-in auto-top-up flag on ``subscription`` (Spec M4, T7b).

A single additive, nullable-safe boolean: ``subscription.auto_topup_enabled`` — the
per-user opt-in for Pro auto-top-up (off by default; only a Pro user may turn it on,
enforced at the toggle + the trigger via ``plan.auto_topup_eligible``). When a Pro
opted-in user's balance CROSSES below $2 (200 credits) on a background deduct, an
off-session $10 charge fires on the saved card and grants a PAYG lot via the existing
``payment_intent.succeeded`` webhook (idempotent on the PI id). This migration only adds
the flag; the trigger + charge live in ``persona_api.billing.autotopup``.

Split-home (cf. 051): the column is declared on the canonical
``persona_api.db.models.subscription`` table, so on a fresh DB ``001_initial``'s
``metadata.create_all`` already builds it and the guarded ``ADD COLUMN IF NOT EXISTS``
below is a harmless no-op; on a previously-deployed Postgres it genuinely adds. No
backfill — existing rows read ``false`` (the NOT NULL DEFAULT), i.e. opted-out (correct:
auto-top-up is strictly opt-in). RLS is inherited from the table's existing
``user_isolation`` policy (051) — no policy change.

Revision ID: 052_auto_topup_opt_in
Revises: 051_two_bucket_ledger
Create Date: 2026-07-24
"""

from __future__ import annotations

from alembic import op

revision = "052_auto_topup_opt_in"
down_revision = "051_two_bucket_ledger"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE subscription "
        "ADD COLUMN IF NOT EXISTS auto_topup_enabled BOOLEAN NOT NULL DEFAULT false"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE subscription DROP COLUMN IF EXISTS auto_topup_enabled")
