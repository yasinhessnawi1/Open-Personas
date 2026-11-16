"""K3 criterion 1 — the cross-persona loop, end to end on the real stack.

Knowledge written via persona A (K2 ``merge``) is retrieved (K1 ``HybridRetriever``)
and USED by persona B — both under the SAME owner — through the exact K3 path the
chat loop runs: ``make_graph_retrieval`` (owner-scoped) → ``select_graph_knowledge``
→ ``PromptBuilder``. This is the direction-3 thesis proven through the prompt: a
persona quietly adapting to something the user told a *different* persona.

Real Postgres + pgvector + the real graph store; ``@pytest.mark.integration`` so
it is out of the default unit run. Mirrors ``test_synthesis_pipeline_wired`` +
``test_k2_direct_write_operator_pass`` (seeding, RLS binding, the wired store).
"""

# ruff: noqa: ARG001 — pytest fixtures used for side effects (seeded/migrated_engine).

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import pytest
from persona.audit import JSONLAuditLogger
from persona.graph import build_graph_store
from persona.graph.config import GraphSettings
from persona.graph.models import NodeKind, NodeProvenance
from persona.graph.protocol import KnowledgeCandidate
from persona.graph.retrieval import HybridRetriever
from persona.schema.chunks import WriteSource
from persona.schema.persona import Persona, PersonaIdentity
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_runtime.graph_selection import make_graph_retrieval
from persona_runtime.prompt import PromptBuilder, RetrievedContext
from sqlalchemy import text

pytestmark = pytest.mark.integration

_OWNER = "k3_owner"
_PERSONA_A = "persona_a"
_PERSONA_B = "persona_b"


@pytest.fixture
def app_engine(migrated_engine: object) -> object:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping K3 cross-persona test")
    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def seeded(migrated_engine: object) -> object:
    with migrated_engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'k3@example.com')"), {"o": _OWNER}
        )
        for pid in (_PERSONA_A, _PERSONA_B):
            conn.execute(
                text(
                    "INSERT INTO personas (id, owner_id, yaml) "
                    "VALUES (:p, :o, 'schema_version: \"1.0\"')"
                ),
                {"p": pid, "o": _OWNER},
            )
    return migrated_engine


def _persona_b() -> Persona:
    return Persona(
        persona_id=_PERSONA_B,
        identity=PersonaIdentity(name="Bea", role="planning assistant", background="."),
        tools=[],
    )


def test_cross_persona_knowledge_reaches_persona_b_prompt(
    seeded: object, app_engine: object, real_embedder: object, tmp_path: Path
) -> None:
    graph_store = build_graph_store(
        engine=app_engine,  # type: ignore[arg-type]
        embedder=real_embedder,  # type: ignore[arg-type]
        audit_logger=JSONLAuditLogger(tmp_path / "audit"),
        persona_guard=False,
        node_releaser=None,
    )
    settings = GraphSettings()
    retriever = HybridRetriever(store=graph_store, settings=settings)

    token = current_user_id.set(_OWNER)
    try:
        # --- persona A writes a known fact (K2 merge) ---
        candidate = KnowledgeCandidate(
            concept_name="vegetarian diet",
            content="Eats a vegetarian diet and avoids all meat and fish.",
            node_kind=NodeKind.PREFERENCE,
            provenance=NodeProvenance(
                source=WriteSource.PERSONA_SELF,
                persona_id=_PERSONA_A,
                written_at=datetime.now(UTC),
                grounding="user stated",
            ),
        )
        graph_store.merge(_OWNER, candidate)

        # --- persona B retrieves + uses it via the exact K3 chat path ---
        graph_retrieval = make_graph_retrieval(
            retriever=retriever,
            owner_provider=current_user_id.get,
            settings=settings,
        )
        # A diet-relevant turn whose REAL bge-small cosine against the vegetarian
        # fact (~0.71) clears ``inject_similarity_floor`` (0.66). It is semantic,
        # not a keyword copy: it names no word in the stored fact ("vegetarian",
        # "meat", "fish") — the model bridges "dietary restrictions / avoid foods"
        # to "vegetarian / avoids meat and fish". The terse "what should I cook
        # for dinner?" scores only ~0.49 (below the floor) under the real
        # embedder, which is why the original assertion was unreachable; it passed
        # review only because the hash embedder (cosine ≈ 0 for any pair) was never
        # run against this Postgres-gated test.
        query = "Given my dietary restrictions, what foods should I avoid eating?"
        graph = graph_retrieval(query)
        # The fact persona A wrote is retrieved for persona B's relevant turn.
        assert any("vegetarian" in item.content.lower() for item in graph.items), (
            f"persona A's fact not retrieved for persona B: {[i.content for i in graph.items]}"
        )

        # And it reaches persona B's assembled prompt (the shared brain felt).
        messages = PromptBuilder().build(
            _persona_b(),
            RetrievedContext(graph=graph),
            history=[],
            skill_index="",
            user_message=query,
            max_tokens=8000,
        )
        system = messages[0].content
        assert "What you already know about this person:" in system
        assert "vegetarian" in system.lower()
    finally:
        current_user_id.reset(token)


def test_irrelevant_turn_injects_nothing_for_persona_b(
    seeded: object, app_engine: object, real_embedder: object, tmp_path: Path
) -> None:
    graph_store = build_graph_store(
        engine=app_engine,  # type: ignore[arg-type]
        embedder=real_embedder,  # type: ignore[arg-type]
        audit_logger=JSONLAuditLogger(tmp_path / "audit"),
        persona_guard=False,
        node_releaser=None,
    )
    settings = GraphSettings()
    retriever = HybridRetriever(store=graph_store, settings=settings)

    token = current_user_id.set(_OWNER)
    try:
        graph_store.merge(
            _OWNER,
            KnowledgeCandidate(
                concept_name="works night shifts",
                content="Works as a nurse on rotating night shifts.",
                node_kind=NodeKind.FACT,
                provenance=NodeProvenance(
                    source=WriteSource.PERSONA_SELF,
                    persona_id=_PERSONA_A,
                    written_at=datetime.now(UTC),
                ),
            ),
        )
        graph_retrieval = make_graph_retrieval(
            retriever=retriever, owner_provider=current_user_id.get, settings=settings
        )
        # Small talk: the relevance gate injects nothing (criterion 3, on the real stack).
        graph = graph_retrieval("what's the weather like today?")
        assert graph.items == ()
    finally:
        current_user_id.reset(token)
