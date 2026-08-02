"""The AVATAR_VIA_QUEUE cutover is COMPLETE and gate-unified (R9-013).

Pre-fix, ``PERSONA_API_AVATAR_VIA_QUEUE=true`` made the create route enqueue
``avatar_generation`` jobs while :func:`register_avatar_handler` had zero
callers — every enqueued job was unknown-type poison and avatar generation via
the queue was completely dead. Proves, against real Postgres + the real FastAPI
create route + the real Worker claim→execute path:

1. **The real trigger chain** — flag on + image backend composed: persona-create
   enqueues exactly one job (create-keyed), a real worker run invokes the
   generator, and the handler persists ``avatar_url`` (+ ``avatar_source=
   'generated'``) through its compare-and-set;
2. **Gate unification** — flag on + NO image backend: the route does NOT enqueue
   (``avatar_queue_ready`` is the producer's AND the registration's one
   predicate) and falls back to the inline fail-soft path (avatar stays null,
   create still 201s);
3. **Registration follows the same gate** — ``build_worker_registry`` registers
   the ``avatar_generation`` tenant iff the shared predicate passes.
"""

# ruff: noqa: ANN401, ARG001, ARG002, SLF001 — fixtures + protocol args + private internals.
from __future__ import annotations

import asyncio
import hashlib
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from persona.backends.types import ChatResponse, TokenUsage
from persona.imagegen import GeneratedImage, GenerationResult, ImageGenOptions
from persona.jobs import JobRegistry
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.background.worker_root import build_worker_registry
from persona_api.config import APIConfig
from persona_api.imagegen.service import ImagegenAvatarGenerator
from persona_api.jobs.handlers.avatar import AVATAR_JOB_TYPE, register_avatar_handler
from persona_api.jobs.worker import Worker
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persona.imagegen.result import ImageMediaType
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_VALID_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: |
    Helps tenants understand husleieloven.
  language_default: en
  constraints: []
"""

# Minimum-valid 1x1 RGB PNG (mirrors the imagegen service/route tests).
_TINY_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae"
    "426082"
)
_PNG_REF = hashlib.blake2b(_TINY_PNG, digest_size=16).hexdigest()

_USER = "u_avatar_cutover"


class _CountingBackend:
    """A happy fake ImageBackend that counts provider invocations."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-1"

    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
        self.calls += 1
        media: ImageMediaType = "image/png"
        return GenerationResult(
            images=[
                GeneratedImage(
                    image_bytes=_TINY_PNG,
                    workspace_path=None,
                    media_type=media,
                    width=1,
                    height=1,
                    revised_prompt=None,
                )
            ],
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=5.0,
        )

    async def edit(self, *a: object, **k: object) -> GenerationResult:
        raise NotImplementedError


# --- minimal tier-registry stubs (the test_a5_default_off idiom) -------------


class _Backend:
    provider_name = "anthropic"
    model_name = "scripted"

    async def chat(self, messages: Any, **_: object) -> ChatResponse:
        return ChatResponse(
            content="{}",
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


class _TierRegistry:
    def get(self, _tier: str) -> _Backend:
        return _Backend()

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ("frontier", "mid", "small")

    def metadata_for(self, _tier: str) -> None:
        return None


class _Emb:
    model_name = "fake"

    @property
    def dimension(self) -> int:
        return 384

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [[1.0] + [0.0] * 383 for _ in texts]


class _FakeStorage:
    def save(self, *a: object, **k: object) -> str:
        return "uploads/fake.png"


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping avatar cutover test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


@pytest.fixture
def client(
    migrated_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, Path]]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    workspace_root = tmp_path / "workspace"
    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),
        workspace_root=workspace_root,
        avatar_via_queue=True,  # the cutover flag ON — the R9-013 shape
    )
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": _USER, "e": f"{_USER}@x.test"},
            )
        yield c, workspace_root
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": _USER})
        su.dispose()


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_USER}"}


def _create(c: TestClient) -> dict[str, Any]:
    resp = c.post("/v1/personas", json={"yaml": _VALID_YAML}, headers=_auth())
    assert resp.status_code == 201, resp.text
    detail: dict[str, Any] = resp.json()
    return detail


def _avatar_jobs(engine: Engine) -> list[tuple[str, str]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text("SELECT idempotency_key, state FROM jobs WHERE type = :t"),
            {"t": AVATAR_JOB_TYPE},
        ).all()
    return [(r.idempotency_key, r.state) for r in rows]


def test_flag_on_with_backend_enqueues_and_real_worker_generates_and_persists(
    client: tuple[TestClient, Path], migrated_engine: Engine, app_engine: Engine
) -> None:
    """The full real chain: route → enqueue → real claim/execute → generator → CAS persist."""
    c, _ws = client
    provider = _CountingBackend()
    c.app.state.image_backend = provider  # type: ignore[attr-defined]

    detail = _create(c)
    pid = detail["id"]
    assert detail["avatar_url"] is None  # async-create: the queue fills it in

    # The producer enqueued exactly one create-keyed durable job.
    jobs = _avatar_jobs(migrated_engine)
    assert jobs == [(f"avatar:{pid}:create", "queued")]

    # A REAL worker whose registry holds the REAL generator composition (the
    # worker-root wiring: ImagegenAvatarGenerator over backend + file storage).
    registry = JobRegistry()
    register_avatar_handler(
        registry,
        ImagegenAvatarGenerator(
            backend=provider,
            file_storage=c.app.state.file_storage,  # type: ignore[attr-defined]
            audit_logger=None,
            timeout_s=10.0,
        ),
    )
    worker = Worker(
        dispatch_engine=migrated_engine,
        rls_engine=app_engine,
        registry=registry,
        worker_id="w-avatar",
    )
    ran = asyncio.run(worker.run_once(batch=5))
    assert ran == 1

    assert provider.calls == 1  # the generator ran through the real handler
    jobs = _avatar_jobs(migrated_engine)
    assert jobs == [(f"avatar:{pid}:create", "succeeded")]
    # Persisted through the handler's compare-and-set, with provenance co-written.
    body = c.get(f"/v1/personas/{pid}", headers=_auth()).json()
    assert body["avatar_url"] == f"uploads/{_PNG_REF}.png"
    assert body["avatar_source"] == "generated"


def test_flag_on_without_backend_does_not_enqueue_and_falls_back_inline(
    client: tuple[TestClient, Path], migrated_engine: Engine
) -> None:
    """Gate unification: no image backend ⇒ the producer must NOT enqueue a dead job."""
    c, _ws = client
    c.app.state.image_backend = None  # type: ignore[attr-defined]

    detail = _create(c)
    pid = detail["id"]

    # No job — the shared predicate failed, so the route took the inline path
    # (which fail-softs to avatar_url=null when no backend is configured).
    assert _avatar_jobs(migrated_engine) == []
    body = c.get(f"/v1/personas/{pid}", headers=_auth()).json()
    assert body["avatar_url"] is None


def test_registry_registers_avatar_tenant_iff_shared_gate_passes(
    app_engine: Engine, tmp_path: Path
) -> None:
    """The registration side of the ONE gate (build_worker_registry)."""

    def types_for(*, flag: bool, backend: object | None, storage: object | None) -> set[str]:
        return set(
            build_worker_registry(
                rls_engine=app_engine,
                embedder=_Emb(),  # type: ignore[arg-type]
                tier_registry=_TierRegistry(),  # type: ignore[arg-type]
                free_tier_registry=None,  # R9-096: no plans here — gating off, stated
                config=APIConfig(audit_root=str(tmp_path), avatar_via_queue=flag),
                synthesis_tier="small",
                image_backend=backend,  # type: ignore[arg-type]
                file_storage=storage,  # type: ignore[arg-type]
            ).types()
        )

    backend = _CountingBackend()
    storage = _FakeStorage()
    assert AVATAR_JOB_TYPE in types_for(flag=True, backend=backend, storage=storage)
    assert AVATAR_JOB_TYPE not in types_for(flag=True, backend=None, storage=storage)
    assert AVATAR_JOB_TYPE not in types_for(flag=True, backend=backend, storage=None)
    assert AVATAR_JOB_TYPE not in types_for(flag=False, backend=backend, storage=storage)
