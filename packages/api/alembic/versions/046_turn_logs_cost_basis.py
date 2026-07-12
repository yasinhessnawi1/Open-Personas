"""Add ``cost_basis TEXT NULL`` to ``turn_logs`` — pricing provenance (Spec M2, M2-T4).

M2 unified turn pricing on the Spec-22/23 resolver chain (D-M2-1) and captures
OpenRouter response-side actuals (D-M2-3). The runtime :class:`TurnLog` has
carried a ``cost_basis`` field since Spec 25; M2 repurposed its vocabulary
(``actual_openrouter`` | ``estimate_static`` | ``estimate_catalog`` |
``unpriced``) and this migration gives it a Postgres column so the persisted
row — and ``GET /v1/me/usage`` — can say whether a turn's ``cost_cents`` is
what we actually paid or an estimate (D-M2-4).

``turn_logs.cost_basis TEXT NULL``:

- Nullable, no backfill — ``NULL`` = "legacy pre-M2 row", rendered as an
  estimate by consumers (the honest reading: pre-M2 numbers were estimates at
  best). Every existing row reads ``NULL`` after this migration.
- **TEXT, not ENUM.** The vocabulary is app-owned and has already changed once
  (Spec 25's two values → M2's four); a CHECK/ENUM would turn the next
  vocabulary evolution into a migration. Same free-form-TEXT posture as
  ``users.preferred_model`` (045).
- **No RLS change.** ``turn_logs`` is RLS-scoped via ``conversations``; a new
  nullable column inherits that scoping.

Split-home (cf. migrations 020 / 023 / 025 / 029 / 034 / 045): the column is
declared on the canonical ``persona_api.db.models.turn_logs`` table, so on a
fresh DB ``001_initial``'s ``metadata.create_all`` already builds it and the
guarded ``ADD COLUMN IF NOT EXISTS`` below is a harmless no-op; on a
previously-deployed Postgres it actually adds the column. Both agree.

Revision ID: 046_turn_logs_cost_basis
Revises: 045_users_preferred_model
Create Date: 2026-07-12
"""

from __future__ import annotations

from alembic import op

revision = "046_turn_logs_cost_basis"
down_revision = "045_users_preferred_model"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``ADD COLUMN IF NOT EXISTS`` per the 020 / 023 / 025 / 029 / 034 / 045
    # precedent: ``001_initial`` builds the schema via ``MetaData.create_all``
    # from the *live* models (which now declare ``cost_basis``), so on a fresh
    # DB the column already exists when this runs (harmless no-op); on a DB
    # that ran 001 before M2-T4 it is genuinely added. NULL default = "legacy
    # pre-M2 row" — no data migration.
    op.execute("ALTER TABLE turn_logs ADD COLUMN IF NOT EXISTS cost_basis TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE turn_logs DROP COLUMN IF EXISTS cost_basis")
