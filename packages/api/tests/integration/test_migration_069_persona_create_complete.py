"""Migration 069 (personas.create_complete) round trip, on Postgres (R9-293 round 3).

On a fresh database ``001_initial`` builds the column from the model, so ``069``'s upgrade is a
no-op there and CI never ran its ``ADD COLUMN`` (Finding 6). Here the schema goes to head, back
to ``068`` (the column dropped), takes a persona written by pre-069 code, and comes up again:
the real upgrade runs, and that persona is complete, so every owner-facing read still sees it.
The downgrade deletes unfinished creates before the column goes (Finding 8), so older code never
lists a half-made persona, and opens a persona erasure for each first (R9-322), so what no
cascade reaches (graph nodes, workspace files) is erased by the sweep.

Each test resets the schema at start and end so the shared integration DB is left clean.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from persona_api.db.models import persona_complete
from persona_api.db.models import personas as personas_t
from sqlalchemy import create_engine, inspect, select, text

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]  # packages/api
_BEFORE = "068_forgotten_conversations"
_OWNER = "u_069"
_HALF_MINTED = "persona_" + "4" * 32


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


def _has_column(database_url: str) -> bool:
    engine = create_engine(database_url)
    try:
        return any(c["name"] == "create_complete" for c in inspect(engine).get_columns("personas"))
    finally:
        engine.dispose()


@pytest.fixture
def clean_db(database_url: str) -> Iterator[str]:
    _reset_schema(database_url)
    yield database_url
    _reset_schema(database_url)


def test_069_upgrade_runs_for_real_and_an_existing_persona_is_complete(clean_db: str) -> None:
    cfg = _alembic_config(clean_db)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, _BEFORE)
    assert not _has_column(clean_db), "068 has no create_complete"

    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:  # a persona as pre-069 code writes it
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:u, 'u069@x.test')"), {"u": _OWNER}
            )
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES ('p_old', :u, 'y')"),
                {"u": _OWNER},
            )
        command.upgrade(cfg, "head")  # 069's ADD COLUMN, this time not a no-op
        assert _has_column(clean_db)
        with engine.connect() as conn:
            complete = conn.execute(
                select(personas_t.c.create_complete).where(personas_t.c.id == "p_old")
            ).scalar_one()
            visible = conn.execute(
                select(personas_t.c.id).where(personas_t.c.id == "p_old", persona_complete)
            ).all()
    finally:
        engine.dispose()
    assert complete is True
    assert [r.id for r in visible] == ["p_old"], "it passes persona_complete"


def test_069_downgrade_deletes_unfinished_creates_and_keeps_the_rest(clean_db: str) -> None:
    cfg = _alembic_config(clean_db)
    command.upgrade(cfg, "head")
    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:u, 'u069@x.test')"), {"u": _OWNER}
            )
            for pid, complete in (("p_done", True), ("p_half", False), (_HALF_MINTED, False)):
                conn.execute(
                    text(
                        "INSERT INTO personas (id, owner_id, yaml, create_complete) "
                        "VALUES (:p, :u, 'y', :c)"
                    ),
                    {"p": pid, "u": _OWNER, "c": complete},
                )
        command.downgrade(cfg, _BEFORE)
        with engine.connect() as conn:
            left = sorted(
                r[0]
                for r in conn.execute(
                    text("SELECT id FROM personas WHERE owner_id = :u"), {"u": _OWNER}
                )
            )
            opened = conn.execute(
                text(
                    "SELECT subject_id, owner_id, state FROM erasures "
                    "WHERE subject_kind = 'persona' ORDER BY subject_id"
                )
            ).all()
    finally:
        engine.dispose()
    assert left == ["p_done"], "older code never sees the half-made persona"
    # p_half is not a minted id, which no erasure acts on; p_done is kept.
    assert [tuple(r) for r in opened] == [(_HALF_MINTED, _OWNER, "pending")], (
        "the half-made persona's erasure is open for the sweep"
    )
