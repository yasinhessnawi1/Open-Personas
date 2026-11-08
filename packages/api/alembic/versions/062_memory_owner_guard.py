"""A memory row needs an owner that exists (R9-284).

``memory_chunks.persona_id`` holds a ``personas.id`` for most kinds and a ``conversations.id`` for
``document`` rows, so migration 007 dropped the foreign key and nothing replaced it. A write that
lands after its persona has gone therefore succeeds and leaves a row nothing can reach: a
consolidation pass queued before the delete, a turn still streaming. On the hosted edition row
level security happens to refuse it. The community edition connects as a superuser, where nothing
did.

The trigger is the conditional foreign key Postgres cannot declare. It takes ``FOR KEY SHARE`` on
the owner, exactly as a foreign key check does and at the same cost, so a write racing the delete
of its owner waits for the delete and is then refused. It raises ``foreign_key_violation``, so a
caller sees the error class it would from a real foreign key.

It guards new writes only: an ``UPDATE`` that leaves ``persona_id`` and ``kind`` alone does not
fire it, so ``persona repair`` can still move version pointers on a row that is already orphaned,
and nothing stops the delete of one. Existing orphans are the sweep's job. ``pg_restore`` creates
triggers after loading data, so a restore of a database holding orphans still loads.

Takes its lock the 057 way: a 5 second ``lock_timeout``, so behind a long transaction the deploy
fails and can be re-run instead of queueing every later query behind it. ``memory_chunks`` is not
declared with this in the model, so a ``create_all`` database (some test fixtures) has no guard;
every real database reaches it through this chain.

Revision ID: 062_memory_owner_guard
Revises: 061_erasures
Create Date: 2026-11-08
"""

from __future__ import annotations

from alembic import op

revision = "062_memory_owner_guard"
down_revision = "061_erasures"
branch_labels = None
depends_on = None

_FUNCTION = "memory_chunks_owner_guard"
_TRIGGER = "memory_chunks_owner_guard_trigger"

# The message names the kind and never the row: this reaches logs.
_CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {_FUNCTION}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.kind = 'document' THEN
        PERFORM 1 FROM conversations WHERE id = NEW.persona_id FOR KEY SHARE;
    ELSE
        PERFORM 1 FROM personas WHERE id = NEW.persona_id FOR KEY SHARE;
    END IF;
    IF NOT FOUND THEN
        RAISE EXCEPTION '{_FUNCTION}: the owner of this % row does not exist', NEW.kind
            USING ERRCODE = 'foreign_key_violation';
    END IF;
    RETURN NEW;
END
$$
"""


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(_CREATE_FUNCTION)
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON memory_chunks")
    op.execute(
        f"CREATE TRIGGER {_TRIGGER} BEFORE INSERT OR UPDATE OF persona_id, kind "
        f"ON memory_chunks FOR EACH ROW EXECUTE FUNCTION {_FUNCTION}()"
    )
    op.execute("RESET lock_timeout")


def downgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.execute(f"DROP TRIGGER IF EXISTS {_TRIGGER} ON memory_chunks")
    op.execute(f"DROP FUNCTION IF EXISTS {_FUNCTION}()")
    op.execute("RESET lock_timeout")
