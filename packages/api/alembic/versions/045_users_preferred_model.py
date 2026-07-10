"""Add ``preferred_model TEXT NULL`` to ``users`` — the sticky last-choice default (Spec M1, M1-T6).

M1 lets a caller pick a specific chat model instead of the tier-resolved default. T3 taught the
runtime loop to prefer a PERSONA's own ``routing.preferred_model`` (author-set, in the YAML); T5
gave the web a catalog to pick from (``GET /v1/models``). This migration adds the USER-level
counterpart: the caller's own last-picked model id, persisted so a return visit starts from where
they left off instead of resetting to the tier default every session.

``users.preferred_model TEXT NULL``:

- Nullable, no backfill — ``NULL`` = "no sticky choice yet, use the tier-resolved default" (the
  pre-M1-T6 behaviour). Every existing user reads ``NULL`` after this migration.
- **TEXT, not FK/ENUM.** The value is an opaque OpenRouter model id string (e.g.
  ``"z-ai/glm-4.6"``); its validity against the live catalog is the WEB picker's concern
  (populated from T5's ``GET /v1/models``) — the API only stores the preference, it never calls
  the catalog here. Same posture as the ``timezone``/name columns: free-form TEXT, app-layer
  boundary validation (blank/whitespace-only rejected as a 422 by the request schema, not a DB
  CHECK).
- **No RLS change.** ``users`` is not RLS-scoped (globally readable by id; tenant RLS lives on
  the child tables) — a new nullable column inherits nothing to change.

Split-home (cf. migrations 020 / 023 / 025 / 029 / 034): the column is declared on the canonical
``persona_api.db.models.users`` table, so on a fresh DB — including the community SQLite edition
— ``001_initial``'s ``metadata.create_all`` already builds it and the guarded
``ADD COLUMN IF NOT EXISTS`` below is a harmless no-op; on a previously-deployed Postgres it
actually adds the column. Both agree.

Revision ID: 045_users_preferred_model
Revises: 044_deferred_digest
Create Date: 2026-07-10
"""

from __future__ import annotations

from alembic import op

revision = "045_users_preferred_model"
down_revision = "044_deferred_digest"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``ADD COLUMN IF NOT EXISTS`` per the 020 / 023 / 025 / 029 / 034 precedent:
    # ``001_initial`` builds the schema via ``MetaData.create_all`` from the *live*
    # models (which now declare ``preferred_model``), so on a fresh DB the column
    # already exists when this runs (harmless no-op); on a DB that ran 001 before
    # M1-T6 it is genuinely added. NULL default = "no sticky choice yet" — no data
    # migration.
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS preferred_model TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS preferred_model")
