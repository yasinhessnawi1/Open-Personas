"""Migration ``062``: a memory row needs an owner that exists (R9-284).

``memory_chunks.persona_id`` means a persona for most kinds and a conversation for documents, so
migration 007 dropped the foreign key and nothing replaced it. A write that lands after the
persona has gone therefore succeeds and leaves a row nothing can reach: a consolidation pass
queued before the delete, a turn still streaming. On the hosted edition row level security
happens to refuse it. On the community edition, connected as a superuser, nothing did.

The trigger is the conditional foreign key Postgres cannot declare. It takes the same
``FOR KEY SHARE`` lock a foreign key check takes, so a write racing a delete waits for it and is
then refused, instead of slipping in behind it.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError, OperationalError, ProgrammingError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]
_PRED = "061_erasures"
_HEAD = "062_memory_owner_guard"
_OWNER = "user_guard"
_PERSONA = "persona_e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1e1"
_GONE = "persona_e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2e2"
_CONVERSATION = "conv_guard"
_INSERT = text(
    "INSERT INTO memory_chunks (id, persona_id, kind, text, embedding, content_hash) "
    "VALUES (:id, :p, :k, 'x', array_fill(0, ARRAY[384])::vector, 'h')"
)


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _reset_schema(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()


def _seed_owner(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'guard@x.test')"), {"o": _OWNER}
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
            {"p": _PERSONA, "o": _OWNER},
        )
        conn.execute(
            text("INSERT INTO conversations (id, owner_id, persona_id) VALUES (:c, :o, :p)"),
            {"c": _CONVERSATION, "o": _OWNER, "p": _PERSONA},
        )


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    _reset_schema(database_url)
    command.upgrade(_alembic_config(database_url), _HEAD)
    eng = create_engine(database_url)
    _seed_owner(eng)
    yield eng
    eng.dispose()
    _reset_schema(database_url)


@pytest.mark.parametrize(
    "kind", ["identity", "self_facts", "worldview", "episodic", "episodic_gist", "core_memory"]
)
def test_a_persona_kind_needs_its_persona(engine: Engine, kind: str) -> None:
    with engine.begin() as conn:
        conn.execute(_INSERT, {"id": f"ok-{kind}", "p": _PERSONA, "k": kind})
    with pytest.raises(IntegrityError, match="memory_chunks_owner_guard"), engine.begin() as conn:
        conn.execute(_INSERT, {"id": f"no-{kind}", "p": _GONE, "k": kind})


def test_a_document_needs_its_conversation(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(_INSERT, {"id": "doc-ok", "p": _CONVERSATION, "k": "document"})
    with pytest.raises(IntegrityError, match="memory_chunks_owner_guard"), engine.begin() as conn:
        conn.execute(_INSERT, {"id": "doc-no", "p": "conv_gone", "k": "document"})


def test_a_persona_kind_is_not_satisfied_by_a_conversation_or_the_reverse(engine: Engine) -> None:
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(_INSERT, {"id": "a", "p": _CONVERSATION, "k": "episodic"})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(_INSERT, {"id": "b", "p": _PERSONA, "k": "document"})


def test_moving_a_row_onto_a_missing_owner_is_refused(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(_INSERT, {"id": "m", "p": _PERSONA, "k": "episodic"})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE memory_chunks SET persona_id = :g WHERE id = 'm'"), {"g": _GONE})
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(text("UPDATE memory_chunks SET kind = 'document' WHERE id = 'm'"))


def test_an_upsert_for_a_missing_owner_is_refused(engine: Engine) -> None:
    upsert = text(
        "INSERT INTO memory_chunks (id, persona_id, kind, text, embedding, content_hash) "
        "VALUES ('u', :p, 'episodic', 'x', array_fill(0, ARRAY[384])::vector, 'h') "
        "ON CONFLICT (id) DO UPDATE SET text = EXCLUDED.text"
    )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(upsert, {"p": _GONE})


def test_the_guard_holds_for_the_app_role_under_row_level_security(
    engine: Engine, database_url: str
) -> None:
    """The role the hosted request path uses: its own persona passes, an unknown one does not."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    _ = database_url
    with engine.begin() as conn:
        conn.execute(text("GRANT USAGE ON SCHEMA public TO persona_app"))
        conn.execute(
            text(
                "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO persona_app"
            )
        )
    rls = make_rls_engine(app_url)
    token = current_user_id.set(_OWNER)
    try:
        with rls.begin() as conn:
            conn.execute(_INSERT, {"id": "app-ok", "p": _PERSONA, "k": "episodic"})
        with pytest.raises((IntegrityError, ProgrammingError)), rls.begin() as conn:
            conn.execute(_INSERT, {"id": "app-no", "p": _GONE, "k": "episodic"})
    finally:
        current_user_id.reset(token)
        rls.dispose()


@pytest.mark.parametrize(
    ("table", "owner", "kind"),
    [("personas", _PERSONA, "episodic"), ("conversations", _CONVERSATION, "document")],
)
def test_a_write_racing_a_delete_waits_and_is_then_refused(
    engine: Engine, table: str, owner: str, kind: str
) -> None:
    """Commit order decides nothing: the write cannot land behind the delete."""
    outcome: list[object] = []

    def write() -> None:
        try:
            with engine.begin() as conn:
                conn.execute(_INSERT, {"id": "late", "p": owner, "k": kind})
            outcome.append("written")
        except IntegrityError as exc:
            outcome.append(exc)

    with engine.connect() as deleter:
        tx = deleter.begin()
        deleter.execute(text(f"DELETE FROM {table} WHERE id = :o"), {"o": owner})  # noqa: S608
        writer = threading.Thread(target=write)
        writer.start()
        writer.join(timeout=1.5)
        assert writer.is_alive(), "the write has to wait for the delete, not pass it"
        assert outcome == []
        tx.commit()
    writer.join(timeout=10)

    assert not writer.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], IntegrityError)
    with engine.begin() as conn:
        left = conn.execute(
            text("SELECT count(*) FROM memory_chunks WHERE persona_id = :o"), {"o": owner}
        ).scalar()
    assert left == 0


def test_existing_orphans_can_still_be_repaired_and_removed(database_url: str) -> None:
    """A restored database has them. The guard is for new writes; the sweep clears the old."""
    _reset_schema(database_url)
    cfg = _alembic_config(database_url)
    command.upgrade(cfg, _PRED)
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(_INSERT, {"id": "orphan", "p": _GONE, "k": "episodic"})

        command.upgrade(cfg, _HEAD)

        with engine.begin() as conn:
            conn.execute(
                text("UPDATE memory_chunks SET superseded_by = NULL, version = 2 WHERE id = :i"),
                {"i": "orphan"},
            )
            removed = conn.execute(text("DELETE FROM memory_chunks WHERE id = 'orphan'")).rowcount
        assert removed == 1
    finally:
        engine.dispose()
        _reset_schema(database_url)


def test_downgrade_removes_the_guard_and_upgrade_returns_it(
    engine: Engine, database_url: str
) -> None:
    cfg = _alembic_config(database_url)
    command.downgrade(cfg, _PRED)
    with engine.begin() as conn:
        conn.execute(_INSERT, {"id": "free", "p": _GONE, "k": "episodic"})
        assert conn.execute(text("SELECT to_regproc('memory_chunks_owner_guard')")).scalar() is None
        conn.execute(text("DELETE FROM memory_chunks WHERE id = 'free'"))

    command.upgrade(cfg, _HEAD)

    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(_INSERT, {"id": "again", "p": _GONE, "k": "episodic"})


def test_a_held_lock_fails_the_migration_fast_instead_of_queueing(database_url: str) -> None:
    """Behind a long transaction the deploy gives up and can re-run; it never queues writes.

    The lock lets go by itself after 20 seconds. So a migration with no timeout waits and then
    succeeds, and this fails on the missing refusal instead of hanging the run.
    """
    _reset_schema(database_url)
    cfg = _alembic_config(database_url)
    command.upgrade(cfg, _PRED)
    holder = create_engine(database_url)
    try:
        with holder.connect() as conn:
            tx = conn.begin()
            conn.execute(text("LOCK TABLE memory_chunks IN ACCESS EXCLUSIVE MODE"))
            release = threading.Timer(20, tx.rollback)
            release.start()
            try:
                started = time.monotonic()
                with pytest.raises(OperationalError, match="lock timeout"):
                    command.upgrade(cfg, _HEAD)
                assert time.monotonic() - started < 15
            finally:
                release.cancel()
                release.join()
                if tx.is_active:
                    tx.rollback()
        command.upgrade(cfg, _HEAD)
    finally:
        holder.dispose()
        _reset_schema(database_url)
