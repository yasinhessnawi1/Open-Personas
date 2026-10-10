"""Each owner's documents waiting for the background index (D-FV0-31, fourth review).

The fourth security review found that one owner starved every other owner's index: each
of an owner's documents was its own ``document_ingest`` job on the one serial lane, and a
document put off by its owner's quota still cost a claim and a poll interval of lane time.
300 such documents delayed another owner's first chunk from 3.9 s to 377 s.

The index now runs one job per OWNER, which walks that owner's waiting documents, and the
documents wait here: ``document_index_backlog``, one row per (owner, conversation,
document), ``added_at`` ordering the walk. RLS ENABLE + FORCE with the direct owner
predicate (the 048 / 051 template), so a job sees only its own owner's rows.

Queued jobs of the old per-document shape (their payload names a ``doc_ref``) become
backlog rows, if their ids pass the index's id rule, and are deleted; the owner's next
upload, turn or document listing starts the owner's job. T0 had not shipped, so only
development databases hold such jobs.

Split-home: declared on the canonical ``persona_api.db.models`` table, so on a fresh
database ``001_initial``'s ``metadata.create_all`` already builds it and the
``checkfirst`` create below is a no-op; on a deployed Postgres it genuinely creates it.
RLS is created entirely here. A community SQLite database is untouched (its index runs
from the in-process queue, which keeps its backlog in memory).

Numbering: T0 lands before FV0, whose own migration becomes 061.

Revision ID: 060_document_index_backlog
Revises: 059_jobs_inflight_by_type
Create Date: 2026-10-10
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import document_index_backlog

revision = "060_document_index_backlog"
down_revision = "059_jobs_inflight_by_type"
branch_labels = None
depends_on = None

# Direct owner-scoped predicate; an unset GUC yields NULL and matches no row (fails closed).
_RLS_PREDICATE = "owner_id = current_setting('app.current_user_id', true)"

# The ids the index accepts (``persona_api.services.document_index``'s payload rule, copied
# here so the migration does not move with the code): a job whose ids fail it is not
# copied into the backlog (fifth review); it is deleted with the other old-shape jobs.
_SAFE_ID = "^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"
_SAFE_DOC_REF = "^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("SET lock_timeout = '5s'")
    document_index_backlog.create(bind, checkfirst=True)
    op.execute("ALTER TABLE document_index_backlog ENABLE ROW LEVEL SECURITY")
    op.execute("ALTER TABLE document_index_backlog FORCE ROW LEVEL SECURITY")
    op.execute("DROP POLICY IF EXISTS user_isolation ON document_index_backlog")
    op.execute(
        "CREATE POLICY user_isolation ON document_index_backlog "
        f"USING ({_RLS_PREDICATE}) WITH CHECK ({_RLS_PREDICATE})"
    )
    op.execute(
        "INSERT INTO document_index_backlog (owner_id, persona_id, conversation_id, doc_ref) "
        "SELECT DISTINCT owner_id, payload->>'persona_id', payload->>'conversation_id', "
        "payload->>'doc_ref' FROM jobs "
        "WHERE type = 'document_ingest' AND state = 'queued' AND payload ? 'doc_ref' "
        f"AND payload->>'persona_id' ~ '{_SAFE_ID}' "
        f"AND payload->>'conversation_id' ~ '{_SAFE_ID}' "
        f"AND payload->>'doc_ref' ~ '{_SAFE_DOC_REF}' "
        "AND strpos(payload->>'doc_ref', '..') = 0 "
        "ON CONFLICT DO NOTHING"
    )
    op.execute(
        "DELETE FROM jobs WHERE type = 'document_ingest' AND state = 'queued' "
        "AND payload ? 'doc_ref'"
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("SET lock_timeout = '5s'")
    op.execute("DROP POLICY IF EXISTS user_isolation ON document_index_backlog")
    document_index_backlog.drop(bind, checkfirst=True)
    op.execute("RESET lock_timeout")
