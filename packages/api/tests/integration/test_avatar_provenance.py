"""Avatar synthetic-media provenance — the three write paths (Spec R3, R3-D-3 / B1-B3).

EU AI Act Art. 50: every generated image is recorded generated-vs-uploaded and
the disclosure derives from the stored signal. These tests pin the **structural
provenance** half (acceptance #1) across all three avatar write paths and prove
the signal is RLS-scoped (never leaks cross-tenant):

- **B1 generated-inline** — ``POST /v1/personas`` create-hook (``_maybe_generate_avatar``
  → ``set_avatar_url``) co-writes ``avatar_source='generated'`` in the SAME ``UPDATE``
  as ``avatar_url``.
- **B2 generated-async** — the real ``AvatarGenerationHandler`` compare-and-set
  co-writes ``avatar_source='generated'``.
- **B3 uploaded** — ``PATCH /v1/personas/{id}`` carrying ``avatar_url`` (upload-to-change)
  co-writes ``avatar_source='uploaded'``.

The "no NULL window" invariant (R3-D-3) is structural: provenance is in the same
``.values(...)`` as the url, so no committed state has the url set but the source
NULL. Real FastAPI ``TestClient`` + real Postgres (skips when ``APP_DATABASE_URL``
is unset).
"""

# ruff: noqa: ANN401, ARG001, ARG002, E501
from __future__ import annotations

import asyncio
import hashlib
import os
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.imagegen import GeneratedImage, GenerationResult, ImageGenOptions
from persona.jobs import JobState
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.jobs import JobQueue, WorkerJobContext
from persona_api.jobs.executor import JobExecutor
from persona_api.jobs.handlers.avatar import (
    AVATAR_JOB_TYPE,
    AvatarGenerationHandler,
    AvatarGenerationPayload,
    AvatarResult,
    avatar_idempotency_key,
    enqueue_avatar_generation,
)
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from persona.imagegen.result import ImageMediaType
    from persona.jobs import JobRegistry
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

_TINY_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae"
    "426082"
)
_PNG_REF = hashlib.blake2b(_TINY_PNG, digest_size=16).hexdigest()

_USER = "u_avatar_prov"
_OTHER = "u_avatar_prov_other"


class _HappyBackend:
    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-1"

    async def generate(
        self, prompt: str, *, options: ImageGenOptions | None = None
    ) -> GenerationResult:
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


@pytest.fixture
def client(
    migrated_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, Path, Engine]]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")

    workspace_root = tmp_path / "workspace"
    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),
        workspace_root=workspace_root,
    )
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        app.state.image_backend = _HappyBackend()

        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            for uid in (_USER, _OTHER):
                conn.execute(
                    text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                    {"i": uid, "e": f"{uid}@x.test"},
                )
        yield c, workspace_root, su
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = ANY(:ids)"), {"ids": [_USER, _OTHER]})
        su.dispose()


def _auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_id}"}


def _avatar_source(su: Engine, persona_id: str) -> str | None:
    with su.begin() as conn:
        return conn.execute(
            text("SELECT avatar_source FROM personas WHERE id = :i"), {"i": persona_id}
        ).scalar_one()


# ---------------------------------------------------------------------------
# B1 — generated-inline (create-hook → set_avatar_url).
# ---------------------------------------------------------------------------


def test_b1_create_hook_marks_avatar_source_generated(
    client: tuple[TestClient, Path, Engine],
) -> None:
    c, _ws, su = client
    resp = c.post("/v1/personas", json={"yaml": _VALID_YAML}, headers=_auth(_USER))
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    # The background create-hook generated the avatar; both url + provenance landed
    # in the SAME write (no NULL window).
    detail = c.get(f"/v1/personas/{pid}", headers=_auth(_USER)).json()
    assert detail["avatar_url"] is not None
    assert _avatar_source(su, pid) == "generated"
    # Group D (acceptance #4): the API surfaces the structural signal + the derived
    # Art. 50 disclosure end-to-end.
    assert detail["avatar_source"] == "generated"
    assert detail["avatar_ai_generated"] is True


def test_b1_provenance_does_not_leak_cross_tenant(
    client: tuple[TestClient, Path, Engine],
) -> None:
    """A second tenant cannot read another tenant's persona (404 under RLS), so it
    can never read that persona's avatar provenance — the new readable field is
    RLS-scoped like every other column on the row."""
    c, _ws, _su = client
    resp = c.post("/v1/personas", json={"yaml": _VALID_YAML}, headers=_auth(_USER))
    pid = resp.json()["id"]
    # owner sees the persona; the other tenant is choked to 404.
    assert c.get(f"/v1/personas/{pid}", headers=_auth(_USER)).status_code == 200
    assert c.get(f"/v1/personas/{pid}", headers=_auth(_OTHER)).status_code == 404


# ---------------------------------------------------------------------------
# B3 — uploaded (PATCH carrying avatar_url).
# ---------------------------------------------------------------------------


def test_b3_patch_upload_marks_avatar_source_uploaded(
    client: tuple[TestClient, Path, Engine],
) -> None:
    c, _ws, su = client
    # Create with a user-supplied avatar_url (auto-gen is skipped — user wins).
    created = c.post(
        "/v1/personas",
        json={"yaml": _VALID_YAML, "avatar_url": "uploads/mine.png"},
        headers=_auth(_USER),
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]

    # PATCH a new uploaded avatar — the upload-to-change path co-writes 'uploaded'.
    patched = c.patch(
        f"/v1/personas/{pid}",
        json={"yaml": _VALID_YAML, "avatar_url": "uploads/changed.png"},
        headers=_auth(_USER),
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["avatar_url"] == "uploads/changed.png"
    assert _avatar_source(su, pid) == "uploaded"
    # Group D: an uploaded avatar discloses ai_generated=False end-to-end.
    assert patched.json()["avatar_source"] == "uploaded"
    assert patched.json()["avatar_ai_generated"] is False


def test_b3_create_with_user_avatar_marks_source_uploaded(
    client: tuple[TestClient, Path, Engine],
) -> None:
    """A user-supplied ``avatar_url`` at CREATE is an upload (the fourth write site:
    ``create_persona``'s INSERT) — provenance is co-written ``'uploaded'`` in the same
    INSERT, not left NULL. Auto-gen is skipped (user wins)."""
    c, _ws, su = client
    created = c.post(
        "/v1/personas",
        json={"yaml": _VALID_YAML, "avatar_url": "uploads/at_create.png"},
        headers=_auth(_USER),
    )
    assert created.status_code == 201, created.text
    pid = created.json()["id"]
    assert created.json()["avatar_url"] == "uploads/at_create.png"
    assert _avatar_source(su, pid) == "uploaded"


def test_b3_patch_without_avatar_leaves_source_untouched(
    client: tuple[TestClient, Path, Engine],
) -> None:
    """A YAML-only PATCH (no avatar_url) must NOT touch provenance — PATCH
    semantics for the presentation field."""
    c, _ws, su = client
    created = c.post(
        "/v1/personas",
        json={"yaml": _VALID_YAML, "avatar_url": "uploads/mine.png"},
        headers=_auth(_USER),
    )
    pid = created.json()["id"]
    assert _avatar_source(su, pid) == "uploaded"
    # YAML-only PATCH (no avatar_url) — provenance unchanged.
    resp = c.patch(f"/v1/personas/{pid}", json={"yaml": _VALID_YAML}, headers=_auth(_USER))
    assert resp.status_code == 200, resp.text
    assert _avatar_source(su, pid) == "uploaded"


# ---------------------------------------------------------------------------
# B2 — generated-async (the real AvatarGenerationHandler compare-and-set).
# ---------------------------------------------------------------------------


class _FakeGenerator:
    async def generate(
        self, *, persona_id: str, owner_id: str, yaml_str: str, billing_key: str
    ) -> AvatarResult | None:
        return AvatarResult(
            avatar_url=f"avatars/{persona_id}.png", cost_micros=1000, provider="fake"
        )


def _registry(generator: _FakeGenerator) -> JobRegistry:
    from persona.jobs import JobRegistry, JobTypeSpec, RetryPolicy

    return JobRegistry(
        [
            JobTypeSpec(
                type=AVATAR_JOB_TYPE,
                payload_model=AvatarGenerationPayload,
                handler=AvatarGenerationHandler(generator=generator),  # type: ignore[arg-type]
                idempotency_key=lambda p: avatar_idempotency_key(p.persona_id),
                retry=RetryPolicy(
                    max_attempts=3, base_backoff_seconds=0.01, max_backoff_seconds=0.01
                ),
            )
        ]
    )


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping avatar job test")
    return create_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def two_personas(migrated_engine: Engine) -> Iterator[Engine]:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES ('u_b2','b2@x.test'),('u_b2o','b2o@x.test')")
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES "
                "('p_b2','u_b2','name: A'),('p_b2o','u_b2o','name: B')"
            )
        )
    yield migrated_engine
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id IN ('u_b2','u_b2o')"))


def test_b2_async_job_marks_avatar_source_generated(
    two_personas: Engine, app_engine: Engine
) -> None:
    queue = JobQueue(two_personas)
    enqueue_avatar_generation(queue, persona_id="p_b2", owner_id="u_b2")
    gen = _FakeGenerator()
    executor = JobExecutor(
        queue=queue, registry=_registry(gen), rls_engine=app_engine, worker_id="w_b2"
    )
    out = asyncio.run(executor.execute(queue.claim(worker_id="w_b2", lease_seconds=30)[0]))
    assert out is JobState.SUCCEEDED

    with two_personas.begin() as conn:
        url, source = conn.execute(
            text("SELECT avatar_url, avatar_source FROM personas WHERE id='p_b2'")
        ).one()
    # Both landed in the same compare-and-set: url set AND provenance 'generated'.
    assert url == "avatars/p_b2.png"
    assert source == "generated"


def test_b2_handler_runs_owner_scoped_no_cross_tenant_write(
    two_personas: Engine, app_engine: Engine
) -> None:
    """The handler runs through the owner-scoped WorkerJobContext: a job for u_b2
    cannot touch u_b2o's persona (RLS choke). The other tenant's provenance stays
    NULL (untouched)."""
    queue = JobQueue(two_personas)
    enqueue_avatar_generation(queue, persona_id="p_b2", owner_id="u_b2")
    rec = queue.claim(worker_id="w_b2", lease_seconds=30)[0]
    assert queue.mark_running(job_id=rec.id, worker_id="w_b2")
    ctx = WorkerJobContext(
        owner_id="u_b2", rls_engine=app_engine, job_id=rec.id, job_type=AVATAR_JOB_TYPE
    )
    asyncio.run(
        AvatarGenerationHandler(generator=_FakeGenerator()).handle(  # type: ignore[arg-type]
            AvatarGenerationPayload(persona_id="p_b2"), ctx
        )
    )
    with two_personas.begin() as conn:
        mine = conn.execute(text("SELECT avatar_source FROM personas WHERE id='p_b2'")).scalar_one()
        other = conn.execute(
            text("SELECT avatar_source FROM personas WHERE id='p_b2o'")
        ).scalar_one()
    assert mine == "generated"
    assert other is None, "cross-tenant persona provenance must stay untouched"
