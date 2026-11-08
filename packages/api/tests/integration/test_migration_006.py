"""Migration 006 (memory_chunks.kind_check accepts 'document') round-trip test.

Spec 19 D-19-X-memory-chunks-kind-check-migration (chain entry 23) — a
mid-flight LAND discovery surfaced by the F5 T16 production-shape
integration test.

Proves the additive CHECK extension:

1. At ``head`` the four typed-store kinds AND ``'document'`` all insert
   cleanly (the new accepted set is a strict superset).
2. ``downgrade`` to ``005`` removes ``'document'`` from the accepted set;
   a fresh ``'document'`` insert then fails with the original CHECK.

Mirrors the spec-08 programmatic Alembic pattern (cwd-independent) in
:mod:`test_migration_002`. Each test resets the schema at the start AND
end so the shared integration DB stays clean.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _API_DIR / "alembic.ini"

# 384-dim zero vector literal for the pgvector NOT NULL ``embedding`` column.
_ZERO_VEC = "[" + ",".join(["0"] * 384) + "]"


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_ALEMBIC_INI))
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


def _seed_owner_and_persona(database_url: str, *, owner: str, persona_id: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e)"),
                {"i": owner, "e": f"{owner}@x.test"},
            )
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, 'y')"),
                {"i": persona_id, "o": owner},
            )
    finally:
        engine.dispose()


def _insert_chunk(database_url: str, *, persona_id: str, kind: str, chunk_id: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO memory_chunks "
                    "(id, persona_id, kind, text, embedding, content_hash) "
                    "VALUES (:i, :p, :k, 't', CAST(:v AS vector), 'h')"
                ),
                {"i": chunk_id, "p": persona_id, "k": kind, "v": _ZERO_VEC},
            )
    finally:
        engine.dispose()


def test_006_accepts_document_kind_at_head(clean_db: str) -> None:
    """At head, all five kinds (four typed + 'document') insert cleanly."""
    cfg = _alembic_config(clean_db)
    command.upgrade(cfg, "head")
    _seed_owner_and_persona(clean_db, owner="u_006", persona_id="p_006")
    # A document row is keyed by its conversation, and that owner has to exist (062).
    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO conversations (id, owner_id, persona_id) "
                    "VALUES ('conv_006', 'u_006', 'p_006')"
                )
            )
    finally:
        engine.dispose()

    # All five kinds satisfy the new CHECK.
    for idx, kind in enumerate(("identity", "self_facts", "worldview", "episodic", "document")):
        owner = "conv_006" if kind == "document" else "p_006"
        _insert_chunk(clean_db, persona_id=owner, kind=kind, chunk_id=f"c_006_{idx}")


def test_006_downgrade_restores_old_check(clean_db: str) -> None:
    """Downgrade to 005 reverts the CHECK; 'document' inserts then fail."""
    cfg = _alembic_config(clean_db)
    command.upgrade(cfg, "head")
    _seed_owner_and_persona(clean_db, owner="u_006d", persona_id="p_006d")

    command.downgrade(cfg, "005_memory_chunks_doc_rls")

    with pytest.raises(IntegrityError):
        _insert_chunk(clean_db, persona_id="p_006d", kind="document", chunk_id="c_006d_x")
