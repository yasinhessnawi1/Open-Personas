"""Add ``day_spend`` + ``inflight_ops`` — the denial-of-wallet caps (Spec R7, R7-D-1/2/3/4/7).

Two cross-session Postgres stores, both **fail-loud** money guards over the
credits seam:

- ``day_spend`` — the per-user per-UTC-day spend counter (R7-D-2). One integer
  row per ``(user_id, utc_day)``; the conditional atomic write in
  ``persona.credits.service.book_day_spend`` books ``WHERE spent + :cost <= :cap``
  (the R2 TOCTOU-safe family) so two concurrent near-cap requests can't both pass.
- ``inflight_ops`` — the durable in-flight registry for long-running ops (chat
  SSE, agentic jobs, R7-D-4). Count-filtered admission under a per-(user,op_class)
  advisory lock caps simultaneous long ops at N; the ``pg_try_advisory_xact_lock``
  bounded-op primitive can't span these out-of-transaction runs, so the durable
  count is the correct duration-spanning mechanism.

Both are RLS-scoped by ``user_id`` (``user_id = current_setting('app.current_user_id')``),
with ENABLE + FORCE + a ``user_isolation`` policy created ENTIRELY in this
migration (the 009/011/012/015/021 split-home template; deliberately NOT in
``db/rls._POLICIES`` since these tables post-date ``001_initial``).

Idempotent + split-home (cf. 021): ``Table.create(checkfirst=True)`` — a fresh
DB already built both from the canonical ``persona_api.db.models`` via
``001_initial``'s ``metadata.create_all``, so this is a no-op there; on a
previously-deployed DB it actually creates them. Both agree. Guarded
``DROP POLICY IF EXISTS`` before ``CREATE POLICY`` for policy idempotency.

Revision ID: 026_dow_caps
Revises: 025_avatar_source_provenance
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import day_spend, inflight_ops

# Renumbered at merge-back (R-19-1 chain): authored as 026 off the then-head
# ``025_avatar_source_provenance``; R7 landed first of the 032-era contenders
# (K7/A8 renumber after it), so it claims 032 off ``031_request_telemetry``.
revision = "032_dow_caps"
down_revision = "031_request_telemetry"
branch_labels = None
depends_on = None

# Direct user-scoped RLS predicate (mirrors credits / credit_transactions). The
# missing-ok ``current_setting(..., true)`` form fails CLOSED: an unset GUC yields
# NULL, which matches no row.
_CUR = "current_setting('app.current_user_id', true)"
_RLS_PREDICATE = f"user_id = {_CUR}"


def _enable_rls(table: str) -> None:
    op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table}")
    op.execute(
        f"CREATE POLICY user_isolation ON {table} "
        f"USING ({_RLS_PREDICATE}) WITH CHECK ({_RLS_PREDICATE})"
    )


def _disable_rls(table: str) -> None:
    op.execute(f"DROP POLICY IF EXISTS user_isolation ON {table}")
    op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")


def upgrade() -> None:
    bind = op.get_bind()
    day_spend.create(bind, checkfirst=True)
    inflight_ops.create(bind, checkfirst=True)
    _enable_rls("day_spend")
    _enable_rls("inflight_ops")
    # R7-D-1 discharge of D-23-X: the soft per-day cost-bias ramp sums today's
    # ``turn_logs.cost_cents`` per conversation over the UTC day. This composite
    # index makes that a sargable range scan. ``turn_logs`` predates this migration
    # (001_initial), so an ``IF NOT EXISTS`` add (declared on the canonical model too,
    # so a fresh ``create_all`` already built it — split-home, idempotent both ways).
    op.execute(
        "CREATE INDEX IF NOT EXISTS idx_turn_logs_conversation_created "
        "ON turn_logs (conversation_id, created_at)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS idx_turn_logs_conversation_created")
    _disable_rls("inflight_ops")
    _disable_rls("day_spend")
    op.execute("DROP TABLE IF EXISTS inflight_ops")
    op.execute("DROP TABLE IF EXISTS day_spend")
