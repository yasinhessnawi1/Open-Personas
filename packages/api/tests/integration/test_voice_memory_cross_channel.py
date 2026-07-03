"""V13-T6 — cross-channel memory, both directions, on the REAL chain (Spec V13 closeout).

The end-to-end proofs the spec exists for, each driven through the real trigger chain
(no hand-forced step — [[feedback_synthetic_harness_real_transition]]):

1. **Said-on-a-call → known-in-chat.** A voice transcript → the real voice enqueue
   writer → the REAL A0 ``Worker.run_once()`` → a minted graph node → surfaced by the
   **chat** retrieval composition (the ``runtime_factory._build_graph_retrieval`` shape:
   ``make_graph_retrieval`` over the real store + ``HybridRetriever`` + the K4 allowlist,
   owner-scoped by ``current_user_id``) — a gated retrieval callable, never a raw store read.
2. **Voice recall reads chat-era memory.** A chat-minted node (real chat enqueue + worker)
   → surfaced by the **voice** retrieval composition (``build_voice_graph_retrieval``).
3. **Never-SELF under the real pipeline.** After a full voice-synthesis batch runs, the
   K6 SELF node is byte-unchanged (dense_query excludes it, so merge cannot touch it).
4. **RLS non-vacuity.** Two tenants each have voice-minted memory; each retrieval
   direction sees only its own — cross-tenant reads are empty, not merely owner-scoped-by-luck.

Home: the api integration suite (the real ``Worker`` + fixtures live here); it imports the
voice writer + voice retrieval to drive the cross-layer seams. ``@pytest.mark.integration``.
"""

# ruff: noqa: ARG001, ARG002 — fixture-ordering param + fakes ignore protocol args.
from __future__ import annotations

import asyncio
import contextlib
import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import JSONLAuditLogger
from persona.backends.types import ChatResponse, TokenUsage
from persona.graph import PostgresEntityRegistry, build_graph_store
from persona.graph.config import GraphSettings
from persona.graph.postgres import PostgresGraphBackend
from persona.graph.protocol import GraphStore
from persona.graph.retrieval import HybridRetriever
from persona.jobs import CHANNEL_CHAT, JobRegistry
from persona.wellbeing_policy import is_gate_eligible, parse_category
from persona_api.jobs import JobQueue, Worker
from persona_api.jobs.handlers.synthesis import (
    PgSynthesisRepository,
    enqueue_synthesis,
    register_synthesis_handler,
)
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_runtime.graph_selection import make_graph_retrieval, recency_bucket
from persona_runtime.graph_window import get_recent_window
from persona_runtime.wellbeing import FlaggedNode, make_allowlist_provider, recency_band
from persona_voice.model.graph import build_voice_graph_retrieval
from persona_voice.session.synthesis_enqueue import enqueue_voice_synthesis
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from persona.stores.embedder import Embedder
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration


class _EchoBackend:
    """Mints a grounded candidate keyed to a phrase in the transcript, so different
    tenants/interactions produce DISTINGUISHABLE facts (RLS + cross-channel need that)."""

    # Keywords chosen to be ABSENT from the extraction prompt's few-shot examples
    # (which contain "vegetarian") — otherwise the phrase match fires on the prompt,
    # not the transcript, and every tenant mints the same fact.
    _FACTS = {
        "hiking": ("hiking", "enjoys hiking on weekends", "I love hiking on weekends"),
        "climbing": ("climbing", "does rock climbing", "I started rock climbing"),
    }

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-haiku-4-5-20251001"

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: object, **kwargs: Any) -> ChatResponse:  # noqa: ANN401
        blob = str(messages)
        concept, contentf, span = next(
            (f for kw, f in self._FACTS.items() if kw in blob),
            ("misc", "said something", "something"),
        )
        content = (
            f'{{"candidates": [{{"concept_name": "{concept}", "content": "{contentf}",'
            f' "node_kind": "preference", "evidence_span": "{span}"}}]}}'
        )
        return ChatResponse(
            content=content,
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            model=self.model_name,
            provider=self.provider_name,
            latency_ms=0.0,
        )

    def chat_stream(self, *a: Any, **k: Any) -> Any:  # noqa: ANN401
        raise NotImplementedError


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping cross-channel test")
    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture(scope="module")
def embedder() -> Embedder:
    """Override the conftest HashEmbedder384 with the REAL bge embedder: cross-channel
    retrieval must match a natural query to a stored fact SEMANTICALLY (production
    behaviour), which a non-semantic hash embedding cannot do. Module-scoped so the
    model loads once for the suite."""
    from persona.stores import SentenceTransformerEmbedder

    return SentenceTransformerEmbedder(model_name="BAAI/bge-small-en-v1.5", device="cpu")


def _seed_user(engine: Engine, owner: str, persona: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, :e) ON CONFLICT (id) DO NOTHING"),
            {"o": owner, "e": f"{owner}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) "
                "VALUES (:p, :o, 'schema_version: \"1.0\"') ON CONFLICT (id) DO NOTHING"
            ),
            {"p": persona, "o": owner},
        )


def _seed_conversation(
    engine: Engine, *, owner: str, persona: str, convo: str, user_text: str
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, compacted_summary) "
                "VALUES (:c, :o, :p, '') ON CONFLICT (id) DO NOTHING"
            ),
            {"c": convo, "o": owner, "p": persona},
        )
        for role, content in (("user", user_text), ("assistant", "Noted.")):
            conn.execute(
                text("INSERT INTO messages (conversation_id, role, content) VALUES (:c, :r, :t)"),
                {"c": convo, "r": role, "t": content},
            )


def _build_worker(
    *, dispatch_engine: Engine, app_engine: Engine, embedder: Embedder, audit_root: Path
) -> Worker:
    graph_backend = PostgresGraphBackend(engine=app_engine)
    graph_store = build_graph_store(
        engine=app_engine, embedder=embedder, audit_logger=JSONLAuditLogger(audit_root)
    )
    entity_registry = PostgresEntityRegistry(backend=graph_backend, embedder=embedder)
    from persona_runtime.extraction.synthesizer import build_synthesizer

    synthesizer = build_synthesizer(
        graph_store=graph_store, registry=entity_registry, backend=_EchoBackend()
    )
    registry = JobRegistry()
    register_synthesis_handler(registry, runner=synthesizer, repository=PgSynthesisRepository())
    return Worker(
        dispatch_engine=dispatch_engine, rls_engine=app_engine, registry=registry, worker_id="w-xc"
    )


def _drain(worker: Worker, *, limit: int = 5) -> int:
    processed = 0
    for _ in range(limit):
        n = asyncio.run(worker.run_once())
        if n == 0:
            break
        processed += n
    return processed


def _chat_retrieval(store: GraphStore) -> Callable[[str], object]:
    """The chat retrieval composition — the ``_build_graph_retrieval`` shape (full
    profile, ``current_user_id`` owner provider, the K4 allowlist)."""
    settings = GraphSettings()
    retriever = HybridRetriever(store=store, settings=settings)

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

    return make_graph_retrieval(
        retriever=retriever,
        owner_provider=current_user_id.get,
        settings=settings,
        allowlist_provider=make_allowlist_provider(
            flagged_nodes=flagged,
            owner_node_ids=lambda o: set(store.node_ids_for_owner(o)),
        ),
        recent_window_provider=get_recent_window,
    )


@contextlib.contextmanager
def _as_owner(owner: str) -> Iterator[None]:
    token = current_user_id.set(owner)
    try:
        yield
    finally:
        current_user_id.reset(token)


def _retrieval_store(app_engine: Engine, embedder: Embedder, audit_root: Path) -> GraphStore:
    return build_graph_store(
        engine=app_engine, embedder=embedder, audit_logger=JSONLAuditLogger(audit_root)
    )


def test_said_on_a_call_is_known_in_chat(
    migrated_engine: Engine, app_engine: Engine, embedder: Embedder, tmp_path: Path
) -> None:
    owner, persona, convo = "xc_a", "xc_a_p", "xc_a_call"
    _seed_user(migrated_engine, owner, persona)
    _seed_conversation(
        migrated_engine,
        owner=owner,
        persona=persona,
        convo=convo,
        user_text="I love hiking on weekends",
    )
    # The REAL voice enqueue writer + the REAL worker (no hand-forced mint).
    enqueue_voice_synthesis(
        migrated_engine, owner_id=owner, conversation_id=convo, persona_id=persona, message_count=2
    )
    worker = _build_worker(
        dispatch_engine=migrated_engine,
        app_engine=app_engine,
        embedder=embedder,
        audit_root=tmp_path / "a",
    )
    assert _drain(worker) == 1

    # Surfaced by the CHAT retrieval composition (natural query, chat floor) in an
    # owner-scoped read.
    retrieve = _chat_retrieval(_retrieval_store(app_engine, embedder, tmp_path / "r"))
    with _as_owner(owner):
        graph = retrieve("does the user enjoy hiking on weekends")
    contents = " ".join(i.content.lower() for i in graph.items)  # type: ignore[attr-defined]
    assert "hiking" in contents, "a fact said on a call must be known in chat"

    # And the minted node is attributable to voice.
    with migrated_engine.begin() as conn:
        prov = conn.execute(
            text("SELECT provenance::text FROM graph_nodes WHERE owner_id = :o"), {"o": owner}
        ).scalar_one()
    assert "voice" in prov


def test_voice_recall_reads_chat_era_memory(
    migrated_engine: Engine, app_engine: Engine, embedder: Embedder, tmp_path: Path
) -> None:
    owner, persona, convo = "xc_b", "xc_b_p", "xc_b_chat"
    _seed_user(migrated_engine, owner, persona)
    _seed_conversation(
        migrated_engine,
        owner=owner,
        persona=persona,
        convo=convo,
        user_text="I started rock climbing",
    )
    # A CHAT-minted memory through the real chat enqueue + worker.
    enqueue_synthesis(
        JobQueue(migrated_engine),
        owner_id=owner,
        interaction_kind="conversation",
        interaction_id=convo,
        persona_id=persona,
        message_count=2,
        channel=CHANNEL_CHAT,
    )
    worker = _build_worker(
        dispatch_engine=migrated_engine,
        app_engine=app_engine,
        embedder=embedder,
        audit_root=tmp_path / "a",
    )
    assert _drain(worker) == 1

    # Read it back through the VOICE retrieval composition.
    voice = build_voice_graph_retrieval(
        _retrieval_store(app_engine, embedder, tmp_path / "r"), owner_id=owner
    )
    # The voice profile floor is stricter (0.72 vs chat's 0.66, D-3 "fewer, surer
    # nodes"), so the query closely tracks the stored fact — the claim under test is
    # that voice retrieval SURFACES a chat-era memory, not NLU breadth.
    with _as_owner(owner):
        graph = voice.retrieval("does rock climbing")
    contents = " ".join(i.content.lower() for i in graph.items)
    assert "climbing" in contents, "voice recall must read a chat-era memory"


def test_self_node_is_byte_unchanged_after_a_voice_synthesis_batch(
    migrated_engine: Engine, app_engine: Engine, embedder: Embedder, tmp_path: Path
) -> None:
    owner, persona, convo = "xc_self", "xc_self_p", "xc_self_call"
    _seed_user(migrated_engine, owner, persona)
    # Create the K6 SELF node (its own path), snapshot it.
    store = _retrieval_store(app_engine, embedder, tmp_path / "s")
    with _as_owner(owner):
        store.get_or_create_self_node(owner, display_name="Alex")
        before = store.get_self_node(owner)
    assert before is not None

    _seed_conversation(
        migrated_engine,
        owner=owner,
        persona=persona,
        convo=convo,
        user_text="I love hiking on weekends",
    )
    enqueue_voice_synthesis(
        migrated_engine, owner_id=owner, conversation_id=convo, persona_id=persona, message_count=2
    )
    worker = _build_worker(
        dispatch_engine=migrated_engine,
        app_engine=app_engine,
        embedder=embedder,
        audit_root=tmp_path / "a",
    )
    assert _drain(worker) == 1

    with _as_owner(owner):
        after = store.get_self_node(owner)
    assert after is not None
    # Byte-unchanged: id, kind, content, and the provenance trail are untouched — the
    # voice synthesis batch created a separate ``::node::`` fact, never the SELF node.
    assert after.id == before.id
    assert after.node_kind == before.node_kind
    assert after.content == before.content
    assert after.model_dump() == before.model_dump()


def test_cross_tenant_voice_memory_is_owner_scoped(
    migrated_engine: Engine, app_engine: Engine, embedder: Embedder, tmp_path: Path
) -> None:
    a, ap, ac = "xc_t_a", "xc_t_a_p", "xc_t_a_call"
    b, bp, bc = "xc_t_b", "xc_t_b_p", "xc_t_b_call"
    _seed_user(migrated_engine, a, ap)
    _seed_user(migrated_engine, b, bp)
    _seed_conversation(
        migrated_engine, owner=a, persona=ap, convo=ac, user_text="I love hiking on weekends"
    )
    _seed_conversation(
        migrated_engine, owner=b, persona=bp, convo=bc, user_text="I started rock climbing"
    )
    enqueue_voice_synthesis(
        migrated_engine, owner_id=a, conversation_id=ac, persona_id=ap, message_count=2
    )
    enqueue_voice_synthesis(
        migrated_engine, owner_id=b, conversation_id=bc, persona_id=bp, message_count=2
    )
    worker = _build_worker(
        dispatch_engine=migrated_engine,
        app_engine=app_engine,
        embedder=embedder,
        audit_root=tmp_path / "a",
    )
    assert _drain(worker) == 2

    store = _retrieval_store(app_engine, embedder, tmp_path / "r")
    chat = _chat_retrieval(store)

    # A (hiking) sees A's own fact ...
    with _as_owner(a):
        a_own = chat("does the user enjoy hiking on weekends")
        # ... and querying B's EXACT topic ("does rock climbing") returns NOTHING — RLS
        # is non-vacuous, not merely relevance-lucky: that query WOULD surface B's node
        # (cosine ~1.0 > the 0.66 chat floor) if the read were not owner-scoped.
        a_cross = chat("does rock climbing")
    assert "hiking" in " ".join(i.content.lower() for i in a_own.items)  # type: ignore[attr-defined]
    assert "climbing" not in " ".join(i.content.lower() for i in a_cross.items)  # type: ignore[attr-defined]

    # B (climbing, VOICE retrieval) sees B's own fact; A's exact topic is invisible.
    # b_cross uses A's EXACT fact phrasing so it WOULD surface A's node if RLS were off
    # (cosine ~1.0 > the 0.72 voice floor) — a real RLS probe, not a relevance miss.
    b_voice = build_voice_graph_retrieval(store, owner_id=b)
    with _as_owner(b):
        b_own = b_voice.retrieval("does rock climbing")
        b_cross = b_voice.retrieval("enjoys hiking on weekends")
    assert "climbing" in " ".join(i.content.lower() for i in b_own.items)
    assert "hiking" not in " ".join(i.content.lower() for i in b_cross.items)
