"""Account erasure: the per-request fence's index and the retained billing table (R9-284 T9).

``idx_erasures_subject`` serves the account fence folded into the per-request user upsert: an
id with any account erasure, finished or not, is refused (D-R9-284-account-erase). The existing
``uq_erasures_open_subject`` is partial (open rows only), so it cannot answer for a finished one.

``retained_billing_records`` keeps an erased account's paid billing rows for the retention
period, keyed by the erasure id. It has no foreign key, since it outlives the ``users`` row, and
RLS ENABLE + FORCE with no policy: ``persona_app`` (blanket DML, D-07-5 grants re-applied by
``release.py``) sees and writes nothing, and only the BYPASSRLS platform engine reaches it.
Idempotent split-home: ``create(checkfirst=True)`` no-ops on a fresh DB, where ``001_initial``'s
``metadata.create_all`` already built both from the canonical model.

Revision ID: 064_account_erasure
Revises: 063_graph_erasure_backfill
Create Date: 2026-11-16
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import erasures, retained_billing_records
from sqlalchemy import inspect, text

revision = "064_account_erasure"
down_revision = "063_graph_erasure_backfill"
branch_labels = None
depends_on = None

_TABLE = "retained_billing_records"
_INDEX = next(i for i in erasures.indexes if i.name == "idx_erasures_subject")


def upgrade() -> None:
    _INDEX.create(op.get_bind(), checkfirst=True)
    retained_billing_records.create(op.get_bind(), checkfirst=True)
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")


def downgrade() -> None:
    """Refuses while the table holds records (review L5): they are kept for the legal retention
    period, and a downgrade must not be the way they are lost."""
    bind = op.get_bind()
    if inspect(bind).has_table(_TABLE):
        kept = bind.execute(text(f"SELECT 1 FROM {_TABLE} LIMIT 1")).first()  # noqa: S608
        if kept is not None:
            msg = (
                f"refusing to downgrade 064: {_TABLE} holds billing records kept for the legal "
                "retention period; move them out first"
            )
            raise RuntimeError(msg)
    retained_billing_records.drop(bind, checkfirst=True)
    _INDEX.drop(bind, checkfirst=True)
