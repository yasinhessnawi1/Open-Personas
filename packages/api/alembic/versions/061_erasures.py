"""The erasure ledger: what is being erased and how far it got (R9-284).

One open row per subject (a persona or an account) being erased, written before anything is
removed. Only one can be open at a time, and a finished one does not stop a later erasure.
It holds the ids later steps need once their rows are gone, each step's counts, and how it ended:
ids and counts, never content. It has no foreign key to ``users`` or ``personas``, because it has
to outlive the deletion it records.

RLS-scoped by ``owner_id`` with ENABLE + FORCE + a ``user_isolation`` policy created ENTIRELY in
this migration (the 048 split-home template). ``persona_app`` holds blanket DML on every table,
so the policy is what keeps one tenant's rows from another's; the erase job runs on the BYPASSRLS
platform engine. No GRANTs here (D-07-5): ``release.py`` re-applies them on every deploy.
Idempotent split-home: ``Table.create(checkfirst=True)`` no-ops on a fresh DB, where
``001_initial``'s ``metadata.create_all`` already built it from the canonical model.

Revision ID: 061_erasures
Revises: 060_channel_persona_set_null
Create Date: 2026-11-08
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import erasures

revision = "061_erasures"
down_revision = "060_channel_persona_set_null"
branch_labels = None
depends_on = None

# The missing-ok ``current_setting(..., true)`` form fails CLOSED: an unset GUC yields NULL,
# which matches no row.
_CUR = "current_setting('app.current_user_id', true)"
_RLS_PREDICATE = f"owner_id = {_CUR}"
_TABLE = "erasures"


def upgrade() -> None:
    erasures.create(op.get_bind(), checkfirst=True)
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS user_isolation ON {_TABLE}")
    op.execute(
        f"CREATE POLICY user_isolation ON {_TABLE} "
        f"USING ({_RLS_PREDICATE}) WITH CHECK ({_RLS_PREDICATE})"
    )


def downgrade() -> None:
    op.execute(f"DROP POLICY IF EXISTS user_isolation ON {_TABLE}")
    erasures.drop(op.get_bind(), checkfirst=True)
