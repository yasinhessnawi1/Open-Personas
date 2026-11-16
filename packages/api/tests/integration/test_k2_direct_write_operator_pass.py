"""K2 operator pass — the model-callable ``record_user_fact`` tool, LIVE (Spec K2, §5.3).

Runtime-level live pass (real Postgres + the real ``PostgresGraphStore`` + a
deterministic embedder) for the direct-write tool, which is NOT operator-exempt (it
is a model-callable surface). Synthesis is already covered by the green wired-tier
eval + the wired-pipeline integration proof; this leg adds the direct path:

  1. a persona records an explicit fact mid-conversation → it LANDS in the graph,
     owner-scoped (RLS holds), ``source=persona_self``;
  2. another persona of the SAME owner RETRIEVES it (the shared-brain loop);
  3. a self-harm-means fact is REJECTED before the write (the means backstop, D-K2-7).

Run with ``-s`` and capture stdout into
``docs/specs/phase3/spec_K2/evidence/operator_pass_<date>.log``.
"""

# ruff: noqa: T201, S101, INP001, ARG001, ARG002, E501 — operator-pass driver: prints are the evidence log; fixture-ordering args.
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.audit import JSONLAuditLogger
from persona.graph import build_graph_store
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_runtime.extraction.direct_write import make_record_user_fact_tool
from sqlalchemy import text

if TYPE_CHECKING:
    from pathlib import Path

    from persona.stores.embedder import Embedder
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "k2_op_owner"


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:
    import os

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping K2 operator pass")
    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'k2op@example.com')"), {"o": _OWNER}
        )
        for pid in ("persona_a", "persona_b"):
            conn.execute(
                text(
                    "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'schema_version: 1.0')"
                ),
                {"p": pid, "o": _OWNER},
            )
    return migrated_engine


def _node_count(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM graph_nodes WHERE owner_id = :o"), {"o": _OWNER}
            ).scalar_one()
        )


@pytest.mark.asyncio
async def test_record_user_fact_operator_pass(
    seeded: Engine, app_engine: Engine, embedder: Embedder, tmp_path: Path
) -> None:
    graph_store = build_graph_store(
        engine=app_engine,
        embedder=embedder,
        audit_logger=JSONLAuditLogger(tmp_path / "audit"),
        persona_guard=False,
    )
    # Bind the owner's RLS scope per the production worker/request wiring.
    token = current_user_id.set(_OWNER)
    try:
        print("\n=== K2 OPERATOR PASS — record_user_fact (LIVE: real Postgres + graph) ===")
        print(f"owner={_OWNER}  graph_nodes(before)={_node_count(app_engine)}")

        # --- Scenario 1: persona_a records an explicit durable fact ---
        tool_a = make_record_user_fact_tool(
            graph_store=graph_store, owner_provider=lambda: _OWNER, persona_id="persona_a"
        )
        res = await tool_a.execute(fact="I'm allergic to shellfish")
        with app_engine.begin() as conn:
            row = conn.execute(
                text("SELECT content, node_kind FROM graph_nodes WHERE owner_id = :o"),
                {"o": _OWNER},
            ).one()
        ok1 = (not res.is_error) and _node_count(app_engine) == 1 and "shellfish" in row.content
        print(
            f"[1] persona_a record_user_fact('allergic to shellfish') → is_error={res.is_error}; "
            f"graph node: content={row.content!r} kind={row.node_kind} → {'PASS' if ok1 else 'FAIL'}"
        )
        assert ok1

        # --- Scenario 2: persona_b retrieves it (shared-brain loop, owner-scoped) ---
        hits = graph_store.search_dense(_OWNER, "food allergies", 5)
        ok2 = any("shellfish" in h.content for h in hits)
        print(
            f"[2] persona_b retrieves 'food allergies' → {len(hits)} hit(s); "
            f"shellfish surfaced={ok2} → {'PASS' if ok2 else 'FAIL'}"
        )
        assert ok2

        # --- Scenario 3: self-harm means is REJECTED before the write (D-K2-7) ---
        before = _node_count(app_engine)
        res3 = await tool_a.execute(
            fact="I keep thinking about taking all my pills at once", category="self_harm"
        )
        ok3 = res3.is_error and _node_count(app_engine) == before
        print(
            f"[3] record_user_fact(self-harm MEANS) → is_error={res3.is_error}; "
            f"graph nodes unchanged={_node_count(app_engine) == before} (means NEVER stored) "
            f"→ {'PASS' if ok3 else 'FAIL'}"
        )
        assert ok3

        # control: the means-free struggle IS recordable (the backstop is not blanket)
        res4 = await tool_a.execute(
            fact="I have been having a really hard time lately", category="self_harm"
        )
        ok4 = (not res4.is_error) and _node_count(app_engine) == before + 1
        print(
            f"[3b] means-free disclosure recordable → is_error={res4.is_error} → {'PASS' if ok4 else 'FAIL'}"
        )
        assert ok4

        print(
            "=== RESULT: 4/4 PASS — record_user_fact lands owner-scoped, retrievable, means-rejected ==="
        )
    finally:
        current_user_id.reset(token)
