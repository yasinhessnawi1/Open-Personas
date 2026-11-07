"""Migration ``060_channel_persona_set_null``: deleting a persona clears a channel's pointer.

``connector_channels`` points at its foregrounded persona through a composite foreign key,
``(active_persona_id, owner_id) -> personas(id, owner_id)``, so a channel can never point at
another tenant's persona. It was declared ``ON DELETE SET NULL``, which nulls BOTH columns,
and ``owner_id`` is NOT NULL: deleting any persona that was some channel's active persona
raised instead. ``ON DELETE SET NULL (active_persona_id)`` (PostgreSQL 15 and later) nulls
only the pointer, so the channel and its owner stay. This matters beyond the persona delete
route, which clears the pointer itself: any delete that reaches ``personas`` (an account
cascade through ``users``, an operator's SQL) hit the same wall.

Mirrors the 054/059 programmatic Alembic pattern: reset the schema at start and end.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]
_PRED = "059_memory_chain_head_unique"
_HEAD = "060_channel_persona_set_null"
_FK = "fk_connector_channels_active_persona"
_OWNER = "user_channel_owner"
_PERSONA = "persona_c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0c0"


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


@pytest.fixture
def clean_db(database_url: str) -> Iterator[str]:
    _reset_schema(database_url)
    yield database_url
    _reset_schema(database_url)


def _fk_definition(database_url: str) -> str:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            return str(
                conn.execute(
                    text("SELECT pg_get_constraintdef(oid) FROM pg_constraint WHERE conname = :n"),
                    {"n": _FK},
                ).scalar_one()
            )
    finally:
        engine.dispose()


def _seed_channel_on_persona(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:o, :e)"),
                {"o": _OWNER, "e": f"{_OWNER}@x.test"},
            )
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
                {"p": _PERSONA, "o": _OWNER},
            )
            conn.execute(
                text(
                    "INSERT INTO connector_channels "
                    "(id, owner_id, platform, channel_key, active_persona_id) "
                    "VALUES ('ch1', :o, 'telegram', '42', :p)"
                ),
                {"o": _OWNER, "p": _PERSONA},
            )
    finally:
        engine.dispose()


def _delete_persona_row(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA})
    finally:
        engine.dispose()


def _channel(database_url: str) -> tuple[str, str | None]:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT owner_id, active_persona_id FROM connector_channels WHERE id = 'ch1'")
            ).one()
            return str(row.owner_id), row.active_persona_id
    finally:
        engine.dispose()


def test_at_head_a_persona_delete_clears_only_the_channel_pointer(clean_db: str) -> None:
    command.upgrade(_alembic_config(clean_db), _HEAD)
    assert "SET NULL (active_persona_id)" in _fk_definition(clean_db)

    _seed_channel_on_persona(clean_db)
    _delete_persona_row(clean_db)

    assert _channel(clean_db) == (_OWNER, None)


def test_before_the_migration_the_same_delete_is_refused(clean_db: str) -> None:
    """Pins the defect, so the migration is proven to be what fixes it."""
    from sqlalchemy.exc import IntegrityError

    command.upgrade(_alembic_config(clean_db), _PRED)
    _seed_channel_on_persona(clean_db)

    with pytest.raises(IntegrityError, match="owner_id"):
        _delete_persona_row(clean_db)


def test_downgrade_restores_the_previous_definition_and_upgrade_returns(clean_db: str) -> None:
    cfg = _alembic_config(clean_db)
    command.upgrade(cfg, _HEAD)
    command.downgrade(cfg, _PRED)
    definition = _fk_definition(clean_db)
    assert "ON DELETE SET NULL" in definition
    assert "(active_persona_id)" not in definition.split("ON DELETE", 1)[1]

    command.upgrade(cfg, _HEAD)
    assert "SET NULL (active_persona_id)" in _fk_definition(clean_db)
