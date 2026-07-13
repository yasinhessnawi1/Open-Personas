"""Calls-surface reads (Spec V9, V9-D-5).

The Calls history is a thin, READ-ONLY view over the durable ``calls`` envelope
the voice runtime writes (V9-D-5). CQS: this module only queries — it never
writes (the call-record is authored by the API-free voice ``CallRecorder``). RLS
scopes every read to the caller's tenant exactly like ``conversations``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import select

from persona_api.db.models import calls as calls_t
from persona_api.db.models import conversations as conversations_t

if TYPE_CHECKING:
    from sqlalchemy import Engine


def list_calls(*, rls_engine: Engine, limit: int, offset: int) -> list[dict[str, object]]:
    """List the caller's calls (RLS-scoped), newest-first, paginated.

    Read-only (CQS). Ordered by ``started_at`` descending so the most recent call
    leads the history; ``call_id`` breaks ties deterministically. Each row carries
    ``conversation_id`` — the link to the saved transcript
    (``GET /v1/conversations/{conversation_id}``) — and its ``title`` (R9-028):
    the ``calls`` table has no title column of its own, so this joins
    ``conversations.title`` (the SAME column ``ConversationSummary`` reads). An
    INNER join is safe: every ``calls`` row FKs ``ON DELETE CASCADE`` to its
    conversation (V9-D-5), so a call-record can never outlive its conversation.
    RLS is enforced per-table by Postgres regardless of the join — both tables
    scope on the same owner.
    """
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(
                select(calls_t, conversations_t.c.title)
                .join(conversations_t, conversations_t.c.id == calls_t.c.conversation_id)
                .order_by(calls_t.c.started_at.desc(), calls_t.c.call_id.desc())
                .limit(limit)
                .offset(offset)
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]
