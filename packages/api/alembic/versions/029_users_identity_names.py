"""Add optional ``first_name`` / ``last_name`` to ``users`` (Spec K6, K6-D-1/K6-D-7).

K6 makes the user a real, named, first-class entity: the name is captured through
our OWN app step (``PATCH /v1/me/profile``) into our DB — Clerk stays auth-only.
Two nullable columns:

- ``first_name TEXT NULL`` / ``last_name TEXT NULL`` — optional, skippable. A
  nameless / pre-K6 account stays fully valid everywhere (null-safe); the name is
  never a hard requirement. TEXT (not CHECK/ENUM) so the community **SQLite**
  edition is byte-identical to Postgres — length cap + control-char strip live at
  the app layer (K6-D-8).

**Backfill = NULL (no data migration).** Every user that predates these columns
reads ``NULL`` (the ``ADD COLUMN`` default) — the honest "no name set yet" state.
An "add your name" prompt may appear later but is never forced (K6 AC-2).

**No RLS change.** ``users`` is deliberately NOT RLS-scoped (globally readable by
``id``; the tenant RLS lives on the child tables — personas/conversations/…). A
new nullable column on ``users`` inherits nothing to change.

Split-home (cf. migrations 020 / 023 / 025): the same columns are declared on the
canonical ``persona_api.db.models.users`` table, so on a fresh DB — including the
community SQLite edition — ``001_initial``'s ``metadata.create_all`` already builds
them and the guarded ``ADD COLUMN IF NOT EXISTS`` below is a harmless no-op; on a
previously-deployed Postgres it actually adds the columns. Both agree.

Revision ID: 029_users_identity_names
Revises: 028_notifications_feed
Create Date: 2026-07-02

PLACEHOLDER down_revision (R-19-1 chain numbering): this worktree branched off main
@ ``e1f1a58`` (pre-S3), so its local head is ``025_avatar_source_provenance``.
Main's REAL head at merge-back is ``027_mcp_oauth`` (S3 ``026_skill_consents`` + R8
``027_mcp_oauth`` landed after the branch); R5/R7/P6 also target the 028-era number.
The revision number + ``down_revision`` are therefore RECOMPUTED at merge-back to
preserve the single-head chain (first-lands-claims-028; orchestrator linearizes) —
do NOT rely on ``028`` / ``025`` surviving verbatim.
"""

from __future__ import annotations

from alembic import op

revision = "029_users_identity_names"
down_revision = "028_notifications_feed"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ``ADD COLUMN IF NOT EXISTS`` per the 020 / 023 / 025 precedent: ``001_initial``
    # builds the schema via ``MetaData.create_all`` from the *live* models (which now
    # declare first_name / last_name), so on a fresh DB the columns already exist
    # when 028 runs (harmless no-op); on a DB that ran 001 before K6 they are
    # genuinely added. NULL default = the "no name set yet" backfill — no data migration.
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name TEXT")
    op.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name TEXT")


def downgrade() -> None:
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS last_name")
    op.execute("ALTER TABLE users DROP COLUMN IF EXISTS first_name")
