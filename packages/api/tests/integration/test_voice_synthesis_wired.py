"""V13-T5 — the voice enqueue → REAL A0 worker, end to end (the non-negotiable end proof).

The cross-layer real-transition ([[feedback_synthetic_harness_real_transition]]): a
synthesis job written by the **voice** raw-INSERT writer (``persona_voice`` — a peer
process to api, D-4-amended) is CLAIMED and PROCESSED by the **real A0 worker** — the
same ``Worker.run_once()`` the api composes — which windows the call transcript, runs
the synthesizer (cheap fake backend — the plumbing, not the model), advances the
marker, and writes the extracted fact into ``graph_nodes`` **marked ``channel=voice``**.
This is the home of the cross-layer test because the api worker + fixtures live here;
it imports the voice writer to drive the real seam (mirrors ``test_synthesis_pipeline_wired``).

A re-enqueue of the same call from the voice writer is an ``ON CONFLICT`` no-op (proven
directly against the writer's return value + the single processed job).
"""

# ruff: noqa: ARG001, ARG002 — fixture-ordering param + fakes ignore protocol args.
from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING, Any

import pytest
from persona.audit import JSONLAuditLogger
from persona.backends.types import ChatResponse, TokenUsage
from persona.graph import PostgresEntityRegistry, build_graph_store
from persona.graph.postgres import PostgresGraphBackend
from persona.jobs import JobRegistry
from persona_api.jobs import Worker
from persona_api.jobs.handlers.synthesis import PgSynthesisRepository, register_synthesis_handler
from persona_voice.session.synthesis_enqueue import enqueue_voice_synthesis
from sqlalchemy import text

if TYPE_CHECKING:
    from pathlib import Path

    from persona.stores.embedder import Embedder
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "voice_synth_user"
_PERSONA = "voice_synth_persona"
_CONVO = "voice_synth_call"


class _CheapBackend:
    """Returns ONE grounded candidate; the entity-judge path is never hit."""

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
        content = (
            '{"candidates": [{"concept_name": "vegetarian", "content": "is vegetarian",'
            ' "node_kind": "preference", "evidence_span": "I went vegetarian"}]}'
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
        pytest.skip("APP_DATABASE_URL not set; skipping wired voice-synthesis test")
    from persona_api.middleware.rls_context import make_rls_engine

    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    """Seed the owner + persona + a voice conversation + a two-turn transcript."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'vsynth@example.com')"), {"o": _OWNER}
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) "
                "VALUES (:p, :o, 'schema_version: \"1.0\"')"
            ),
            {"p": _PERSONA, "o": _OWNER},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, compacted_summary) "
                "VALUES (:c, :o, :p, '')"
            ),
            {"c": _CONVO, "o": _OWNER, "p": _PERSONA},
        )
        # A voice transcript persists to the SAME messages table (V9), channel voice.
        for role, content in (("user", "I went vegetarian"), ("assistant", "Noted.")):
            conn.execute(
                text(
                    "INSERT INTO messages (conversation_id, role, content, channel) "
                    'VALUES (:c, :r, :t, \'{"modality": "voice"}\')'
                ),
                {"c": _CONVO, "r": role, "t": content},
            )
    return migrated_engine


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
        graph_store=graph_store, registry=entity_registry, backend=_CheapBackend()
    )
    registry = JobRegistry()
    register_synthesis_handler(registry, runner=synthesizer, repository=PgSynthesisRepository())
    return Worker(
        dispatch_engine=dispatch_engine,
        rls_engine=app_engine,
        registry=registry,
        worker_id="w-vsynth",
    )


def test_voice_enqueued_job_is_claimed_processed_and_minted_as_voice(
    seeded: Engine, app_engine: Engine, embedder: Embedder, tmp_path: Path
) -> None:
    worker = _build_worker(
        dispatch_engine=seeded, app_engine=app_engine, embedder=embedder, audit_root=tmp_path / "a"
    )
    # The VOICE writer enqueues (not JobQueue) — the seam under test.
    job_id = enqueue_voice_synthesis(
        seeded, owner_id=_OWNER, conversation_id=_CONVO, persona_id=_PERSONA, message_count=2
    )
    assert job_id is not None

    # The real worker claims + processes exactly one job.
    assert asyncio.run(worker.run_once()) == 1

    with seeded.begin() as conn:
        # The extracted fact landed in the graph, owner-scoped ...
        rows = conn.execute(
            text("SELECT content, provenance::text FROM graph_nodes WHERE owner_id = :o"),
            {"o": _OWNER},
        ).all()
        state = conn.execute(
            text("SELECT state FROM jobs WHERE id = :i"), {"i": job_id}
        ).scalar_one()
    assert len(rows) == 1
    content, provenance_text = rows[0]
    assert content == "is vegetarian"
    # ... marked as VOICE-originated (the cross-channel provenance, source stays system).
    assert '"channel": "voice"' in provenance_text or '"channel":"voice"' in provenance_text
    assert state == "succeeded"


def test_voice_reenqueue_of_the_same_call_is_a_conflict_noop(
    seeded: Engine, app_engine: Engine, embedder: Embedder, tmp_path: Path
) -> None:
    # First enqueue writes the job; the identical second is an A0 ON CONFLICT no-op
    # (same conversation_id + message_count → same idempotency key), so no dup mint.
    first = enqueue_voice_synthesis(
        seeded, owner_id=_OWNER, conversation_id=_CONVO, persona_id=_PERSONA, message_count=2
    )
    second = enqueue_voice_synthesis(
        seeded, owner_id=_OWNER, conversation_id=_CONVO, persona_id=_PERSONA, message_count=2
    )
    assert first is not None
    assert second is None  # the re-enqueue dedup'd

    worker = _build_worker(
        dispatch_engine=seeded, app_engine=app_engine, embedder=embedder, audit_root=tmp_path / "a"
    )
    assert asyncio.run(worker.run_once()) == 1
    with seeded.begin() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM graph_nodes WHERE owner_id = :o"), {"o": _OWNER}
        ).scalar_one()
    assert count == 1  # exactly one fact — no duplicate from the re-enqueue
