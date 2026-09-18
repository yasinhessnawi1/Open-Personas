"""Add ``free_model_daily_usage``, the free-model daily request meter (R9-179 item 3).

OpenRouter caps free-model calls at N requests per UTC DAY ACCOUNT-WIDE (1,000 since
credits were bought; 50 before), and that single ceiling is shared by every free-plan
user's turn and every background job that runs on a free chain. Nothing counted them,
so the first sign of exhaustion would have been free users' turns failing in the
afternoon. This table is the count: one row per UTC day, incremented at the api's
existing served-model attribution points.

Persisted rather than held in process because the alarm must survive a deploy, an
in-memory counter silently resets the day to zero on every restart, which is exactly
the afternoon it would have warned about.

NOT RLS-scoped, deliberately: the row describes OUR OpenRouter account, not a tenant,
and carries no user column at all. The exemption is recorded in
``persona_api.db.rls.RLS_EXEMPT_TABLES`` with that reason.

Split-home (cf. 048): the table is declared on the canonical ``persona_api.db.models``
metadata, so ``001_initial``'s ``metadata.create_all`` already builds it on a fresh DB
and ``create(checkfirst=True)`` below no-ops there; on a previously-deployed Postgres
it genuinely creates.

Revision ID: 055_free_model_daily_usage
Revises: 054_runs_task_id
Create Date: 2026-09-18
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import free_model_daily_usage

revision = "055_free_model_daily_usage"
down_revision = "054_runs_task_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    free_model_daily_usage.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    free_model_daily_usage.drop(op.get_bind(), checkfirst=True)
