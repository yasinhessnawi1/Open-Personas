"""K10 end-to-end acceptance — through the APP, no hand-forced writes (Spec K10, T8).

Two acceptance proofs on a REAL managed embedded Postgres, driven through the app:

1. **Chat → worker → memory → recall.** A fresh managed install → a real HTTP
   conversation turn → the REAL synthesis producer enqueues a job → a REAL worker
   drain (``build_worker_registry`` + ``Worker.run_once`` — the same classes the app
   runs, with a deterministic fake tier backend because tests have no model key) →
   a graph node lands in the managed store → the REAL ``/v1/memory/graph`` route
   renders it (``available=true``) → the REAL ``/v1/memory/search`` route (K1 hybrid
   retrieval) surfaces it. The job is enqueued by the actual chat turn, not by hand.

2. **Managed boot auto-imports a legacy install.** A legacy SQLite + Chroma store is
   present at the configured paths → the app boots managed, ``maybe_run_community_import``
   runs on the boot path (T6), the imported persona is served over ``/v1/personas``,
   and the legacy sources are renamed to ``*.migrated-*``.
"""

# ruff: noqa: ARG002 — fakes ignore protocol args.
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.backends.types import ChatResponse
from persona.schema.conversation import ConversationMessage
from persona_api.app import create_app
from persona_api.background.worker_root import build_worker_registry
from persona_api.config import APIConfig, Edition
from persona_api.jobs import Worker
from sqlalchemy import text

pytestmark = pytest.mark.integration

pytest.importorskip("pixeltable_pgserver")

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from persona.schema.conversation import Conversation
    from tests.conftest import HashEmbedder384

_YAML = (
    "schema_version: '1.0'\n"
    "identity:\n"
    "  name: Sigrid\n"
    "  role: research assistant\n"
    "  background: A research assistant built to help with literature reviews.\n"
)
_MODEL_KEY_VARS = (
    "PERSONA_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "DEEPSEEK_API_KEY",
)


class _ScriptedLoop:
    """A ConversationLoop stand-in (no model backend) — mirrors community_flows."""

    def __init__(self, reply: str = "Noted about your diet.") -> None:
        self._reply = reply

    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: Callable[[object], Awaitable[None]] | None = None,
        *,
        turn_has_image: bool = False,
        images: list[object] | None = None,
        documents: list[object] | None = None,
        document_context: object | None = None,
    ) -> AsyncIterator[StreamChunk]:
        now = datetime.now(UTC)
        conversation.messages.append(
            ConversationMessage(role="user", content=user_message, created_at=now)
        )
        yield StreamChunk(delta=self._reply, is_final=False)
        conversation.messages.append(
            ConversationMessage(role="assistant", content=self._reply, created_at=now)
        )
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        )


class _CheapBackend:
    """Deterministic one-candidate extractor — real synthesis, no model key."""

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

    def chat_stream(self, *a: object, **k: object) -> object:
        raise NotImplementedError


class _FakeTierRegistry:
    def get(self, tier: str) -> _CheapBackend:
        return _CheapBackend()


def _managed_community_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    embedder: HashEmbedder384,
    *,
    db_path: Path,
    memory_path: Path,
) -> APIConfig:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("APP_DATABASE_URL", raising=False)
    # Keyless model config → a live tier_registry is built (chat runs via the
    # injected scripted loop); the app's OWN worker is disabled (no model key), and
    # the drain is driven manually with a working fake backend (real Worker class).
    monkeypatch.setenv("PERSONA_PROVIDER", "deepseek")
    monkeypatch.setenv("PERSONA_MODEL", "deepseek-chat")
    monkeypatch.setenv("PERSONA_API_IN_PROCESS_WORKER", "false")
    for var in _MODEL_KEY_VARS:
        monkeypatch.delenv(var, raising=False)
    from persona_api.services import persona_service

    monkeypatch.setattr(persona_service, "default_embedder", lambda *_a, **_k: embedder)
    return APIConfig(
        edition=Edition.community,
        community_db_mode="embedded",
        community_managed_db_dir=tmp_path / "pgbase",
        community_db_path=db_path,
        community_memory_path=memory_path,
        workspace_root=tmp_path / "work",
        audit_root=str(tmp_path / "audit"),
    )


def _drain(worker: Worker, *, max_jobs: int = 8) -> int:
    ran = 0
    for _ in range(max_jobs):
        if asyncio.run(worker.run_once()) != 1:
            break
        ran += 1
    return ran


def _short_base() -> Path:
    # The embedded datadir socket path must stay under the AF_UNIX cap; tmp_path on
    # macOS is long, so relocate the managed base under a short /tmp dir.
    import tempfile

    return Path(tempfile.mkdtemp(prefix="k10e2e", dir="/tmp"))


def test_managed_chat_to_memory_recall_end_to_end(
    monkeypatch: pytest.MonkeyPatch, embedder: HashEmbedder384
) -> None:
    import shutil

    from fastapi.testclient import TestClient

    base = _short_base()
    config = _managed_community_app(
        base,
        monkeypatch,
        embedder,
        db_path=base / ".persona_community.db",  # absent → fresh managed install
        memory_path=base / ".persona_chroma",
    )
    app = create_app(config)
    try:
        with TestClient(app) as client:
            assert str(client.app.state.rls_engine.url).startswith("postgresql")

            async def _build_loop(_persona_id: str) -> _ScriptedLoop:
                return _ScriptedLoop()

            client.app.state.build_conversation_loop = _build_loop  # type: ignore[attr-defined]

            pid = client.post("/v1/personas", json={"yaml": _YAML}).json()["id"]
            conv_id = client.post(f"/v1/personas/{pid}/conversations", json={"title": ""}).json()[
                "id"
            ]
            # THE REAL CHAT TURN (HTTP) — its completion enqueues synthesis.
            resp = client.post(
                f"/v1/conversations/{conv_id}/messages", json={"content": "I went vegetarian"}
            )
            assert resp.status_code == 200, resp.text
            assert "event: chunk" in resp.text

            rls_engine = client.app.state.rls_engine
            # The real producer enqueued a synthesis job through the app.
            with rls_engine.begin() as conn:
                pending = conn.execute(
                    text("SELECT count(*) FROM jobs WHERE type = 'synthesis'")
                ).scalar_one()
            assert pending >= 1, "the real chat turn enqueued synthesis (no hand-enqueue)"

            # A REAL worker drain (real registry + Worker.run_once, fake tier backend).
            registry = build_worker_registry(
                rls_engine=rls_engine,
                embedder=embedder,
                tier_registry=_FakeTierRegistry(),  # type: ignore[arg-type]
                config=APIConfig(audit_root=str(base / "worker-audit")),
                synthesis_tier="small",
                memory_backend=client.app.state.memory_backend,
            )
            worker = Worker(
                dispatch_engine=rls_engine,
                rls_engine=rls_engine,
                registry=registry,
                worker_id="e2e",
            )
            assert _drain(worker) >= 1  # the worker claimed + ran the real passes

            # graph nodes appear → the /memory route renders them (available=true).
            window = client.get("/v1/memory/graph")
            assert window.status_code == 200, window.text
            body = window.json()
            assert body["available"] is True
            assert body["total_nodes"] >= 1
            assert any("vegetarian" in n["label"] for n in body["nodes"]), body

            # recall surfaces it — the K1 hybrid-retrieval search route.
            found = client.get("/v1/memory/search", params={"q": "vegetarian"})
            assert found.status_code == 200, found.text
            results = found.json()["results"]
            assert any("vegetarian" in r["label"] for r in results), found.json()
    finally:
        shutil.rmtree(base, ignore_errors=True)


def _build_legacy_store(root: Path, embedder: HashEmbedder384) -> tuple[Path, Path, str]:
    from persona.stores.chroma import ChromaBackend
    from persona_api.db.community import (
        create_community_schema,
        ensure_owner,
        make_community_engine,
    )

    sqlite_path = root / ".persona_community.db"
    chroma_path = root / ".persona_chroma"
    persona_id = "33333333-3333-4333-8333-333333333333"
    engine = make_community_engine(sqlite_path)
    create_community_schema(engine)
    ensure_owner(engine, owner_id="local-owner", email="local@localhost")
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'name: Imported')"),
            {"p": persona_id, "o": "local-owner"},
        )
    engine.dispose()
    from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id

    cb = ChromaBackend(persist_path=chroma_path, embedder=embedder)
    cid = mint_chunk_id(persona_id, "identity")
    now = datetime.now(UTC)
    cb.upsert(
        persona_id=persona_id,
        store_kind="identity",
        chunks=[
            PersonaChunk(
                id=cid,
                text="Imported persona identity",
                created_at=now,
                provenance=ChunkProvenance(
                    source=WriteSource.SYSTEM, logical_id=cid, version=1, written_at=now,
                    written_by="test",
                ),
            )
        ],
    )
    return sqlite_path, chroma_path, persona_id


def test_managed_boot_auto_imports_legacy_and_serves_it(
    monkeypatch: pytest.MonkeyPatch, embedder: HashEmbedder384
) -> None:
    import shutil

    from fastapi.testclient import TestClient

    base = _short_base()
    sqlite_path, chroma_path, persona_id = _build_legacy_store(base, embedder)
    assert sqlite_path.exists()  # legacy store present before boot

    config = _managed_community_app(
        base, monkeypatch, embedder, db_path=sqlite_path, memory_path=chroma_path
    )
    app = create_app(config)
    try:
        with TestClient(app) as client:  # lifespan runs maybe_run_community_import (T6/T8)
            assert str(client.app.state.rls_engine.url).startswith("postgresql")
            # The imported persona is served from the managed store.
            listed = client.get("/v1/personas")
            assert listed.status_code == 200, listed.text
            assert any(p["id"] == persona_id for p in listed.json()), listed.json()
        # The legacy sources were renamed to the rollback marker (import complete).
        assert not sqlite_path.exists()
        assert any(p.name.startswith(".persona_community.db.migrated-") for p in base.iterdir())
    finally:
        shutil.rmtree(base, ignore_errors=True)
