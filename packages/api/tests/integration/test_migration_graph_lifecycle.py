"""Migration 030 (graph lifecycle) up/down reversibility (Spec K7, T1 / K7-D-X-migration).

A clean ``alembic upgrade head`` must build the K7 additions — the two new tables
(``graph_node_versions`` / ``graph_consolidation_markers``) with RLS, the
``graph_nodes`` lifecycle columns, and the ``graph_edges`` ``edge_key`` PK + window
columns + partial-unique open-edge index. Downgrading just 030 → 029 must drop ONLY
the K7 additions, leaving the K0 graph tables intact and the edge PK restored to
``id``. Mirrors ``test_migration_graph.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

pytestmark = pytest.mark.integration

_API_DIR = Path(__file__).resolve().parents[2]  # packages/api
_NEW_TABLES = {"graph_node_versions", "graph_consolidation_markers"}
_NODE_COLS = {"salience", "last_evidence_epoch", "merged_into", "updated_at"}
_EDGE_COLS = {"edge_key", "valid_at", "invalid_at", "invalidated_by", "invalidated_at"}


def _alembic_config(database_url: str) -> Config:
    cfg = Config(str(_API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", database_url)
    return cfg


def _grant_persona_app(database_url: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as conn:
            if conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'persona_app'")).first():
                conn.execute(text("GRANT USAGE ON SCHEMA public TO persona_app"))
                conn.execute(
                    text(
                        "GRANT SELECT, INSERT, UPDATE, DELETE "
                        "ON ALL TABLES IN SCHEMA public TO persona_app"
                    )
                )
    finally:
        engine.dispose()


def test_migration_030_builds_k7_additions(database_url: str) -> None:
    engine = create_engine(database_url)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()

    command.upgrade(_alembic_config(database_url), "head")

    engine = create_engine(database_url)
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    assert _NEW_TABLES.issubset(tables)

    node_cols = {c["name"] for c in insp.get_columns("graph_nodes")}
    assert _NODE_COLS.issubset(node_cols)
    edge_cols = {c["name"] for c in insp.get_columns("graph_edges")}
    assert _EDGE_COLS.issubset(edge_cols)

    # edge PK is edge_key (surrogate), not id.
    edge_pk = set(insp.get_pk_constraint("graph_edges")["constrained_columns"])
    assert edge_pk == {"edge_key"}

    # partial-unique open-edge index + K7 node index present.
    edge_idx = {i["name"] for i in insp.get_indexes("graph_edges")}
    assert "uq_graph_edges_open_id" in edge_idx
    node_idx = {i["name"] for i in insp.get_indexes("graph_nodes")}
    assert "ix_graph_nodes_updated_at" in node_idx

    with engine.connect() as conn:
        forced = {
            r[0]
            for r in conn.execute(
                text("SELECT relname FROM pg_class WHERE relrowsecurity AND relforcerowsecurity")
            )
        }
        policied = {
            r[0]
            for r in conn.execute(
                text("SELECT tablename FROM pg_policies WHERE policyname = 'user_isolation'")
            )
        }
    assert forced >= _NEW_TABLES
    assert policied >= _NEW_TABLES
    engine.dispose()
    _grant_persona_app(database_url)


def test_migration_030_downgrade_drops_only_k7(database_url: str) -> None:
    cfg = _alembic_config(database_url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "032_dow_caps")

    engine = create_engine(database_url)
    insp = inspect(engine)
    tables = set(insp.get_table_names())
    # K7 tables gone; K0 graph tables survive.
    assert tables.isdisjoint(_NEW_TABLES)
    assert {"graph_nodes", "graph_edges", "graph_entities"}.issubset(tables)

    node_cols = {c["name"] for c in insp.get_columns("graph_nodes")}
    assert node_cols.isdisjoint(_NODE_COLS)
    edge_cols = {c["name"] for c in insp.get_columns("graph_edges")}
    assert edge_cols.isdisjoint(_EDGE_COLS)
    # edge PK restored to id.
    assert set(insp.get_pk_constraint("graph_edges")["constrained_columns"]) == {"id"}
    engine.dispose()

    command.upgrade(cfg, "head")  # leave migrated for later session tests
    _grant_persona_app(database_url)
