"""Migration ``061_erasures``: the ledger table, scoped to its owner, outliving its owner.

``persona_app`` holds blanket DML on every table, so row level security is what keeps one
tenant's erasures from another. There is no foreign key to ``users`` or ``personas``, because
the row has to survive the deletion it records.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services.erasure_ledger import ErasureLedger, SubjectKind
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Iterator

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]
_PRED = "060_channel_persona_set_null"
_HEAD = "061_erasures"


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
    command.upgrade(_alembic_config(database_url), _HEAD)
    yield database_url
    _reset_schema(database_url)


def _scalar(database_url: str, sql: str) -> object:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            return conn.execute(text(sql)).scalar()
    finally:
        engine.dispose()


def test_the_table_is_under_forced_row_level_security(clean_db: str) -> None:
    forced = _scalar(
        clean_db,
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class WHERE relname = 'erasures'",
    )
    assert forced is True
    policies = _scalar(
        clean_db,
        "SELECT count(*) FROM pg_policies "
        "WHERE tablename = 'erasures' AND policyname = 'user_isolation'",
    )
    assert policies == 1


def test_it_has_no_foreign_key_so_it_can_outlive_what_it_records(clean_db: str) -> None:
    keys = _scalar(
        clean_db,
        "SELECT count(*) FROM pg_constraint WHERE contype = 'f' "
        "AND conrelid = 'erasures'::regclass",
    )
    assert keys == 0


def test_one_tenant_cannot_see_anothers_erasures(clean_db: str) -> None:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    admin = create_engine(clean_db)
    with admin.begin() as conn:
        # The schema was just recreated, so the role has neither until they are granted here.
        conn.execute(text("GRANT USAGE ON SCHEMA public TO persona_app"))
        conn.execute(text("GRANT SELECT, INSERT, UPDATE, DELETE ON erasures TO persona_app"))
    ErasureLedger(admin).claim(
        owner_id="user_a", subject_kind=SubjectKind.PERSONA, subject_id="p_a", captured={}
    )
    rls = make_rls_engine(app_url)
    token = current_user_id.set("user_b")
    try:
        assert ErasureLedger(rls).get(SubjectKind.PERSONA, "p_a") is None
    finally:
        current_user_id.reset(token)
        rls.dispose()
        admin.dispose()


def test_a_subject_has_one_open_row_and_may_be_erased_again_once_finished(clean_db: str) -> None:
    engine = create_engine(clean_db)
    insert = text(
        "INSERT INTO erasures (id, owner_id, subject_kind, subject_id) "
        "VALUES (:i, 'u', 'persona', 'p')"
    )
    try:
        with engine.begin() as conn:
            conn.execute(insert, {"i": "e1"})
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(insert, {"i": "e2"})
        with engine.begin() as conn:
            conn.execute(text("UPDATE erasures SET state = 'complete' WHERE id = 'e1'"))
            conn.execute(insert, {"i": "e3"})
    finally:
        engine.dispose()


@pytest.mark.parametrize(("kind", "state"), [("conversation", "pending"), ("persona", "half_done")])
def test_the_vocabulary_is_held_at_the_database(clean_db: str, kind: str, state: str) -> None:
    engine = create_engine(clean_db)
    try:
        with pytest.raises(IntegrityError), engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO erasures (id, owner_id, subject_kind, subject_id, state) "
                    "VALUES ('e', 'u', :k, 'p', :s)"
                ),
                {"k": kind, "s": state},
            )
    finally:
        engine.dispose()


def test_downgrade_removes_it_and_upgrade_returns_it(clean_db: str) -> None:
    cfg = _alembic_config(clean_db)
    command.downgrade(cfg, _PRED)
    assert _scalar(clean_db, "SELECT to_regclass('public.erasures')") is None
    command.upgrade(cfg, _HEAD)
    assert _scalar(clean_db, "SELECT to_regclass('public.erasures')") == "erasures"
