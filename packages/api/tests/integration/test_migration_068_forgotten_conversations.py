"""Migration ``068``: a forgotten conversation refuses new memory (R9-311).

Deleting a conversation with ``forget_memory`` marks it forgotten in the delete's transaction.
The memory owner guard then holds a stamped row's conversation ``FOR KEY SHARE`` and refuses the
write once that conversation is gone and marked, so a turn still streaming cannot bring the
forgotten memory back. A plain delete marks nothing and its late writes are kept, and so are
legacy chunks the community import brings across for long-deleted conversations.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]
_PRED = "066_wellbeing_disclosure_release"
_HEAD = "068_forgotten_conversations"
_OWNER = "user_forgot"
_PERSONA = "persona_f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1"
_LIVE = "conv_live"
_FORGOTTEN = "conv_forgotten"
_PLAIN = "conv_plain"
_INSERT = text(
    "INSERT INTO memory_chunks (id, persona_id, kind, text, embedding, content_hash, metadata) "
    "VALUES (:id, :p, 'episodic', 'x', array_fill(0, ARRAY[384])::vector, 'h', "
    "CAST(:m AS jsonb))"
)


def _stamp(conversation_id: str) -> str:
    return '{"conversation_id": "' + conversation_id + '"}'


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


def _seed(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'forgot@x.test')"), {"o": _OWNER}
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
            {"p": _PERSONA, "o": _OWNER},
        )
        for conversation in (_LIVE, _FORGOTTEN, _PLAIN):
            conn.execute(
                text("INSERT INTO conversations (id, owner_id, persona_id) VALUES (:c, :o, :p)"),
                {"c": conversation, "o": _OWNER, "p": _PERSONA},
            )
        # The forget: marked and deleted in one transaction. The plain delete marks nothing.
        conn.execute(
            text(
                "INSERT INTO forgotten_conversations (conversation_id, owner_id, persona_id) "
                "VALUES (:c, :o, :p)"
            ),
            {"c": _FORGOTTEN, "o": _OWNER, "p": _PERSONA},
        )
        conn.execute(
            text("DELETE FROM conversations WHERE id IN (:f, :pl)"),
            {"f": _FORGOTTEN, "pl": _PLAIN},
        )


@pytest.fixture
def engine(database_url: str) -> Iterator[Engine]:
    _reset_schema(database_url)
    command.upgrade(_alembic_config(database_url), _HEAD)
    eng = create_engine(database_url)
    _seed(eng)
    yield eng
    eng.dispose()
    _reset_schema(database_url)


def test_a_row_stamped_with_a_forgotten_conversation_is_refused(engine: Engine) -> None:
    with pytest.raises(IntegrityError, match="memory_chunks_owner_guard"), engine.begin() as conn:
        conn.execute(_INSERT, {"id": "late", "p": _PERSONA, "m": _stamp(_FORGOTTEN)})


@pytest.mark.parametrize(
    ("chunk_id", "metadata"),
    [
        ("live", _stamp(_LIVE)),
        ("plain", _stamp(_PLAIN)),
        ("empty", _stamp("")),
        ("unstamped", "{}"),
    ],
)
def test_every_other_stamp_still_writes(engine: Engine, chunk_id: str, metadata: str) -> None:
    """Live, plainly deleted (its memory is kept by contract), empty, and legacy chunks."""
    with engine.begin() as conn:
        conn.execute(_INSERT, {"id": chunk_id, "p": _PERSONA, "m": metadata})


def test_restamping_a_row_onto_a_forgotten_conversation_is_refused(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(_INSERT, {"id": "moved", "p": _PERSONA, "m": _stamp(_LIVE)})
    with pytest.raises(IntegrityError, match="memory_chunks_owner_guard"), engine.begin() as conn:
        conn.execute(
            text("UPDATE memory_chunks SET metadata = CAST(:m AS jsonb) WHERE id = 'moved'"),
            {"m": _stamp(_FORGOTTEN)},
        )


def test_a_reinforcement_of_a_row_left_from_before_still_updates(engine: Engine) -> None:
    """Only a write that names the conversation anew is checked, not lifecycle updates."""
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE memory_chunks DISABLE TRIGGER USER"))
        conn.execute(_INSERT, {"id": "old", "p": _PERSONA, "m": _stamp(_FORGOTTEN)})
        conn.execute(text("ALTER TABLE memory_chunks ENABLE TRIGGER USER"))
    with engine.begin() as conn:
        updated = conn.execute(
            text("UPDATE memory_chunks SET strength = strength + 1 WHERE id = 'old'")
        ).rowcount
        removed = conn.execute(text("DELETE FROM memory_chunks WHERE id = 'old'")).rowcount
    assert (updated, removed) == (1, 1)


def test_a_write_racing_the_forget_waits_and_is_then_refused(engine: Engine) -> None:
    """The delete holds the row; the write waits, then finds it gone and forgotten."""
    racing = "conv_racing"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO conversations (id, owner_id, persona_id) VALUES (:c, :o, :p)"),
            {"c": racing, "o": _OWNER, "p": _PERSONA},
        )
    outcome: list[object] = []

    def write() -> None:
        try:
            with engine.begin() as conn:
                conn.execute(_INSERT, {"id": "racer", "p": _PERSONA, "m": _stamp(racing)})
            outcome.append("written")
        except IntegrityError as exc:
            outcome.append(exc)

    with engine.connect() as deleter:
        tx = deleter.begin()
        deleter.execute(
            text("SELECT id FROM conversations WHERE id = :c FOR UPDATE"), {"c": racing}
        )
        writer = threading.Thread(target=write)
        writer.start()
        writer.join(timeout=1.5)
        assert writer.is_alive(), "the write has to wait for the forget, not pass it"
        deleter.execute(
            text(
                "INSERT INTO forgotten_conversations (conversation_id, owner_id, persona_id) "
                "VALUES (:c, :o, :p)"
            ),
            {"c": racing, "o": _OWNER, "p": _PERSONA},
        )
        deleter.execute(text("DELETE FROM conversations WHERE id = :c"), {"c": racing})
        tx.commit()
    writer.join(timeout=10)

    assert not writer.is_alive()
    assert len(outcome) == 1
    assert isinstance(outcome[0], IntegrityError)
    with engine.begin() as conn:
        left = conn.execute(text("SELECT count(*) FROM memory_chunks WHERE id = 'racer'")).scalar()
    assert left == 0


def test_the_guard_reads_the_mark_for_the_app_role_under_row_level_security(
    engine: Engine,
) -> None:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
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
            conn.execute(_INSERT, {"id": "app-live", "p": _PERSONA, "m": _stamp(_LIVE)})
        with pytest.raises(IntegrityError, match="memory_chunks_owner_guard"), rls.begin() as conn:
            conn.execute(_INSERT, {"id": "app-late", "p": _PERSONA, "m": _stamp(_FORGOTTEN)})
        # The request path marks a forget on this role, in the owner's scope.
        with rls.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO forgotten_conversations (conversation_id, owner_id, persona_id) "
                    "VALUES ('conv_app', :o, :p)"
                ),
                {"o": _OWNER, "p": _PERSONA},
            )
    finally:
        current_user_id.reset(token)
        rls.dispose()


def test_the_mark_goes_with_its_persona(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})
        left = conn.execute(text("SELECT count(*) FROM forgotten_conversations")).scalar()
    assert left == 0


def test_downgrade_restores_the_062_guard_and_upgrade_returns_this_one(
    engine: Engine, database_url: str
) -> None:
    cfg = _alembic_config(database_url)
    command.downgrade(cfg, _PRED)
    with engine.begin() as conn:
        conn.execute(_INSERT, {"id": "free", "p": _PERSONA, "m": _stamp(_FORGOTTEN)})
        missing = conn.execute(text("SELECT to_regclass('forgotten_conversations')")).scalar()
        conn.execute(text("DELETE FROM memory_chunks WHERE id = 'free'"))
    assert missing is None

    command.upgrade(cfg, _HEAD)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO forgotten_conversations (conversation_id, owner_id, persona_id) "
                "VALUES (:c, :o, :p)"
            ),
            {"c": _FORGOTTEN, "o": _OWNER, "p": _PERSONA},
        )
    with pytest.raises(IntegrityError, match="memory_chunks_owner_guard"), engine.begin() as conn:
        conn.execute(_INSERT, {"id": "guarded", "p": _PERSONA, "m": _stamp(_FORGOTTEN)})
