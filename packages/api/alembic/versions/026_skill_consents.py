"""Spec S3 (S3-D-1): ``skill_consents`` — per-persona speciality consent + RLS.

The first migration since ``025``. Adds the append-only consent-event table for
the user-facing "Specialities" surface (S1-D-6: S3 implements the real consent
store the S1 default-deny stub stood in for). Follows the 009/011 pattern: the
table lives in the canonical ``MetaData`` (created here with ``checkfirst=True``)
and its RLS policy lives ENTIRELY in this migration (NOT in ``db.rls._POLICIES``),
so ``001``'s downgrade never ALTERs a later table.

- ``skill_consents`` — owner-scoped through the persona FK-chain
  (``persona_id IN (SELECT id FROM personas WHERE owner_id = current_user)``),
  ``ENABLE`` + ``FORCE`` RLS, fail-closed (unset ``app.current_user_id`` →
  ``owner_id = NULL`` → no rows). ``USING`` + ``WITH CHECK`` (read and write).

Idempotent: table created with ``checkfirst=True``; the policy ``DROP ... IF
EXISTS`` before ``CREATE``. Manual (``alembic upgrade head``), never auto-on-boot.

Revision ID: 026_skill_consents
Revises: 025_avatar_source_provenance
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import skill_consents

# NOTE (S3-D-1): ``down_revision`` = main's ACTUAL head when this branched (verified
# 025). A4/K5 are also migration-bearing and may land 026/027 first — RECOMPUTE this
# in finish order at merge-back (the S2-D-11 tripwire linearization protocol).
revision = "026_skill_consents"
down_revision = "025_avatar_source_provenance"
branch_labels = None
depends_on = None

_CUR = "current_setting('app.current_user_id', true)"
# Owner-scoped via the persona FK-chain (the memory_chunks / persona_mcp_assignments form).
_PRED = f"persona_id IN (SELECT id FROM personas WHERE owner_id = {_CUR})"


def upgrade() -> None:
    bind = op.get_bind()
    skill_consents.create(bind, checkfirst=True)
    op.execute("ALTER TABLE skill_consents ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE skill_consents FORCE ROW LEVEL SECURITY")
    # DROP IF EXISTS keeps this idempotent (+ avoids clashing with a fresh-install
    # 001 that already created the policy once the table is in the metadata).
    op.execute("DROP POLICY IF EXISTS user_isolation ON skill_consents")
    op.execute(
        f"CREATE POLICY user_isolation ON skill_consents USING ({_PRED}) WITH CHECK ({_PRED})"
    )


def downgrade() -> None:
    bind = op.get_bind()
    op.execute("DROP POLICY IF EXISTS user_isolation ON skill_consents")
    op.execute("ALTER TABLE skill_consents NO FORCE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE skill_consents DISABLE ROW LEVEL SECURITY")
    skill_consents.drop(bind, checkfirst=True)
