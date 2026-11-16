"""Migration 063 reads every place the graph names a persona, and only those (review HIGH-2).

The backfill must name the persona everywhere the sweep's graph step looks for it, or a persona
named only there would never get its erasure row; and the persona id must keep its minted shape,
since the step's own guards key on it. The behaviour needs Postgres and runs in CI
(tests/integration/test_erase_graph_backfill_postgres.py); this reads the SQL.
"""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest
from persona_api.workspace_paths import PERSONA_ID_PATTERN

_MIGRATION = (
    Path(__file__).resolve().parents[3] / "alembic" / "versions" / "063_graph_erasure_backfill.py"
)


def _module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("backfill_063", _MIGRATION)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_it_follows_the_erasure_migration() -> None:
    module = _module()
    assert module.revision == "063_graph_erasure_backfill"
    assert module.down_revision == "062_memory_owner_guard"


def test_it_reads_every_column_the_graph_step_reads_a_persona_from() -> None:
    sql = _module().BACKFILL
    read = set(re.findall(r"\b([a-z]\.(?:provenance|aliases))\b", sql))
    tables = set(re.findall(r"FROM (graph_\w+) [a-z]\b", sql))

    assert read == {"n.provenance", "v.provenance", "g.provenance", "x.provenance", "x.aliases"}
    assert tables == {"graph_nodes", "graph_node_versions", "graph_edges", "graph_entities"}


def test_it_records_only_minted_personas_that_are_gone_and_not_yet_erased() -> None:
    sql = _module().BACKFILL
    assert f"~ '{PERSONA_ID_PATTERN}'" in sql
    assert "NOT EXISTS (SELECT 1 FROM personas p WHERE p.id = named.persona_id)" in sql
    assert "r.owner_id = named.owner_id" in sql, "per owner: the backstop looks per owner"
    assert "ON CONFLICT (id) DO NOTHING" in sql
    assert "'complete'" in sql, "complete, so the backstop (not the resume) acts on it"


def test_a_role_that_rls_would_blind_is_refused_before_anything_is_inserted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Round 5 LOW-4: under RLS a plain role sees no other owner's rows and would insert nothing."""
    module = _module()
    ran: list[str] = []
    bind = type("Bind", (), {"dialect": type("D", (), {"name": "postgresql"})()})()
    monkeypatch.setattr(module.op, "get_bind", lambda: bind)
    monkeypatch.setattr(module.op, "execute", ran.append)

    module.upgrade()

    assert ran == [module.ROLE_CHECK, module.BACKFILL], "the check runs first"
    check = module.ROLE_CHECK
    assert "rolsuper OR rolbypassrls" in check
    assert "RAISE EXCEPTION" in check
    assert "%" not in check, "no format placeholder for the driver to rewrite"
    for table in ("graph_nodes", "graph_node_versions", "graph_edges", "graph_entities"):
        assert f"'{table}'" in check
    assert "'personas'" in check
    assert "'erasures'" in check


def test_the_role_check_counts_rls_that_applies_to_the_running_role() -> None:
    """Reviewer's M1: RLS binds the role when it is forced, or when the role is not the owner."""
    check = _module().ROLE_CHECK
    assert "AND c.relrowsecurity" in check
    assert "AND (c.relforcerowsecurity OR pg_get_userbyid(c.relowner) <> current_user)" in check
    assert "ns.nspname = current_schema()" in check
