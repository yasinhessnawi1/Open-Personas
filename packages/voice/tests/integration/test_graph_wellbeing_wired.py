"""V13-T2 — the K4 wellbeing gate on the voice composition, end to end (real stack).

The voice-layer analog of ``api/tests/integration/test_k4_wellbeing_wired.py``:
drives the EXACT gate the voice runner composes — ``build_voice_graph_retrieval``
over the real ``build_graph_store`` + ``HybridRetriever`` — against real Postgres
+ pgvector + the real ``bge-small`` embedder. Proves the behaviour the unit tests
simulate, on the real path (the [[feedback_synthetic_harness_real_transition]]
discipline): a gate-eligible crisis disclosure is SUBTRACTED on an unrelated,
unopened voice turn (the gate holds), and SURFACES when the caller opens the topic
(the gate lifts) — and it is owner-scoped under the ``persona_app`` non-superuser
role (RLS non-vacuous, criterion 7).

``@pytest.mark.integration``: out of the default unit run. Requires a migrated
persona-pg + the ``persona_app`` role (``APP_DATABASE_URL``); skips cleanly
otherwise. Run against an ISOLATED database per worktree (shared-DB contention).
"""

from __future__ import annotations

import contextlib
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from persona.audit import JSONLAuditLogger
from persona.graph import build_graph_store
from persona.graph.models import NodeKind, NodeProvenance
from persona.graph.protocol import GraphStore, KnowledgeCandidate
from persona.schema.chunks import WriteSource
from persona.stores import SentenceTransformerEmbedder
from persona.wellbeing_policy import WellbeingCategory
from persona_voice.model.graph import build_voice_graph_retrieval
from persona_voice.session.state_machine import make_session_rls_engine
from sqlalchemy import create_engine, text

pytestmark = [pytest.mark.integration]

_BGE_MODEL = "BAAI/bge-small-en-v1.5"
_CRISIS_TEXT = "User had a severe mental-health crisis with panic attacks this week."


def _superuser_url() -> str:
    return os.environ.get(
        "DATABASE_URL",
        os.environ.get(
            "PERSONA_VOICE_TEST_DATABASE_URL",
            "postgresql+psycopg://persona:persona@localhost:5436/persona",
        ),
    )


def _app_url() -> str | None:
    return os.environ.get("APP_DATABASE_URL", os.environ.get("PERSONA_VOICE_TEST_APP_DATABASE_URL"))


@pytest.fixture(scope="module")
def real_embedder() -> SentenceTransformerEmbedder:
    return SentenceTransformerEmbedder(model_name=_BGE_MODEL, device="cpu")


@pytest.fixture
def owner_id() -> str:
    return f"vm_owner_{uuid.uuid4().hex[:8]}"


@pytest.fixture
def seeded_owner(owner_id: str) -> str:
    """Insert the owner (superuser), yield the id, clean up graph rows + the user."""
    try:
        su = create_engine(_superuser_url(), pool_size=1)
        with su.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"persona-pg not reachable: {exc}")
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, :e) ON CONFLICT (id) DO NOTHING"),
            {"o": owner_id, "e": f"{owner_id}@x.test"},
        )
    try:
        yield owner_id
    finally:
        with su.begin() as conn:
            # Best-effort per-table cleanup: the savepoint rolls back on a missing
            # table (suppress wraps begin_nested so the failed savepoint is undone,
            # not committed), then the next table proceeds.
            for tbl in ("graph_edges", "graph_nodes", "graph_entity_mentions", "graph_entities"):
                with contextlib.suppress(Exception), conn.begin_nested():
                    conn.execute(
                        text(f"DELETE FROM {tbl} WHERE owner_id = :o"),  # noqa: S608 — literal table
                        {"o": owner_id},
                    )
            conn.execute(text("DELETE FROM users WHERE id = :o"), {"o": owner_id})
        su.dispose()


def _store(owner: str, embedder: SentenceTransformerEmbedder, tmp_path: Path) -> GraphStore:
    app_url = _app_url()
    if app_url is None:
        pytest.skip("APP_DATABASE_URL not set — the persona_app non-superuser DSN is required.")
    try:
        engine = make_session_rls_engine(app_url, user_id=owner)
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"persona_app role not reachable: {exc}")
    return build_graph_store(
        engine=engine,  # type: ignore[arg-type]
        embedder=embedder,  # type: ignore[arg-type]
        audit_logger=JSONLAuditLogger(tmp_path / "audit"),
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
            source=WriteSource.SYSTEM,
            persona_id="vm_persona",
            written_at=datetime.now(UTC),
        ),
    )


def test_voice_composition_subtracts_an_unopened_crisis_node(
    seeded_owner: str, real_embedder: SentenceTransformerEmbedder, tmp_path: Path
) -> None:
    # The gate HOLDS on the voice path: an ACUTE gate-eligible crisis node the caller
    # has not opened is withheld from the voice turn's graph context.
    store = _store(seeded_owner, real_embedder, tmp_path)
    store.merge(
        seeded_owner,
        _candidate("acute crisis", _CRISIS_TEXT, WellbeingCategory.MENTAL_HEALTH_CRISIS),
    )
    store.merge(seeded_owner, _candidate("hobby", "User enjoys hiking on weekends.", None))

    comp = build_voice_graph_retrieval(store, owner_id=seeded_owner)
    graph = comp.retrieval("help me plan a fun birthday party this weekend")
    contents = " ".join(item.content.lower() for item in graph.items)
    assert "panic attacks" not in contents, "the unopened crisis node must be subtracted on voice"


def test_voice_composition_lifts_when_the_caller_opens_the_topic(
    seeded_owner: str, real_embedder: SentenceTransformerEmbedder, tmp_path: Path
) -> None:
    # The gate LIFTS on the voice path: when the caller opens the topic, the crisis
    # node surfaces (so the persona can respond with care, K4-D-5).
    store = _store(seeded_owner, real_embedder, tmp_path)
    store.merge(
        seeded_owner,
        _candidate("acute crisis", _CRISIS_TEXT, WellbeingCategory.MENTAL_HEALTH_CRISIS),
    )

    comp = build_voice_graph_retrieval(store, owner_id=seeded_owner)
    graph = comp.retrieval("I've been having a severe mental-health crisis and panic attacks")
    contents = " ".join(item.content.lower() for item in graph.items)
    assert "crisis" in contents, "the crisis node must surface when the caller opens the topic"


def test_voice_graph_is_owner_scoped_under_rls(
    seeded_owner: str, real_embedder: SentenceTransformerEmbedder, tmp_path: Path
) -> None:
    # RLS non-vacuity (criterion 7): a DIFFERENT owner's composition sees none of
    # this owner's graph — the persona_app role scopes every read to the caller.
    store = _store(seeded_owner, real_embedder, tmp_path)
    store.merge(seeded_owner, _candidate("hobby", "User enjoys hiking on weekends.", None))

    other = f"vm_other_{uuid.uuid4().hex[:8]}"
    other_store = _store(other, real_embedder, tmp_path)
    comp = build_voice_graph_retrieval(other_store, owner_id=other)
    graph = comp.retrieval("what does the user enjoy on weekends")
    contents = " ".join(item.content.lower() for item in graph.items)
    assert "hiking" not in contents, "a different owner must not see this owner's graph (RLS)"
