"""The WIRED synthesis pipeline — end-to-end against real Postgres (Spec K2, T8d).

Proves the activated consumer: enqueue a ``synthesis`` job → the worker claims it
→ the :class:`SynthesisHandler` (composed by the worker root via
``register_synthesis_handler`` over the real ``build_synthesizer`` + real
``PgSynthesisRepository`` + real ``PostgresGraphStore``) windows the conversation,
runs the synthesizer on a CHEAP fake backend (the plumbing — not the model), and:

- advances the ``synthesis_markers`` high-water-mark (idempotency marker), AND
- writes the extracted fact into ``graph_nodes``, owner-scoped (RLS holds).

A re-delivery of the same job is a marker no-op (no duplicate graph write). This is
the plumbing leg — the model-quality gate is the separate ``@pytest.mark.external``
extraction eval re-run on the wired tier.
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
from persona_api.jobs import JobQueue, Worker
from persona_api.jobs.handlers.synthesis import (
    PgSynthesisRepository,
    SynthesisJobPayload,
    register_synthesis_handler,
    synthesis_idempotency_key,
)
from sqlalchemy import text

if TYPE_CHECKING:
    from pathlib import Path

    from persona.stores.embedder import Embedder
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "synth_user"
_PERSONA = "synth_persona"
_CONVO = "synth_convo"


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
    """The ``persona_app`` RLS engine — built via ``make_rls_engine`` so the
    checkout listener sets ``app.current_user_id`` from the contextvar the worker
    binds per job. A plain ``create_engine`` would leave the GUC unset and RLS
    would block every owner-scoped graph write (this is exactly the production
    wiring the worker root composes)."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping wired-synthesis test")
    from persona_api.middleware.rls_context import make_rls_engine

    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    """Seed the owner + persona + conversation + a two-turn transcript."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'synth@example.com')"), {"o": _OWNER}
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
        for role, content in (("user", "I went vegetarian"), ("assistant", "Noted.")):
            conn.execute(
                text("INSERT INTO messages (conversation_id, role, content) VALUES (:c, :r, :t)"),
                {"c": _CONVO, "r": role, "t": content},
            )
    return migrated_engine


def _build_worker(
    *, dispatch_engine: Engine, app_engine: Engine, embedder: Embedder, audit_root: Path
) -> Worker:
    """Compose the worker exactly as ``worker_root.build_worker_registry`` does."""
    graph_backend = PostgresGraphBackend(engine=app_engine, persona_guard=False, node_releaser=None)
    graph_store = build_graph_store(
        engine=app_engine,
        embedder=embedder,
        audit_logger=JSONLAuditLogger(audit_root),
        persona_guard=False,
        node_releaser=None,
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
        worker_id="w-synth",
    )


def _graph_node_count(engine: Engine, owner_id: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM graph_nodes WHERE owner_id = :o"), {"o": owner_id}
            ).scalar_one()
        )


def _marker(engine: Engine, owner_id: str) -> int | None:
    with engine.begin() as conn:
        return conn.execute(
            text(
                "SELECT synthesised_up_to FROM synthesis_markers "
                "WHERE owner_id = :o AND interaction_id = :i"
            ),
            {"o": owner_id, "i": _CONVO},
        ).scalar_one_or_none()


def test_wired_handler_advances_marker_and_writes_graph(
    seeded: Engine, app_engine: Engine, embedder: Embedder, tmp_path: Path
) -> None:
    worker = _build_worker(
        dispatch_engine=seeded,
        app_engine=app_engine,
        embedder=embedder,
        audit_root=tmp_path / "audit",
    )
    payload = SynthesisJobPayload(
        interaction_kind="conversation",
        interaction_id=_CONVO,
        persona_id=_PERSONA,
        high_water_mark=2,
    )
    job = JobQueue(seeded).enqueue(
        type="synthesis",
        owner_id=_OWNER,
        payload=payload.model_dump(),
        idempotency_key=synthesis_idempotency_key(payload),
    )
    assert job is not None

    # Assertions read via the SUPERUSER engine (``seeded``) — it bypasses RLS, so
    # the test sees the owner's rows without binding the request contextvar (the
    # worker binds it per job; a bare assertion read would otherwise be RLS-empty).
    assert _graph_node_count(seeded, _OWNER) == 0
    assert _marker(seeded, _OWNER) is None

    # Run the worker once: claim → window → synthesise → meter → advance + graph write.
    assert asyncio.run(worker.run_once()) == 1

    # The handler advanced the high-water-mark marker (idempotency) ...
    assert _marker(seeded, _OWNER) == 2
    # ... and the extracted fact landed in the graph, owner-scoped.
    assert _graph_node_count(seeded, _OWNER) == 1
    with seeded.begin() as conn:
        content = conn.execute(
            text("SELECT content FROM graph_nodes WHERE owner_id = :o"), {"o": _OWNER}
        ).scalar_one()
    assert content == "is vegetarian"

    # The job succeeded.
    with seeded.begin() as conn:
        state = conn.execute(
            text("SELECT state FROM jobs WHERE id = :i"), {"i": job.id}
        ).scalar_one()
    assert state == "succeeded"


def test_redelivery_is_a_marker_noop_no_duplicate_graph_write(
    seeded: Engine, app_engine: Engine, embedder: Embedder, tmp_path: Path
) -> None:
    worker = _build_worker(
        dispatch_engine=seeded,
        app_engine=app_engine,
        embedder=embedder,
        audit_root=tmp_path / "audit",
    )
    payload = SynthesisJobPayload(
        interaction_kind="conversation",
        interaction_id=_CONVO,
        persona_id=_PERSONA,
        high_water_mark=2,
    )
    queue = JobQueue(seeded)
    queue.enqueue(
        type="synthesis",
        owner_id=_OWNER,
        payload=payload.model_dump(),
        idempotency_key=synthesis_idempotency_key(payload),
    )
    assert asyncio.run(worker.run_once()) == 1
    assert _graph_node_count(seeded, _OWNER) == 1

    # A re-delivery of the SAME interaction window: the marker is already at 2, so
    # the window is empty → no synthesise, no second graph write.
    queue.enqueue(
        type="synthesis",
        owner_id=_OWNER,
        payload=payload.model_dump(),
        idempotency_key=synthesis_idempotency_key(payload) + ":redelivery",
    )
    assert asyncio.run(worker.run_once()) == 1
    assert _graph_node_count(seeded, _OWNER) == 1  # still exactly one — no duplicate
    assert _marker(seeded, _OWNER) == 2
