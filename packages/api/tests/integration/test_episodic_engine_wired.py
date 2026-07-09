"""The WIRED sleep-time engine — end-to-end through the A0 worker (Spec K8, T8).

The T6 carry-forward, the A4 anti-inertness bar: the proof is the FIRE, not the
registration. No hand-enqueue of the episodic job, no hand-invoked handler or
engine anywhere in the live legs:

- **Turn-end leg**: the REAL turn-boundary producer (the exact function
  ``ChatTurnWorker._enqueue_synthesis`` calls at every completed turn) →
  the coalesced ``episodic_consolidation`` job row → the REAL worker claims it
  through the REAL composition root (``build_worker_registry``) → the engine
  runs → real gists + real ``episode`` concept nodes exist; raw rows untouched.
- **Run-end leg**: the REAL agentic-run-completion producer causes the same
  claim → fire → outputs chain.
- **Gating leg**: the composition root registers the handler ONLY when
  ``PERSONA_EPISODIC_ENGINE_ENABLED`` is on (built-but-inert, handler half —
  the trigger half is pinned in the unit suite).

Timing knobs ride the REAL config path (env → ``EpisodicSettings``): the tests
set idle delay 0 / bucket 1s so the deferred job is immediately claimable —
config, not code, exactly how an operator would tune cadence.
"""

# ruff: noqa: ARG002 — fakes ignore protocol args.
from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.stores.postgres import PostgresBackend
from persona_api.background.worker_root import build_worker_registry
from persona_api.config import APIConfig
from persona_api.jobs import JobQueue, Worker
from persona_api.jobs.handlers.episodic_consolidation import EPISODIC_CONSOLIDATION_JOB_TYPE
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.services.synthesis_trigger import (
    enqueue_conversation_synthesis,
    enqueue_run_synthesis,
)
from sqlalchemy import text

if TYPE_CHECKING:
    from pathlib import Path

    from persona.stores.embedder import Embedder
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


class _CheapBackend:
    """Deterministic fake provider behind the REAL tier adapters (wired-test convention)."""

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

    async def chat(self, messages: object, **kwargs: object) -> ChatResponse:
        return ChatResponse(
            content="They discussed moving to Oslo and starting the new job.",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            model=self.model_name,
            provider=self.provider_name,
            latency_ms=0.0,
        )

    def chat_stream(self, *a: object, **k: object) -> object:
        raise NotImplementedError


class _FakeTierRegistry:
    def get(self, tier: str) -> _CheapBackend:
        return _CheapBackend()


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:  # noqa: ARG001 — ordering: schema first
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping wired-engine test")
    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


def _fast_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PERSONA_EPISODIC_ENGINE_ENABLED", "true")
    monkeypatch.setenv("PERSONA_EPISODIC_IDLE_DELAY_SECONDS", "0")
    monkeypatch.setenv("PERSONA_EPISODIC_BUCKET_SECONDS", "1")
    monkeypatch.setenv("PERSONA_EPISODIC_MIN_CHUNKS_PER_RUN", "2")


def _seed(migrated_engine: Engine, embedder: Embedder, owner: str, persona: str) -> list[str]:
    """Owner + persona + one CLOSED episodic session (superuser seed)."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'w@example.com')"), {"o": owner}
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: w')"),
            {"p": persona, "o": owner},
        )
    backend = PostgresBackend(engine=migrated_engine, embedder=embedder)
    chunks = []
    for i, line in enumerate(
        ("USER: we moved to Oslo\nASSISTANT: great", "USER: job starts Monday\nASSISTANT: nice")
    ):
        cid = mint_chunk_id(persona, "episodic")
        created = datetime.now(UTC) - timedelta(hours=3) + timedelta(minutes=i)
        chunks.append(
            PersonaChunk(
                id=cid,
                text=line,
                created_at=created,
                provenance=ChunkProvenance(
                    source=WriteSource.SYSTEM,
                    logical_id=cid,
                    version=1,
                    written_at=created,
                    written_by="runtime.loop",
                ),
            )
        )
    backend.upsert(persona_id=persona, store_kind="episodic", chunks=chunks)
    return [c.id for c in chunks]


def _registry(app_engine: Engine, embedder: Embedder, audit_root: Path) -> object:
    return build_worker_registry(
        rls_engine=app_engine,
        embedder=embedder,
        tier_registry=_FakeTierRegistry(),  # type: ignore[arg-type]
        config=APIConfig(audit_root=str(audit_root)),
        synthesis_tier="small",
        memory_backend=PostgresBackend(engine=app_engine, embedder=embedder),
    )


def _drain(worker: Worker, *, max_jobs: int = 6) -> int:
    ran = 0
    for _ in range(max_jobs):
        if asyncio.run(worker.run_once()) != 1:
            break
        ran += 1
    return ran


def _fire_and_verify(
    migrated_engine: Engine,
    app_engine: Engine,
    embedder: Embedder,
    audit_root: Path,
    *,
    owner: str,
    persona: str,
    raw_ids: list[str],
) -> None:
    registry = _registry(app_engine, embedder, audit_root)
    assert EPISODIC_CONSOLIDATION_JOB_TYPE in registry.types()  # type: ignore[attr-defined]
    worker = Worker(
        dispatch_engine=migrated_engine, rls_engine=app_engine, registry=registry, worker_id="w-k8"
    )
    ran = _drain(worker)
    assert ran >= 1  # the worker CLAIMED — nothing was hand-invoked

    with migrated_engine.begin() as conn:
        gists = conn.execute(
            text(
                "SELECT member_ids FROM memory_chunks "
                "WHERE persona_id = :p AND kind = 'episodic_gist'"
            ),
            {"p": persona},
        ).all()
        # R4-C1-15: episode nodes are named from the gist's opening sentence
        # (no longer the ``episode <timestamp>`` label), so select the node by
        # the engine's provenance reason — the stable merge contract.
        episode_rows = conn.execute(
            text(
                "SELECT concept_name FROM graph_nodes "
                "WHERE owner_id = :o AND provenance @> "
                """'[{"reason": "sleep-time episodic consolidation"}]'"""
            ),
            {"o": owner},
        ).all()
        raw = conn.execute(
            text("SELECT id, text FROM memory_chunks WHERE persona_id = :p AND kind = 'episodic'"),
            {"p": persona},
        ).all()
    assert len(gists) == 1  # the closed session got its gist THROUGH the fire
    assert set(gists[0].member_ids) == set(raw_ids)
    assert len(episode_rows) == 1  # and its concept node landed via the real merge
    # R4-C1-15 naming: the node reads as a real memory (the gist's opening
    # sentence from the deterministic fake), never the timestamp fallback.
    assert episode_rows[0].concept_name.startswith("They discussed moving to Oslo")
    assert {r.id for r in raw} == set(raw_ids)  # raw layer untouched


def test_turn_end_fires_the_engine_through_the_real_worker(
    migrated_engine: Engine,
    app_engine: Engine,
    embedder: Embedder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_cadence(monkeypatch)
    owner, persona = "wired_turn_user", "wired_turn_persona"
    raw_ids = _seed(migrated_engine, embedder, owner, persona)

    # THE REAL BOUNDARY: the exact producer ChatTurnWorker calls at turn-end.
    # (Its own callers are pinned by the P1/K2 suites; the K8 delta rides it.)
    enqueue_conversation_synthesis(
        JobQueue(migrated_engine),
        owner_id=owner,
        conversation_id="wired-convo",
        persona_id=persona,
        message_count=2,
    )
    with migrated_engine.begin() as conn:
        pending = conn.execute(
            text("SELECT count(*) FROM jobs WHERE type = :t AND owner_id = :o"),
            {"t": EPISODIC_CONSOLIDATION_JOB_TYPE, "o": owner},
        ).scalar_one()
    assert pending == 1  # the boundary produced the coalesced job

    _fire_and_verify(
        migrated_engine,
        app_engine,
        embedder,
        tmp_path / "audit-turn",
        owner=owner,
        persona=persona,
        raw_ids=raw_ids,
    )


def test_run_end_fires_the_engine_through_the_real_worker(
    migrated_engine: Engine,
    app_engine: Engine,
    embedder: Embedder,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fast_cadence(monkeypatch)
    owner, persona = "wired_run_user", "wired_run_persona"
    raw_ids = _seed(migrated_engine, embedder, owner, persona)

    # THE REAL BOUNDARY: the exact producer run_worker calls at run completion.
    enqueue_run_synthesis(
        JobQueue(migrated_engine), owner_id=owner, run_id="wired-run", persona_id=persona
    )

    _fire_and_verify(
        migrated_engine,
        app_engine,
        embedder,
        tmp_path / "audit-run",
        owner=owner,
        persona=persona,
        raw_ids=raw_ids,
    )


def test_engine_handler_registered_only_when_enabled(
    app_engine: Engine, embedder: Embedder, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PERSONA_EPISODIC_ENGINE_ENABLED", "true")
    enabled = _registry(app_engine, embedder, tmp_path / "a1")
    assert EPISODIC_CONSOLIDATION_JOB_TYPE in enabled.types()  # type: ignore[attr-defined]

    monkeypatch.setenv("PERSONA_EPISODIC_ENGINE_ENABLED", "false")
    disabled = _registry(app_engine, embedder, tmp_path / "a2")
    assert EPISODIC_CONSOLIDATION_JOB_TYPE not in disabled.types()  # type: ignore[attr-defined]
