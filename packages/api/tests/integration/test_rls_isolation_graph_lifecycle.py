"""Adversarial RLS isolation for the K7 lifecycle tables (Spec K7, T1; crit 8).

Mirrors ``test_rls_isolation_graph.py`` for the two new tables
(``graph_node_versions`` / ``graph_consolidation_markers``): seed two tenants as
superuser, then read/write under each tenant's RLS context as the NON-SUPERUSER
``persona_app`` role and assert ZERO cross-tenant rows + fail-closed on an unset
GUC + WITH CHECK blocks a cross-tenant write. Direct ``owner_id`` policy (per user).
Skips if the ``persona_app`` role DSN (``APP_DATABASE_URL``) is unset.
"""

# ruff: noqa: ANN401, ARG001
from __future__ import annotations

import os

import pytest
from persona_api.db.engine import rls_connection
from sqlalchemy import create_engine, text

pytestmark = pytest.mark.integration

_NEW_TABLES = ("graph_node_versions", "graph_consolidation_markers")
_ZERO_VEC = "[" + ",".join(["0"] * 384) + "]"


@pytest.fixture
def app_engine(migrated_engine: object) -> object:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping graph RLS test")
    return create_engine(app_url.replace("+asyncpg", "+psycopg"))


def _seed_two_tenants(superuser_engine: object) -> None:
    with superuser_engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(
            text(
                "INSERT INTO users (id, email) VALUES "
                "('user_a','a@example.com'),('user_b','b@example.com')"
            )
        )
        for nid, owner in (("na1", "user_a"), ("nb1", "user_b")):
            conn.execute(
                text(
                    "INSERT INTO graph_nodes "
                    "(id, owner_id, node_kind, concept_name, content, metadata, embedding, "
                    " embedding_model, content_hash, provenance, created_at, updated_at) VALUES "
                    f"(:id, :owner, 'fact', 'c', 'c', '{{}}', '{_ZERO_VEC}', 'm', 'h', '[]', "
                    "now(), now())"
                ),
                {"id": nid, "owner": owner},
            )
        # A window-closed version row per tenant, tied to that tenant's node.
        for owner, nid in (("user_a", "na1"), ("user_b", "nb1")):
            conn.execute(
                text(
                    "INSERT INTO graph_node_versions "
                    "(owner_id, node_id, node_kind, concept_name, content, metadata, embedding, "
                    " embedding_model, content_hash, provenance, valid_at, invalid_at) VALUES "
                    f"(:owner, :nid, 'fact', 'c', 'prior', '{{}}', '{_ZERO_VEC}', 'm', 'h', '[]', "
                    "now() - interval '1 day', now())"
                ),
                {"owner": owner, "nid": nid},
            )
        for owner in ("user_a", "user_b"):
            conn.execute(
                text(
                    "INSERT INTO graph_consolidation_markers (owner_id, current_epoch) "
                    "VALUES (:owner, 0)"
                ),
                {"owner": owner},
            )


@pytest.mark.parametrize("table", _NEW_TABLES)
def test_k7_tables_isolated_per_tenant(
    migrated_engine: object, app_engine: object, table: str
) -> None:
    _seed_two_tenants(migrated_engine)
    with rls_connection(app_engine, "user_a") as conn:  # type: ignore[arg-type]
        owners = {r[0] for r in conn.execute(text(f"SELECT owner_id FROM {table}"))}  # noqa: S608
    assert owners == {"user_a"}, f"RLS leak on {table}: user_a saw {owners}"


@pytest.mark.parametrize("table", _NEW_TABLES)
def test_k7_tables_fail_closed_when_user_unset(
    migrated_engine: object, app_engine: object, table: str
) -> None:
    _seed_two_tenants(migrated_engine)
    with app_engine.begin() as conn:  # type: ignore[attr-defined]
        rows = conn.execute(text(f"SELECT owner_id FROM {table}")).all()  # noqa: S608
    assert rows == [], f"{table} must fail closed when app.current_user_id is unset"


def test_k7_cross_tenant_write_blocked_by_with_check(
    migrated_engine: object, app_engine: object
) -> None:
    _seed_two_tenants(migrated_engine)
    from sqlalchemy.exc import ProgrammingError

    with (
        rls_connection(app_engine, "user_a") as conn,  # type: ignore[arg-type]
        pytest.raises(ProgrammingError),
    ):
        conn.execute(
            text(
                "INSERT INTO graph_consolidation_markers (owner_id, current_epoch) "
                "VALUES ('user_b', 5)"
            )
        )
