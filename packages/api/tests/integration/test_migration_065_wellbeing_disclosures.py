"""Migration ``065_wellbeing_disclosures``: owner scoped, cascading, and backfilled (R9-253).

The table is the durable fact the initiative wellbeing gate reads. ``persona_app`` holds
blanket DML on every table, so row level security is what keeps one tenant's facts from
another, for reads and for writes. The backfill gives every interaction a tagged node cites
today its fact, with the persona the interaction belongs to.

Postgres only (CI). Every engine here has a short connect timeout and a lock timeout, so a
missing or wedged database fails the test quickly instead of hanging the job.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _erase_seed import row
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Connection, Engine

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]
_PRED = "064_account_erasure"
_HEAD = "065_wellbeing_disclosures"
_FAST = {"connect_timeout": 5, "options": "-c lock_timeout=5000 -c statement_timeout=30000"}


def _engine(url: str) -> Engine:
    return create_engine(url, connect_args=_FAST)


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _reset_schema(database_url: str) -> None:
    engine = _engine(database_url)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
    finally:
        engine.dispose()


@pytest.fixture
def at_pred(database_url: str) -> Iterator[str]:
    """A database migrated to the revision before this one."""
    _reset_schema(database_url)
    command.upgrade(_alembic_config(database_url), _PRED)
    yield database_url
    _reset_schema(database_url)


@pytest.fixture
def at_head(at_pred: str) -> str:
    command.upgrade(_alembic_config(at_pred), _HEAD)
    return at_pred


def _scalar(url: str, sql: str, **params: Any) -> Any:  # noqa: ANN401 - a column value
    engine = _engine(url)
    try:
        with engine.begin() as conn:
            return conn.execute(text(sql), params).scalar()
    finally:
        engine.dispose()


def _facts(url: str) -> set[tuple[str, str, str]]:
    engine = _engine(url)
    try:
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT owner_id, persona_id, interaction_id FROM wellbeing_disclosures")
            ).all()
    finally:
        engine.dispose()
    return {(str(r[0]), str(r[1]), str(r[2])) for r in rows}


def test_the_table_is_under_forced_row_level_security_for_reads_and_writes(at_head: str) -> None:
    forced = _scalar(
        at_head,
        "SELECT relrowsecurity AND relforcerowsecurity FROM pg_class "
        "WHERE relname = 'wellbeing_disclosures'",
    )
    assert forced is True
    policy = _scalar(
        at_head,
        "SELECT qual IS NOT NULL AND with_check IS NOT NULL FROM pg_policies "
        "WHERE tablename = 'wellbeing_disclosures' AND policyname = 'user_isolation'",
    )
    assert policy is True


def test_both_parents_cascade_and_no_conversation_key(at_head: str) -> None:
    engine = _engine(at_head)
    try:
        with engine.begin() as conn:
            keys = conn.execute(
                text(
                    "SELECT confrelid::regclass::text, confdeltype FROM pg_constraint "
                    "WHERE contype = 'f' AND conrelid = 'wellbeing_disclosures'::regclass"
                )
            ).all()
    finally:
        engine.dispose()
    assert sorted((str(k[0]), str(k[1])) for k in keys) == [("personas", "c"), ("users", "c")]


def _node(
    conn: Connection, node_id: str, owner: str, category: str | None, *trail: dict[str, Any]
) -> None:
    conn.execute(
        text(
            "INSERT INTO graph_nodes (id, owner_id, node_kind, concept_name, content, metadata, "
            "wellbeing_category, embedding, embedding_model, content_hash, provenance, created_at) "
            "VALUES (:id, :o, 'fact', 'c', 'n', '{}'::jsonb, :cat, "
            "array_fill(0, ARRAY[384])::vector, 'm', :id, CAST(:trail AS jsonb), now())"
        ),
        {"id": node_id, "o": owner, "cat": category, "trail": json.dumps(list(trail))},
    )


def _entry(interaction: str | None, persona: str | None = None) -> dict[str, Any]:
    return {
        "source": "persona_self",
        "interaction_id": interaction,
        "persona_id": persona,
        "written_at": "2026-11-18T00:00:00+00:00",
    }


def test_the_backfill_records_what_tagged_nodes_cite_with_the_interactions_persona(
    at_pred: str,
) -> None:
    engine = _engine(at_pred)
    try:
        with engine.begin() as conn:
            for user in ("u_a", "u_b"):
                row(conn, "users", id=user, email=f"{user}@example.test")
            for persona, owner in (("p_a", "u_a"), ("p_a2", "u_a"), ("p_b", "u_b")):
                row(conn, "personas", id=persona, owner_id=owner, yaml="x: 1")
            row(conn, "conversations", id="conv_a", owner_id="u_a", persona_id="p_a", origin="chat")
            # Tagged, cites a live conversation: the persona is the conversation's, not the
            # (wrong) one the entry names.
            _node(conn, "n1", "u_a", "disordered_eating", _entry("conv_a", "p_a2"), _entry(None))
            # Tagged, cites a conversation already deleted: the entry's persona.
            _node(conn, "n2", "u_a", "self_harm", _entry("conv_gone", "p_a2"))
            # Tagged, cites an interaction whose persona no longer exists: nothing.
            _node(conn, "n3", "u_a", "self_harm", _entry("conv_orphan", "p_erased"))
            # Untagged sibling of a disclosure: nothing of its own.
            _node(conn, "n4", "u_a", None, _entry("conv_quiet", "p_a"))
            # Another owner's tagged node, and a duplicate citation of conv_a.
            _node(conn, "n5", "u_b", "mental_health_crisis", _entry("conv_b", "p_b"))
            _node(conn, "n6", "u_a", "self_harm", _entry("conv_a", "p_a"))
    finally:
        engine.dispose()

    command.upgrade(_alembic_config(at_pred), _HEAD)

    assert _facts(at_pred) == {
        ("u_a", "p_a", "conv_a"),
        ("u_a", "p_a2", "conv_gone"),
        ("u_b", "p_b", "conv_b"),
    }


def test_a_fact_goes_with_its_persona_and_not_with_its_conversation(at_head: str) -> None:
    engine = _engine(at_head)
    try:
        with engine.begin() as conn:
            row(conn, "users", id="u", email="u@example.test")
            row(conn, "personas", id="p", owner_id="u", yaml="x: 1")
            row(conn, "conversations", id="c", owner_id="u", persona_id="p", origin="chat")
            row(conn, "wellbeing_disclosures", owner_id="u", persona_id="p", interaction_id="c")
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM conversations WHERE id = 'c'"))
        assert _facts(at_head) == {("u", "p", "c")}, "the nodes learned from it outlive it"
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM personas WHERE id = 'p'"))
        assert _facts(at_head) == set()
    finally:
        engine.dispose()


@pytest.fixture
def app_role(at_head: str) -> Iterator[tuple[Engine, Engine]]:
    """(the superuser engine, the persona_app engine), with one fact owned by user_a."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    from persona_api.middleware.rls_context import make_rls_engine  # noqa: PLC0415

    admin = _engine(at_head)
    with admin.begin() as conn:
        conn.execute(text("GRANT USAGE ON SCHEMA public TO persona_app"))
        conn.execute(
            text("GRANT SELECT, INSERT, UPDATE, DELETE ON wellbeing_disclosures TO persona_app")
        )
        conn.execute(text("GRANT SELECT ON personas TO persona_app"))
        for user in ("user_a", "user_b"):
            row(conn, "users", id=user, email=f"{user}@example.test")
            row(conn, "personas", id=f"p_{user}", owner_id=user, yaml="x: 1")
        row(
            conn,
            "wellbeing_disclosures",
            owner_id="user_a",
            persona_id="p_user_a",
            interaction_id="conv_a",
        )
    rls = make_rls_engine(app_url)
    yield admin, rls
    rls.dispose()
    admin.dispose()


def _as(rls: Engine, user: str, sql: str) -> list[Any]:
    from persona_api.middleware.rls_context import current_user_id  # noqa: PLC0415

    token = current_user_id.set(user)
    try:
        with rls.begin() as conn:
            result = conn.execute(text(sql))
            return list(result.all()) if result.returns_rows else [result.rowcount]
    finally:
        current_user_id.reset(token)


def test_a_tenant_reads_its_own_fact_and_never_anothers(app_role: tuple[Engine, Engine]) -> None:
    _admin, rls = app_role
    select_all = "SELECT interaction_id FROM wellbeing_disclosures"
    assert [str(r[0]) for r in _as(rls, "user_a", select_all)] == ["conv_a"]
    assert _as(rls, "user_b", select_all) == []
    assert _as(rls, "", select_all) == [], "an unset owner sees nothing"


def test_a_tenant_cannot_write_or_remove_anothers_fact(app_role: tuple[Engine, Engine]) -> None:
    admin, rls = app_role
    with pytest.raises(DBAPIError):
        _as(
            rls,
            "user_b",
            "INSERT INTO wellbeing_disclosures (owner_id, persona_id, interaction_id) "
            "VALUES ('user_a', 'p_user_a', 'conv_forged')",
        )
    assert _as(rls, "user_b", "DELETE FROM wellbeing_disclosures") == [0]
    with admin.connect() as conn:
        rows = conn.execute(text("SELECT interaction_id FROM wellbeing_disclosures")).all()
    assert [str(r[0]) for r in rows] == ["conv_a"]


def test_downgrade_removes_it_and_upgrade_returns_it(at_head: str) -> None:
    cfg = _alembic_config(at_head)
    command.downgrade(cfg, _PRED)
    assert _scalar(at_head, "SELECT to_regclass('public.wellbeing_disclosures')") is None
    command.upgrade(cfg, _HEAD)
    assert (
        _scalar(at_head, "SELECT to_regclass('public.wellbeing_disclosures')::text")
        == "wellbeing_disclosures"
    )
