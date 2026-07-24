"""Two-bucket ledger — allowance marker + ``payg_grants`` lots + ``subscription`` (Spec M4 T1a).

M4 makes credits purchasable + plan-gated. Owner Decision 1 splits the balance into
two buckets over M3's ledger, WITHOUT renaming the physical ``credits.balance``
column (D-M4-rename → Option A — the M3 adversarial credits parity tests encode that
column name; keeping them pristine on money code wins). This migration is additive:

- ``credits.allowance_period TEXT NULL`` — the lazy monthly-reset marker ('YYYY-MM'
  UTC); NULL = never reset by the monthly cycle. Inert until T6 wires the reset
  (the R7 ``day_spend`` idempotent-per-period pattern). ``credits.balance`` is
  UNCHANGED — it now conceptually IS the allowance bucket (documented on the model).
- ``payg_grants`` — the PAYG "lots" table (per-lot 12-month expiry): ``credits_total``
  / ``credits_remaining`` (CHECKs ≥ 0 and ≤ total), ``granted_at`` / ``expires_at``,
  ``source_billing_key`` (UNIQUE — one lot per Stripe event; the secondary
  idempotency guard), a partial spendable/FIFO index ``(user_id, expires_at) WHERE
  credits_remaining > 0``. RLS-scoped by ``user_id``.
- ``subscription`` — one row per user (teams = M5): ``plan_code`` (default 'free'),
  ``status``, Stripe customer/subscription ids + period bounds, ``cancel_at_period_end``;
  a partial-unique on ``stripe_subscription_id`` + a ``stripe_customer_id`` index.
  RLS-scoped by ``user_id``.

Split-home (cf. 020 / 023 / 025 / 034 / 045 / 046 / 048 / 050): all three shapes are
declared on the canonical ``persona_api.db.models`` tables, so on a fresh DB
``001_initial``'s ``metadata.create_all`` already builds them and the guarded
``ADD COLUMN IF NOT EXISTS`` / ``Table.create(checkfirst=True)`` below are harmless
no-ops; on a previously-deployed Postgres they genuinely add. RLS for the two new
tables is created ENTIRELY here (the 048 template; deliberately NOT in
``db/rls._POLICIES`` since they post-date ``001_initial``). No backfill — existing
``credits`` rows read ``allowance_period = NULL`` (correct: the monthly cycle has not
touched them; T6's lazy reset stamps it), and existing balances are the allowance
bucket verbatim (no data migration — the physical column is unchanged).

Revision ID: 051_two_bucket_ledger
Revises: 050_credit_tx_cost_columns
Create Date: 2026-07-20
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import payg_grants, subscription

revision = "051_two_bucket_ledger"
down_revision = "050_credit_tx_cost_columns"
branch_labels = None
depends_on = None

# Direct owner-scoped RLS predicate (mirrors day_spend / schedule_tombstones). The
# missing-ok ``current_setting(..., true)`` form fails CLOSED: an unset GUC yields
# NULL, which matches no row.
_CUR = "current_setting('app.current_user_id', true)"
_RLS_PREDICATE = f"user_id = {_CUR}"

_RLS_TABLES = ("payg_grants", "subscription")


def upgrade() -> None:
    bind = op.get_bind()
    # 1. allowance_period on credits (additive, nullable). balance is UNCHANGED.
    op.execute("ALTER TABLE credits ADD COLUMN IF NOT EXISTS allowance_period TEXT")
    # 2. the two new tables (idempotent: create_all built them on a fresh DB).
    payg_grants.create(bind, checkfirst=True)
    subscription.create(bind, checkfirst=True)
    # 3. RLS ENABLE + FORCE + user_isolation policy on each new table.
    for tbl in _RLS_TABLES:
        op.execute(f"ALTER TABLE {tbl} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {tbl} FORCE ROW LEVEL SECURITY")
        op.execute(f"DROP POLICY IF EXISTS user_isolation ON {tbl}")
        op.execute(
            f"CREATE POLICY user_isolation ON {tbl} "
            f"USING ({_RLS_PREDICATE}) WITH CHECK ({_RLS_PREDICATE})"
        )


def downgrade() -> None:
    bind = op.get_bind()
    for tbl in _RLS_TABLES:
        op.execute(f"DROP POLICY IF EXISTS user_isolation ON {tbl}")
    subscription.drop(bind, checkfirst=True)
    payg_grants.drop(bind, checkfirst=True)
    op.execute("ALTER TABLE credits DROP COLUMN IF EXISTS allowance_period")
