"""A forgotten conversation refuses new memory: ``forgotten_conversations`` + the guard (R9-311).

Deleting a conversation with ``forget_memory`` forgets the episodic memory stamped with it
(``metadata.conversation_id``). That forget now commits as a durable job with the row delete, so
a failure is retried rather than stranding the memory. What a forget run after the commit cannot
see is a write still in flight: a turn streaming when the user deleted, a voice call, an
origination. Such a write lands after the forget and the memory is back.

This is 062's answer applied to the conversation. The delete marks the conversation forgotten in
its own transaction (one row here, ids and a time). The memory owner guard, for a row stamped
with a conversation, now takes ``FOR KEY SHARE`` on that conversation's row, as a foreign key
check would, and when the row is not there and the conversation is marked forgotten it refuses
the write. So a write racing the delete waits for it (the delete holds the row ``FOR UPDATE``)
and is then refused, and the set of stamped rows the forget has to remove is final at the commit.

Only a forgotten conversation refuses. A plain delete keeps the persona's memory by contract, and
the community import re-embeds legacy chunks whose conversation was deleted long ago; neither
may start failing. An empty stamp names no conversation and is not checked. The ``document``
kind is unchanged: its ``persona_id`` already is its conversation.

The guard now also fires on an ``UPDATE`` of ``metadata``, so a row cannot be restamped onto a
forgotten conversation either. Reinforcement and band updates leave ``metadata`` alone.

Owner-scoped under RLS like every tenant table (the 065 template): ENABLE + FORCE + a
``user_isolation`` policy. The guard runs as the writer, and every memory writer runs in its
owner's scope (or bypasses RLS), so it sees the marks it needs. No GRANTs (D-07-5):
``release.py`` re-applies them on every deploy. ``Table.create(checkfirst=True)`` no-ops on a
fresh database, where ``001_initial``'s ``metadata.create_all`` already built it from the model.

The function keeps 062's name and message prefix, so ``PostgresBackend`` still reports a refusal
as ``MemoryOwnerMissingError``. Takes its lock the 057 way: a 5 second ``lock_timeout``.

Revision ID: 068_forgotten_conversations
Revises: 066_wellbeing_disclosure_release
Create Date: 2026-11-20
"""

from __future__ import annotations

from alembic import op
from persona_api.db.models import forgotten_conversations

revision = "068_forgotten_conversations"
down_revision = "066_wellbeing_disclosure_release"
branch_labels = None
depends_on = None

_TABLE = "forgotten_conversations"
_FUNCTION = "memory_chunks_owner_guard"
_TRIGGER = "memory_chunks_owner_guard_trigger"

# The missing-ok ``current_setting(..., true)`` form fails CLOSED: an unset GUC yields NULL,
# which matches no row.
_PREDICATE = "owner_id = current_setting('app.current_user_id', true)"

POLICY = f"CREATE POLICY user_isolation ON {_TABLE} USING ({_PREDICATE}) WITH CHECK ({_PREDICATE})"

# The message names the kind and never the row: this reaches logs. The tables are schema
# qualified so the session's search_path cannot change which ones are checked. ``->>`` on a
# JSON value that is not an object is NULL, so a malformed metadata value is simply unstamped.
GUARD = f"""
CREATE OR REPLACE FUNCTION {_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE
    stamped text;
BEGIN
    IF NEW.kind = 'document' THEN
        PERFORM 1 FROM public.conversations WHERE id = NEW.persona_id FOR KEY SHARE;
    ELSE
        PERFORM 1 FROM public.personas WHERE id = NEW.persona_id FOR KEY SHARE;
    END IF;
    IF NOT FOUND THEN
        RAISE EXCEPTION '{_FUNCTION}: the owner of this % row does not exist', NEW.kind
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    IF NEW.kind <> 'document' THEN
        stamped := NEW.metadata ->> 'conversation_id';
        IF stamped IS NOT NULL AND stamped <> '' THEN
            PERFORM 1 FROM public.conversations WHERE id = stamped FOR KEY SHARE;
            IF NOT FOUND THEN
                PERFORM 1 FROM public.{_TABLE} WHERE conversation_id = stamped;
                IF FOUND THEN
                    RAISE EXCEPTION
                        '{_FUNCTION}: the conversation of this % row was forgotten', NEW.kind
                        USING ERRCODE = 'foreign_key_violation';
                END IF;
            END IF;
        END IF;
    END IF;
    RETURN NEW;
END
$$
"""

# 062's function, restored on downgrade.
_GUARD_062 = f"""
CREATE OR REPLACE FUNCTION {_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kind = 'document' THEN
        PERFORM 1 FROM public.conversations WHERE id = NEW.persona_id FOR KEY SHARE;
    ELSE
        PERFORM 1 FROM public.personas WHERE id = NEW.persona_id FOR KEY SHARE;
    END IF;
    IF NOT FOUND THEN
        RAISE EXCEPTION '{_FUNCTION}: the owner of this % row does not exist', NEW.kind
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END
$$
"""


def _trigger(columns: str) -> str:
    return (
        f"CREATE TRIGGER {_TRIGGER} BEFORE INSERT OR UPDATE OF {columns} "
        f"ON memory_chunks FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()"
    )


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    forgotten_conversations.create(op.get_bind(), checkfirst=True)
    op.execute(f"ALTER TABLE {_TABLE} ENABLE ROW LEVEL SECURITY")
    op.execute(f"ALTER TABLE {_TABLE} FORCE ROW LEVEL SECURITY")
    op.execute(f"DROP POLICY IF EXISTS user_isolation ON {_TABLE}")
    op.execute(POLICY)
    op.execute(GUARD)
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON memory_chunks")
    op.execute(_trigger("persona_id, kind, metadata"))
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON memory_chunks")
    op.execute(_GUARD_062)
    op.execute(_trigger("persona_id, kind"))
    op.execute(f"DROP POLICY IF EXISTS user_isolation ON {_TABLE}")
    forgotten_conversations.drop(op.get_bind(), checkfirst=True)
    op.execute("RESET lock_timeout")
