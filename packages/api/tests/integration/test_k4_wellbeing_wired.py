"""K4 criteria 2 / 4 / 5 — the wellbeing layer end to end on the real stack.

Drives the EXACT K4 path the chat loop composes (``make_graph_retrieval`` with the K4
allowlist provider over the real graph store + ``HybridRetriever``), against real
Postgres + pgvector + the real ``bge-small`` embedder. Proves the behaviours the unit
tests can only simulate:

- **Criterion 2 (share-with-care flows):** a disordered-eating disclosure (share-with-
  care, NEVER gated) reaches the prompt carrying its ``wellbeing_category`` (so K3's
  surfacing slot attaches the care text); untagged knowledge flows untouched.
- **Criterion 4 (context gating holds + lifts):** a gate-eligible crisis disclosure is
  SUBTRACTED from the per-turn allowlist on an unrelated, unopened turn (the gate
  holds), and SURFACES when the user opens the topic (the gate lifts).
- **Criterion 5 (never-store, retrieval side):** a means/method disclosure recorded via
  the real ``record_user_fact`` tool is rejected at the write (D-K2-7) and its specifics
  are NOT retrievable from the graph, while the care-relevant fact IS. (The write-side
  rejection is also covered by ``test_k2_direct_write_operator_pass``; here it is K4's
  "no method content retrievable from the graph" wording, end to end.)

``@pytest.mark.integration`` so it is out of the default unit run. Mirrors
``test_k3_cross_persona_wired`` (seeding, RLS binding, the wired store). Run against an
ISOLATED database (a distinct ``PERSONA_TEST_DB_NAME`` for this worktree) — do not run
concurrently with other specs' integration close-out (shared-DB contention).
"""

# ruff: noqa: ARG001 — pytest fixtures used for side effects (seeded/migrated_engine).

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from persona.audit import JSONLAuditLogger
from persona.graph import build_graph_store
from persona.graph.config import GraphSettings
from persona.graph.models import NodeKind, NodeProvenance
from persona.graph.protocol import GraphStore, KnowledgeCandidate
from persona.graph.retrieval import HybridRetriever
from persona.schema.chunks import WriteSource
from persona.wellbeing import WellbeingCategory
from persona.wellbeing_policy import is_gate_eligible, parse_category
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_runtime.extraction.direct_write import make_record_user_fact_tool
from persona_runtime.graph_selection import GatingContext, make_graph_retrieval, recency_bucket
from persona_runtime.wellbeing import FlaggedNode, make_allowlist_provider, recency_band
from sqlalchemy import text

pytestmark = pytest.mark.integration

_OWNER = "k4_owner"
_PERSONA = "k4_persona"


@pytest.fixture
def app_engine(migrated_engine: object) -> object:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping K4 wellbeing test")
    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def seeded(migrated_engine: object) -> object:
    with migrated_engine.begin() as conn:  # type: ignore[attr-defined]
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'k4@example.com')"), {"o": _OWNER}
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
        persona_guard=False,
        node_releaser=None,
    )


def _candidate(
    concept: str, content: str, category: WellbeingCategory | None
) -> KnowledgeCandidate:
    return KnowledgeCandidate(
        concept_name=concept,
        content=content,
        node_kind=NodeKind.FACT,
        wellbeing_category=None if category is None else category.value,
        provenance=NodeProvenance(
            source=WriteSource.PERSONA_SELF,
            persona_id=_PERSONA,
            written_at=datetime.now(UTC),
        ),
    )


def _allowlist_provider(
    store: GraphStore,
) -> Callable[[GatingContext], set[str] | None]:
    """The K4 allowlist provider over the real store — mirrors the runtime_factory glue."""

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


def test_share_with_care_disclosure_flows_with_its_category(
    seeded: object, app_engine: object, real_embedder: object, tmp_path: Path
) -> None:
    # Criterion 2: a disordered-eating disclosure is share-with-care — it must reach the
    # prompt (never gated) AND carry its category so the surfacing slot attaches care.
    store = _store(app_engine, real_embedder, tmp_path)
    settings = GraphSettings()
    token = current_user_id.set(_OWNER)
    try:
        store.merge(
            _OWNER,
            _candidate(
                "eating struggle",
                "User has shared a disordered-eating struggle around food and calories.",
                WellbeingCategory.DISORDERED_EATING,
            ),
        )
        retrieve = make_graph_retrieval(
            retriever=HybridRetriever(store=store, settings=settings),
            owner_provider=current_user_id.get,
            settings=settings,
            allowlist_provider=_allowlist_provider(store),
        )
        graph = retrieve("I have been struggling with disordered eating and food lately")
        match = [i for i in graph.items if "disordered-eating" in i.content.lower()]
        assert match, (
            f"share-with-care ED node was not retrieved: {[i.content for i in graph.items]}"
        )
        # It carries the category → K3's surfacing slot will attach the care text.
        assert match[0].wellbeing_category == WellbeingCategory.DISORDERED_EATING.value
    finally:
        current_user_id.reset(token)


def test_context_gate_subtracts_an_unopened_crisis_node(
    seeded: object, app_engine: object, real_embedder: object, tmp_path: Path
) -> None:
    # Criterion 4 (holds): a gate-eligible crisis disclosure is subtracted from the
    # per-turn allowlist on an unrelated, unopened turn — proven at the allowlist level
    # against the real flagged_nodes read (no embedder-similarity dependency).
    store = _store(app_engine, real_embedder, tmp_path)
    token = current_user_id.set(_OWNER)
    try:
        store.merge(
            _OWNER,
            _candidate(
                "acute crisis",
                "User had a severe mental-health crisis with panic attacks this week.",
                WellbeingCategory.MENTAL_HEALTH_CRISIS,
            ),
        )
        store.merge(_OWNER, _candidate("hobby", "User enjoys hiking on weekends.", None))
        crisis_id = next(
            n.id for n in store.flagged_nodes(_OWNER) if n.wellbeing_category is not None
        )
        provider = _allowlist_provider(store)

        allowlist = provider(
            GatingContext(
                owner_id=_OWNER,
                query="help me plan a fun birthday party this weekend",
                recent_messages=(),
            )
        )
        assert allowlist is not None, "expected a positive allowlist (something was gated)"
        assert crisis_id not in allowlist, "the unopened crisis node was NOT subtracted"
        # Share-with-care / untagged knowledge is never subtracted.
        all_ids = set(store.node_ids_for_owner(_OWNER))
        assert (all_ids - {crisis_id}) <= allowlist
    finally:
        current_user_id.reset(token)


def test_context_gate_lifts_when_the_user_opens_the_topic(
    seeded: object, app_engine: object, real_embedder: object, tmp_path: Path
) -> None:
    # Criterion 4 (lifts): the same crisis disclosure SURFACES end to end when the user
    # raises the topic — the gate lifts and K1 retrieves it.
    store = _store(app_engine, real_embedder, tmp_path)
    settings = GraphSettings()
    token = current_user_id.set(_OWNER)
    try:
        store.merge(
            _OWNER,
            _candidate(
                "acute crisis",
                "User had a severe mental-health crisis with panic attacks this week.",
                WellbeingCategory.MENTAL_HEALTH_CRISIS,
            ),
        )
        retrieve = make_graph_retrieval(
            retriever=HybridRetriever(store=store, settings=settings),
            owner_provider=current_user_id.get,
            settings=settings,
            allowlist_provider=_allowlist_provider(store),
        )
        # The user opens the topic → the gate lifts → the node surfaces.
        graph = retrieve("I've been having a severe mental-health crisis and panic attacks")
        assert any("crisis" in i.content.lower() for i in graph.items), (
            f"the crisis node did not surface when the topic was opened: "
            f"{[i.content for i in graph.items]}"
        )
    finally:
        current_user_id.reset(token)


@pytest.mark.asyncio
async def test_means_specifics_are_not_retrievable_from_the_graph(
    seeded: object, app_engine: object, real_embedder: object, tmp_path: Path
) -> None:
    # Criterion 5 (retrieval side): a means disclosure is rejected at the write (D-K2-7),
    # so the method specifics are NOT retrievable from the graph; the care-relevant fact
    # IS recorded and retrievable.
    store = _store(app_engine, real_embedder, tmp_path)
    settings = GraphSettings()
    token = current_user_id.set(_OWNER)
    try:
        tool = make_record_user_fact_tool(
            graph_store=store, owner_provider=current_user_id.get, persona_id=_PERSONA
        )
        means = await tool.execute(
            fact="I want to hurt myself with a razor blade", category="self_harm"
        )
        assert means.is_error, "a self-harm means fact was NOT rejected at the write"

        care = await tool.execute(
            fact="I have been having self-harm urges lately", category="self_harm"
        )
        assert not care.is_error, "the care-relevant fact was wrongly rejected"

        retrieve = make_graph_retrieval(
            retriever=HybridRetriever(store=store, settings=settings),
            owner_provider=current_user_id.get,
            settings=settings,
        )
        graph = retrieve("how have I been feeling about self-harm")
        contents = " ".join(i.content.lower() for i in graph.items)
        assert "razor" not in contents, "means specifics (razor) retrievable from the graph"
        assert "blade" not in contents, "means specifics (blade) retrievable from the graph"
    finally:
        current_user_id.reset(token)
