"""Criterion 6 + 7 end-to-end through the K4-GATED retrieval (Spec K5, K5-R-4).

The non-negotiable trust proof: a user's DELETE and CORRECT propagate all the way to what a
persona actually retrieves — through ``make_graph_retrieval`` **with the K4 allowlist gate**
(never a bare ``HybridRetriever``; the K4 handover mandates graph retrieval routes through the
gate). Two legs, BOTH required (do not let the correction leg fall out as "the delete harness"):

- **Criterion 7 (delete):** seed → gated-retrieval surfaces it → ``delete_node`` → it is gone
  from Postgres AND the dense index AND gated-retrieval no longer surfaces it.
- **Criterion 6 (correct):** seed → gated-retrieval surfaces by the old meaning → ``correct_node``
  → gated-retrieval surfaces the NEW meaning and the old meaning is no longer retrievable (the
  corrected knowledge reaches the persona).

Mirrors ``test_k4_wellbeing_wired`` (RLS binding, the wired store, the real K4 allowlist provider)
and uses the production ``real_embedder`` so the gate's similarity floor sees real semantics. Run
against an ISOLATED test DB (shared-DB contention rule).
"""

# ruff: noqa: ARG001 — pytest fixtures used for side effects (migrated_engine/seeded).
from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.audit import JSONLAuditLogger
from persona.graph import build_graph_store
from persona.graph.config import GraphSettings
from persona.graph.models import NodeKind, NodeProvenance
from persona.graph.protocol import KnowledgeCandidate
from persona.graph.retrieval import HybridRetriever
from persona.schema.chunks import WriteSource
from persona.wellbeing_policy import is_gate_eligible, parse_category
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_runtime.graph_selection import make_graph_retrieval, recency_bucket
from persona_runtime.wellbeing import FlaggedNode, make_allowlist_provider, recency_band
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.graph.protocol import GraphStore
    from persona_runtime.graph_selection import GatingContext

pytestmark = pytest.mark.integration

_OWNER = "k5_owner"
_PERSONA = "k5_persona"


@pytest.fixture
def app_engine(migrated_engine: object) -> object:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping K5 gated-retrieval test")
    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def seeded(migrated_engine: object) -> object:
    with migrated_engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'k5@example.com')"), {"o": _OWNER}
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) "
                "VALUES (:p, :o, 'schema_version: \"1.0\"')"
            ),
            {"p": _PERSONA, "o": _OWNER},
        )
    return migrated_engine


def _store(app_engine: object, real_embedder: object, tmp_path: Path) -> GraphStore:
    return build_graph_store(
        engine=app_engine,  # type: ignore[arg-type]
        embedder=real_embedder,  # type: ignore[arg-type]
        audit_logger=JSONLAuditLogger(tmp_path / "audit"),
    )


def _candidate(concept: str, content: str) -> KnowledgeCandidate:
    """A normal (non-flagged) node — routes THROUGH the K4 gate but is never subtracted."""
    return KnowledgeCandidate(
        concept_name=concept,
        content=content,
        node_kind=NodeKind.FACT,
        provenance=NodeProvenance(
            source=WriteSource.PERSONA_SELF, persona_id=_PERSONA, written_at=datetime.now(UTC)
        ),
    )


def _allowlist_provider(store: GraphStore) -> Callable[[GatingContext], set[str] | None]:
    """The real K4 allowlist provider over the store — the runtime_factory glue."""

    def flagged(owner_id: str) -> list[FlaggedNode]:
        now = datetime.now(UTC)
        out: list[FlaggedNode] = []
        for node in store.flagged_nodes(owner_id):
            category = parse_category(node.wellbeing_category)
            if category is None or not is_gate_eligible(category):
                continue
            out.append(
                FlaggedNode(
                    node_id=node.id,
                    category=category,
                    recency=recency_band(recency_bucket(node, now)),
                    text=f"{node.concept_name} {node.content}",
                )
            )
        return out

    return make_allowlist_provider(
        flagged_nodes=flagged,
        owner_node_ids=lambda owner_id: set(store.node_ids_for_owner(owner_id)),
    )


def _gated(store: GraphStore, settings: GraphSettings) -> Callable[[str], object]:
    """``make_graph_retrieval`` WITH the K4 gate — exactly what the chat loop composes."""
    return make_graph_retrieval(
        retriever=HybridRetriever(store=store, settings=settings),
        owner_provider=current_user_id.get,
        settings=settings,
        allowlist_provider=_allowlist_provider(store),
    )


def _contents(graph: object) -> list[str]:
    return [i.content.lower() for i in graph.items]  # type: ignore[attr-defined]


def test_deletion_is_gone_from_gated_retrieval_end_to_end(
    seeded: object, app_engine: object, real_embedder: object, tmp_path: Path
) -> None:
    """Criterion 7: deleted → gone from Postgres + index + the K4-gated retrieval."""
    store = _store(app_engine, real_embedder, tmp_path)
    settings = GraphSettings()
    token = current_user_id.set(_OWNER)
    try:
        node_id = store.merge(
            _OWNER,
            _candidate(
                "zoo visit",
                "The user visited the zoo and was fascinated by the zebras and their stripes.",
            ),
        ).node_id
        surrogate = store._backend.surrogate_for(_OWNER, node_id)  # noqa: SLF001
        assert surrogate is not None
        retrieve = _gated(store, settings)

        # Surfaces through the gated path before deletion.
        before = _contents(retrieve("what does the user know about zebras at the zoo"))
        assert any("zebra" in c for c in before), before

        assert store.delete_node(_OWNER, node_id) is True

        # Gone from Postgres AND the dense index (same-path sync)…
        assert store.get_node(_OWNER, node_id) is None
        assert store._index.contains(surrogate) is False  # noqa: SLF001
        # …AND no longer surfaces from the persona's gated retrieval (criterion 7).
        after = _contents(retrieve("what does the user know about zebras at the zoo"))
        assert not any("zebra" in c for c in after), after
    finally:
        current_user_id.reset(token)


def test_correction_is_reflected_in_gated_retrieval_end_to_end(
    seeded: object, app_engine: object, real_embedder: object, tmp_path: Path
) -> None:
    """Criterion 6: corrected → the NEW meaning reaches gated retrieval, the old no longer does."""
    store = _store(app_engine, real_embedder, tmp_path)
    settings = GraphSettings()
    token = current_user_id.set(_OWNER)
    try:
        node_id = store.merge(
            _OWNER,
            _candidate("food note", "The user's favourite food is spicy Thai green curry."),
        ).node_id
        retrieve = _gated(store, settings)
        assert any("curry" in c for c in _contents(retrieve("what food does the user love")))

        store.correct_node(
            _OWNER, node_id, "The user is allergic to shellfish and avoids all seafood."
        )

        # The NEW meaning reaches the persona's gated retrieval…
        new_view = _contents(retrieve("what is the user allergic to"))
        assert any("shellfish" in c for c in new_view), new_view
        # …and the superseded meaning is no longer retrievable (the correction propagated).
        old_view = _contents(retrieve("what is the user's favourite food, the Thai curry"))
        assert not any("curry" in c for c in old_view), old_view
    finally:
        current_user_id.reset(token)
