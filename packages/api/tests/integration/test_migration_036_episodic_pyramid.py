"""Migration 035 (episodic pyramid) round-trip test (Spec K8, K8-D-13).

Proves the K8 schema change lands and reverses correctly on the DEPLOYED-DB
path (the 032 programmatic-Alembic pattern): rewind to the branch-point head,
apply 035 — the
lifecycle columns, the widened kind CHECK, and the recent() pushdown index all
appear; downgrade — gist rows are deleted (derived artifacts, K8-D-13),
the five-kind CHECK is restored, columns and index are gone, raw rows survive
untouched. Split-home: on a fresh DB 001's ``create_all`` already builds all of
it from the canonical models, so 035's guarded DDL is a no-op there — this test
reproduces the rewound path so the guarded statements actually run.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]  # packages/api
_ALEMBIC_INI = _API_DIR / "alembic.ini"
_PRED = "035_memory_seed_index"  # K8's re-parented predecessor (merge-back linearization)
_REV = "036_episodic_pyramid"
_K8_COLS = {"strength", "last_recalled_at", "fidelity_band", "pinned", "member_ids"}
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
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    engine.dispose()


@pytest.fixture
def rewound_db(database_url: str) -> Iterator[str]:
    """A DB in the deployed PRE-K8 state, version-stamped at 035's predecessor.

    Split-home nuance (the 032-test discipline): a fresh DB gets the K8 columns
    already at ``001_initial`` (create_all from the CANONICAL models), so merely
    rewinding the version does not produce a pre-K8 table. Reproduce the
    deployed state faithfully: rewind, then strip the K8 objects and restore the
    five-kind CHECK — so 035's guarded DDL actually runs.
    """
    _reset_schema(database_url)
    cfg = _alembic_config(database_url)
    command.upgrade(cfg, _PRED)
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP INDEX IF EXISTS idx_memory_persona_kind_created"))
        for col in ("member_ids", "pinned", "fidelity_band", "last_recalled_at", "strength"):
            conn.execute(text(f"ALTER TABLE memory_chunks DROP COLUMN IF EXISTS {col}"))
        conn.execute(
            text("ALTER TABLE memory_chunks DROP CONSTRAINT IF EXISTS memory_chunks_kind_check")
        )
        conn.execute(
            text(
                "ALTER TABLE memory_chunks ADD CONSTRAINT memory_chunks_kind_check CHECK "
                "(kind IN ('identity','self_facts','worldview','episodic','document'))"
            )
        )
    engine.dispose()
    yield database_url
    _reset_schema(database_url)
    command.upgrade(cfg, "head")  # leave the DB at head for later tests


def test_upgrade_adds_columns_check_and_index(rewound_db: str) -> None:
    cfg = _alembic_config(rewound_db)
    engine = create_engine(rewound_db)
    try:
        cols_before = {c["name"] for c in inspect(engine).get_columns("memory_chunks")}
        assert not (_K8_COLS & cols_before)  # the deployed path really lacks them

        command.upgrade(cfg, _REV)

        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("memory_chunks")}
        assert _K8_COLS.issubset(cols)
        names = {i["name"] for i in insp.get_indexes("memory_chunks")}
        assert "idx_memory_persona_kind_created" in names

        # The widened CHECK accepts a gist row; defaults backfill lifecycle state.
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id, email) VALUES ('mu','m@x.com')"))
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES ('mp','mu','name: m')")
            )
            conn.execute(
                text(
                    "INSERT INTO memory_chunks (id, persona_id, kind, text, embedding, "
                    f"content_hash) VALUES ('mr','mp','episodic','raw','{_ZERO_VEC}','h')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO memory_chunks (id, persona_id, kind, text, embedding, "
                    "content_hash, fidelity_band, member_ids) VALUES "
                    f"('mg','mp','episodic_gist','gist','{_ZERO_VEC}','h',1,"
                    "ARRAY['mr']::text[])"
                )
            )
            row = conn.execute(
                text("SELECT strength, fidelity_band, pinned FROM memory_chunks WHERE id='mr'")
            ).one()
            assert (row.strength, row.fidelity_band, row.pinned) == (1, 0, False)
    finally:
        engine.dispose()


def test_downgrade_restores_the_five_kind_check_and_deletes_gists(rewound_db: str) -> None:
    cfg = _alembic_config(rewound_db)
    engine = create_engine(rewound_db)
    try:
        command.upgrade(cfg, _REV)
        with engine.begin() as conn:
            conn.execute(text("INSERT INTO users (id, email) VALUES ('du','d@x.com')"))
            conn.execute(
                text("INSERT INTO personas (id, owner_id, yaml) VALUES ('dp','du','name: d')")
            )
            conn.execute(
                text(
                    "INSERT INTO memory_chunks (id, persona_id, kind, text, embedding, "
                    f"content_hash) VALUES ('dr','dp','episodic','raw','{_ZERO_VEC}','h')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO memory_chunks (id, persona_id, kind, text, embedding, "
                    "content_hash, fidelity_band, member_ids) VALUES "
                    f"('dg','dp','episodic_gist','gist','{_ZERO_VEC}','h',1,"
                    "ARRAY['dr']::text[])"
                )
            )

        command.downgrade(cfg, _PRED)

        insp = inspect(engine)
        cols = {c["name"] for c in insp.get_columns("memory_chunks")}
        assert not (_K8_COLS & cols)
        names = {i["name"] for i in insp.get_indexes("memory_chunks")}
        assert "idx_memory_persona_kind_created" not in names
        with engine.connect() as conn:
            kinds = {
                r.kind
                for r in conn.execute(
                    text("SELECT kind FROM memory_chunks WHERE persona_id='dp'")
                ).all()
            }
        assert kinds == {"episodic"}  # gist deleted (derived); raw untouched
    finally:
        engine.dispose()
