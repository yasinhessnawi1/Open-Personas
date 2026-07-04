"""``enable_graph_writes`` composes a graph store only on a Postgres engine (Spec K5).

Regression guard for the community-on-SQLite render-500: the K0 graph schema
(``persona.graph._schema``) is Postgres-only — JSONB, pgvector ``Vector``,
``TSVECTOR`` + HNSW indexes have no SQLite equivalent (``create_all`` won't even
compile the DDL on SQLite). Composing a Postgres-typed store over a SQLite engine
left ``graph_store`` non-``None``, so the Memory route queried it and 500'd
("no such table: graph_nodes") instead of degrading gracefully.

The fix gates ``enable_graph_writes`` on the engine DIALECT (availability), not the
edition — so a self-hosted community deploy backed by Postgres still gets the graph,
while community-on-SQLite leaves ``graph_store`` ``None`` (the documented
graceful-absence shape: ``record_user_fact`` absent, Memory reads empty + nav-gated).
"""

from __future__ import annotations

from pathlib import Path

from persona_api.services.runtime_factory import RuntimeFactory
from sqlalchemy import create_engine


def _factory(engine: object, audit_root: Path) -> RuntimeFactory:
    # ``enable_graph_writes`` reads only ``engine.dialect.name``; the other
    # collaborators are never touched on the non-Postgres path, so sentinels suffice.
    return RuntimeFactory(
        rls_engine=engine,  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        tier_registry=None,  # type: ignore[arg-type]
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=audit_root,
    )


def test_enable_graph_writes_skips_sqlite_engine(tmp_path: Path) -> None:
    """A SQLite engine has no usable K0 graph ⇒ ``graph_store`` stays ``None``."""
    factory = _factory(create_engine("sqlite://"), tmp_path)
    factory.enable_graph_writes(audit_root=tmp_path)
    assert factory.graph_store is None


def test_enable_graph_writes_is_idempotent_on_sqlite(tmp_path: Path) -> None:
    """Re-calling the gate is a no-op — never composes a store on the second pass."""
    factory = _factory(create_engine("sqlite://"), tmp_path)
    factory.enable_graph_writes(audit_root=tmp_path)
    factory.enable_graph_writes(audit_root=tmp_path)
    assert factory.graph_store is None
