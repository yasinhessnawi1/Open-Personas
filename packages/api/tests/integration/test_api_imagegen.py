"""Integration tests for ``POST /v1/personas/:id/imagegen`` (spec 15 T16).

Exercises the route through a real FastAPI :class:`TestClient` against
Docker Postgres so the cap + deduct + persist + audit composition is
verified end-to-end against the live RLS engine + advisory-lock
primitive + audit ledger.

Seven scenarios per ``tasks.md`` §T16 acceptance bullets:

1. **Happy path** — 201 + ImageRef-shape payload; bytes land on disk at
   the D-13-4 ``uploads/<blake2b>.<ext>`` layout; the API-layer audit
   row carries the REQUESTED size (D-15-X-size-rounding); a subsequent
   ``GET /v1/personas/:id/uploads/:ref`` serves the same bytes
   (provenance-blind verification — D-15-X-workspace-coordination).
2. **Cross-tenant** — user B tries to generate against user A's persona
   id → 404 (existence-disclosure-safe per D-08-1; no credits movement,
   no audit row, no bytes).
3. **Credits exhausted** → 402 (the route-layer ``require_credits``
   gate fires BEFORE the service-layer pre-deduct, so an exhausted user
   never burns a deduct/refund pair).
4. **Concurrency capped** → 429 + ``Retry-After`` (the second
   in-flight request from the same user lands on a held advisory lock
   and surfaces ``ConcurrencyCappedError``).
5. **Provider rejection** → 422 with structured ``content_rejected``
   body carrying ``reason`` + ``stage`` from the exception context.
6. **Auth missing** → 401 (no ``Authorization`` header trips the
   bearer-token check before any route logic runs).
7. **Provenance-blind GET** — after the happy-path POST, the generated
   bytes are served by the existing
   ``GET /v1/personas/:id/uploads/:ref`` route exactly as if they had
   been uploaded (this is the D-15-X-workspace-coordination invariant
   in binary form: generated images and uploaded images share ONE
   storage-and-serve path).

The tests use a fake :class:`ImageBackend` injected onto
``app.state.image_backend`` so the route layer is exercised without
touching the live OpenAI / fal.ai endpoints; T20's
``@pytest.mark.external`` smoke suite covers the live-provider half.
"""

# ruff: noqa: ANN401, ARG001, ARG002, E501
from __future__ import annotations

import hashlib
import threading
import time
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.imagegen import (
    ContentRejectedError,
    GeneratedImage,
    GenerationResult,
    ImageGenOptions,
    ImageProviderError,
)
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import credit_transactions as credit_tx_t
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import select, text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from persona.imagegen.result import ImageMediaType
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------------
# Test fixtures + fakes
# ---------------------------------------------------------------------------


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


# Minimum-valid 1x1 RGB PNG (mirrors test_uploads + test_imagegen_service).
_TINY_PNG: bytes = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010802000000907753de"
    "0000000c49444154789c63f8cfc0000003010100c9fe92ef0000000049454e44ae"
    "426082"
)


class _HappyBackend:
    """Mock backend that returns deterministic bytes from a fixed list.

    Implements the :class:`persona.imagegen.protocol.ImageBackend`
    Protocol structurally (duck-typed; ``runtime_checkable`` on the
    Protocol verifies this works at runtime).
    """

    def __init__(
        self,
        *,
        image_bytes_list: list[bytes],
        media_type: ImageMediaType = "image/png",
        latency_ms: float = 12.5,
    ) -> None:
        self._image_bytes_list = image_bytes_list
        self._media_type: ImageMediaType = media_type
        self._latency_ms = latency_ms

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model-1"

    async def generate(
        self,
        prompt: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        n = options.count if options is not None else 1
        images = [
            GeneratedImage(
                image_bytes=b,
                workspace_path=None,
                media_type=self._media_type,
                width=1,
                height=1,
                revised_prompt=None,
            )
            for b in self._image_bytes_list[:n]
        ]
        return GenerationResult(
            images=images,
            provider=self.provider_name,
            model=self.model_name,
            latency_ms=self._latency_ms,
        )

    async def edit(
        self,
        input_image: GeneratedImage,
        instructions: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        raise NotImplementedError("edit not supported in v1")


class _RejectingBackend:
    """Mock backend that raises :class:`ContentRejectedError`."""

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model-rejecting"

    async def generate(
        self,
        prompt: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        raise ContentRejectedError(
            "provider rejected prompt",
            context={
                "reason": "provider_moderation",
                "stage": "input",
                "provider": self.provider_name,
                "model": self.model_name,
            },
        )

    async def edit(
        self,
        input_image: GeneratedImage,
        instructions: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        raise NotImplementedError("edit not supported in v1")


class _ProviderErrorBackend:
    """Mock backend that raises :class:`ImageProviderError` (transient)."""

    @property
    def provider_name(self) -> str:
        return "fake"

    @property
    def model_name(self) -> str:
        return "fake-model-bad"

    async def generate(
        self,
        prompt: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        raise ImageProviderError(
            "transient provider 5xx",
            context={
                "reason": "transient",
                "provider": self.provider_name,
                "model": self.model_name,
            },
        )

    async def edit(
        self,
        input_image: GeneratedImage,
        instructions: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        raise NotImplementedError("edit not supported in v1")


_USER_A = "u_imagegen_route_a"
_USER_B = "u_imagegen_route_b"


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema + grants
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, str, str, Path, Engine]]:
    """Real FastAPI client + two seeded users + workspace_root + the admin engine.

    Mirrors the :func:`tests.integration.test_uploads.client` fixture
    shape so the cross-tenant scenarios use the same patterns. Yields
    the client, user_a id, user_b id, workspace_root path, and a
    superuser engine for direct ledger / audit inspection.
    """
    import os

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
        # Drop the lifespan-installed TierRegistry so the persona-detail
        # capabilities surface doesn't lazily instantiate a real chat backend
        # (AuthenticationError("missing API key") on CI without ANTHROPIC_API_KEY).
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        # Default: install a happy backend so the route is wired even
        # when PERSONA_IMAGEGEN_API_KEY is unset in the test env. Per-
        # test overrides replace this with rejecting / failing / None.
        app.state.image_backend = _HappyBackend(image_bytes_list=[_TINY_PNG])

        # Seed both users as superuser (FK target for personas.owner_id +
        # credits.user_id).
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            for u in (_USER_A, _USER_B):
                conn.execute(
                    text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                    {"i": u, "e": f"{u}@x.test"},
                )
        yield c, _USER_A, _USER_B, workspace_root, su
        # Cleanup
        with su.begin() as conn:
            conn.execute(
                text("DELETE FROM users WHERE id IN (:a, :b)"),
                {"a": _USER_A, "b": _USER_B},
            )
        su.dispose()


def _auth(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {user_id}"}


def _create_persona(c: TestClient, user_id: str) -> str:
    resp = c.post("/v1/personas", json={"yaml": _VALID_YAML}, headers=_auth(user_id))
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _audit_actions(engine: Engine, user_id: str) -> list[str]:
    with engine.begin() as conn:
        rows = conn.execute(
            select(audit_log_t.c.action).where(audit_log_t.c.user_id == user_id)
        ).all()
    return [str(r[0]) for r in rows]


def _audit_metadata_for_action(
    engine: Engine, user_id: str, action: str
) -> list[dict[str, object]]:
    with engine.begin() as conn:
        rows = (
            conn.execute(
                select(audit_log_t.c.metadata).where(
                    (audit_log_t.c.user_id == user_id) & (audit_log_t.c.action == action)
                )
            )
            .mappings()
            .all()
        )
    return [dict(r["metadata"]) for r in rows]


def _tx_deltas(engine: Engine, user_id: str) -> list[int]:
    with engine.begin() as conn:
        rows = conn.execute(
            select(credit_tx_t.c.delta)
            .where(credit_tx_t.c.user_id == user_id)
            .order_by(credit_tx_t.c.created_at.asc(), credit_tx_t.c.id.asc())
        ).all()
    return [int(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# Scenario 1: happy path — 201 + ImageRef payload + bytes on disk + audit.
# ---------------------------------------------------------------------------


def test_imagegen_happy_path_returns_imageref_and_persists_bytes(
    client: tuple[TestClient, str, str, Path, Engine],
) -> None:
    """``POST /v1/personas/:id/imagegen`` returns 201 + ImageRef payload; bytes land on disk."""
    c, uid_a, _uid_b, workspace_root, _su = client
    pid = _create_persona(c, uid_a)

    resp = c.post(
        f"/v1/personas/{pid}/imagegen",
        json={"prompt": "a red bicycle", "size": "1024x1024", "count": 1},
        headers=_auth(uid_a),
    )
    assert resp.status_code == 201, resp.text

    body = resp.json()
    assert body["provider"] == "fake"
    assert body["model"] == "fake-model-1"
    assert isinstance(body["latency_ms"], (int, float))
    assert len(body["images"]) == 1

    img = body["images"][0]
    assert img["media_type"] == "image/png"
    assert img["width"] == 1
    assert img["height"] == 1
    assert img["revised_prompt"] is None
    assert img["workspace_path"].startswith("uploads/")
    assert img["workspace_path"].endswith(".png")

    # Bytes on disk at the D-13-4 layout (uploads/<blake2b>.png).
    expected_ref = hashlib.blake2b(_TINY_PNG, digest_size=16).hexdigest()
    expected_path = workspace_root / uid_a / pid / "uploads" / f"{expected_ref}.png"
    assert expected_path.is_file()
    assert expected_path.read_bytes() == _TINY_PNG


def test_imagegen_happy_path_audits_requested_size_not_rounded(
    client: tuple[TestClient, str, str, Path, Engine],
) -> None:
    """The API-layer audit row carries the REQUESTED size per D-15-X-size-rounding."""
    c, uid_a, _uid_b, _ws, su = client
    pid = _create_persona(c, uid_a)

    # Use a non-square preset; the OpenAI backend would round it to
    # 1024x1536, but the audit must capture the user-supplied value so
    # the operator sees what the user asked for, not the wire value.
    resp = c.post(
        f"/v1/personas/{pid}/imagegen",
        json={"prompt": "a portrait", "size": "1024x1792", "count": 1},
        headers=_auth(uid_a),
    )
    assert resp.status_code == 201, resp.text

    rows = _audit_metadata_for_action(su, uid_a, "imagegen.create")
    assert len(rows) == 1, f"expected one imagegen.create audit row, got {len(rows)}"
    metadata = rows[0]
    assert metadata["requested_size"] == "1024x1792"
    assert metadata["provider"] == "fake"
    assert metadata["model"] == "fake-model-1"
    assert metadata["count"] == "1"
    # latency_ms is stringified for the dict[str, str] audit metadata
    # shape; the value is whatever the backend reported (12.5 in our fake).
    assert "latency_ms" in metadata


# ---------------------------------------------------------------------------
# Scenario 2: cross-tenant → 404 (no credits movement, no audit, no bytes).
# ---------------------------------------------------------------------------


def test_imagegen_cross_tenant_returns_404(
    client: tuple[TestClient, str, str, Path, Engine],
) -> None:
    """User B → user A's persona id returns 404 (existence-disclosure-safe).

    Requires the non-superuser ``persona_app`` role configured against
    APP_DATABASE_URL (RLS bypasses superusers per D-07-5). When the test
    env only has the ``persona`` superuser, RLS is structurally bypassed
    and the test cannot assert isolation — skip cleanly rather than
    surface a misleading failure.
    """
    import os

    app_url = os.environ.get("APP_DATABASE_URL", "")
    if "persona_app" not in app_url:
        pytest.skip(
            "APP_DATABASE_URL is not using the non-superuser persona_app role;"
            " RLS isolation cannot be verified (D-07-5)"
        )
    c, uid_a, uid_b, workspace_root, su = client
    pid = _create_persona(c, uid_a)

    resp = c.post(
        f"/v1/personas/{pid}/imagegen",
        json={"prompt": "anything", "size": "1024x1024", "count": 1},
        headers=_auth(uid_b),
    )
    assert resp.status_code == 404

    # No credits movement for user B (the persona pre-flight 404 fires
    # before require_credits, so no row is even created in credits_t).
    deltas = _tx_deltas(su, uid_b)
    assert deltas == [], f"expected no deltas for user B, got {deltas}"

    # No audit row for user B against the imagegen action.
    assert "imagegen.create" not in _audit_actions(su, uid_b)

    # No bytes anywhere under user B's workspace subtree.
    user_b_workspace = workspace_root / uid_b
    if user_b_workspace.exists():
        files = list(user_b_workspace.rglob("*"))
        assert files == [], f"expected no bytes for user B, got {files}"


# ---------------------------------------------------------------------------
# Scenario 3: credits exhausted → 402 (no deduct, no refund, no bytes).
# ---------------------------------------------------------------------------


def test_imagegen_credits_exhausted_returns_402(
    client: tuple[TestClient, str, str, Path, Engine],
) -> None:
    """Exhausted credits trip the route-level pre-flight gate → 402."""
    c, uid_a, _uid_b, _ws, su = client
    pid = _create_persona(c, uid_a)

    # Force the user's balance to 0. ``ensure_balance`` would otherwise
    # create the row with the default 100,000 — so we INSERT the row
    # with balance=0 here directly (the route's ``require_credits`` runs
    # ``ensure_balance`` which is a no-op when the row exists).
    with su.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO credits (user_id, balance) VALUES (:i, 0)"
                " ON CONFLICT (user_id) DO UPDATE SET balance = 0"
            ),
            {"i": uid_a},
        )

    # The deltas before the call (from any prior persona-related deducts).
    deltas_before = _tx_deltas(su, uid_a)

    resp = c.post(
        f"/v1/personas/{pid}/imagegen",
        json={"prompt": "a red bicycle"},
        headers=_auth(uid_a),
    )
    assert resp.status_code == 402, resp.text

    # No new deltas — the route-level gate fires BEFORE the service-
    # layer pre-deduct, so no deduct/refund pair is written.
    deltas_after = _tx_deltas(su, uid_a)
    assert deltas_after == deltas_before, (
        f"expected no new deltas, got before={deltas_before} after={deltas_after}"
    )


# ---------------------------------------------------------------------------
# Scenario 4: concurrency capped → 429 + Retry-After.
# ---------------------------------------------------------------------------


def test_imagegen_concurrency_capped_returns_429(
    client: tuple[TestClient, str, str, Path, Engine],
) -> None:
    """A held advisory lock on the same user → second call returns 429 + Retry-After.

    Acquires the per-user advisory lock on a separate connection (held
    for the duration of the test) before the route call fires, then
    verifies the route surfaces ``ConcurrencyCappedError`` → 429 via the
    app-level handler with the ``Retry-After`` header set.
    """
    c, uid_a, _uid_b, _ws, _su = client
    pid = _create_persona(c, uid_a)

    rls_engine: Engine = c.app.state.rls_engine

    # Pre-acquire the user's advisory lock on a separate connection so
    # the route's call to ``acquire_user_concurrency`` finds it held.
    # The connection stays open for the duration of the route call;
    # commit/rollback after we've observed the 429 to release.
    conn = rls_engine.connect()
    try:
        # Manual transaction so the lock is held throughout.
        tx = conn.begin()
        # Reuse the same SQL the helper uses so the lock key matches
        # bit-for-bit (md5 of user_id, hex → bit(64) → bigint).
        conn.execute(
            text("SELECT pg_try_advisory_xact_lock(('x' || md5(:user_id))::bit(64)::bigint)"),
            {"user_id": uid_a},
        )

        resp = c.post(
            f"/v1/personas/{pid}/imagegen",
            json={"prompt": "a red bicycle"},
            headers=_auth(uid_a),
        )
        assert resp.status_code == 429, resp.text
        assert resp.headers.get("Retry-After") is not None
        body = resp.json()
        assert body["error"] == "concurrency_capped"

        tx.rollback()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Scenario 5: provider rejection → 422 with structured body.
# ---------------------------------------------------------------------------


def test_imagegen_provider_rejection_returns_422(
    client: tuple[TestClient, str, str, Path, Engine],
) -> None:
    """Provider moderation rejection surfaces as 422 with structured body."""
    c, uid_a, _uid_b, workspace_root, su = client
    # Spec 29: persona-create now auto-generates an avatar via the wired
    # image_backend, which would land a file in the persona's uploads dir and
    # break the "no files on disk" assertion below. Set the rejecting backend
    # BEFORE create so the create-time avatar gen also rejects (fail-soft to
    # null, free → no ledger movement, no bytes) — leaving the workspace
    # genuinely empty so the assertion is about the rejected imagegen POST, not
    # the create-time avatar. The rejecting backend also rejects the create-time
    # avatar (fail-soft → null, no billing on the rejected path), so the ledger
    # deduct/refund pair below is the imagegen POST's alone.
    c.app.state.image_backend = _RejectingBackend()
    pid = _create_persona(c, uid_a)

    resp = c.post(
        f"/v1/personas/{pid}/imagegen",
        json={"prompt": "anything", "size": "1024x1024", "count": 1},
        headers=_auth(uid_a),
    )
    assert resp.status_code == 422, resp.text

    body = resp.json()
    # Structured body shape: {"detail": {"error": "content_rejected", ...}}
    detail = body["detail"]
    assert detail["error"] == "content_rejected"
    assert detail["context"]["reason"] == "provider_moderation"
    assert detail["context"]["stage"] == "input"

    # Spec M3 (T3a): refund-on-full-failure landed — the service pre-deducted the
    # CEILING (50 credits/image, count=1) then refunded it in full before
    # propagating the exception (net-zero; no true-up on the failure path).
    deltas = _tx_deltas(su, uid_a)
    assert -50 in deltas, f"expected ceiling deduct entry, got {deltas}"
    assert 50 in deltas, f"expected refund entry, got {deltas}"

    # No bytes on disk — the failure branch persists nothing.
    user_ws = workspace_root / uid_a / pid / "uploads"
    if user_ws.exists():
        assert list(user_ws.iterdir()) == [], (
            "no files must land when the provider rejected the prompt"
        )


def test_imagegen_provider_transient_error_returns_502(
    client: tuple[TestClient, str, str, Path, Engine],
) -> None:
    """:class:`ImageProviderError` (non-moderation) surfaces as 502."""
    c, uid_a, _uid_b, _ws, su = client
    pid = _create_persona(c, uid_a)

    c.app.state.image_backend = _ProviderErrorBackend()

    resp = c.post(
        f"/v1/personas/{pid}/imagegen",
        json={"prompt": "anything"},
        headers=_auth(uid_a),
    )
    assert resp.status_code == 502, resp.text

    body = resp.json()
    detail = body["detail"]
    assert detail["error"] == "provider_error"
    assert detail["context"]["reason"] == "transient"

    # Spec M3 (T3a): refund-on-full-failure — the ceiling pre-deduct (50) is
    # refunded in full before the exception propagates (no true-up on failure).
    deltas = _tx_deltas(su, uid_a)
    assert -50 in deltas, f"expected ceiling deduct entry, got {deltas}"
    assert 50 in deltas, f"expected refund entry, got {deltas}"


# ---------------------------------------------------------------------------
# Scenario 6: auth missing → 401.
# ---------------------------------------------------------------------------


def test_imagegen_auth_missing_returns_401(
    client: tuple[TestClient, str, str, Path, Engine],
) -> None:
    """No Authorization header → 401 before any route logic runs."""
    c, _uid_a, _uid_b, _ws, _su = client
    # Even a real persona id is irrelevant — the bearer check fires first.
    resp = c.post(
        "/v1/personas/p_does_not_matter/imagegen",
        json={"prompt": "anything"},
    )
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Scenario 7: backend unconfigured → 503.
# ---------------------------------------------------------------------------


def test_imagegen_backend_unconfigured_returns_503(
    client: tuple[TestClient, str, str, Path, Engine],
) -> None:
    """``app.state.image_backend is None`` → 503 ImageGenUnavailableError."""
    c, uid_a, _uid_b, _ws, _su = client
    pid = _create_persona(c, uid_a)

    # Simulate a deployment without PERSONA_IMAGEGEN_API_KEY: the
    # lifespan would set image_backend to None and log a warning.
    c.app.state.image_backend = None

    resp = c.post(
        f"/v1/personas/{pid}/imagegen",
        json={"prompt": "anything"},
        headers=_auth(uid_a),
    )
    # ImageGenUnavailableError → 503 via the registered handler in
    # :mod:`persona_api.errors` (T16 wiring). The route raises the
    # exception directly; the handler maps it to a 503 with structured
    # body + Retry-After header.
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"] == "imagegen_unavailable"
    assert resp.headers.get("Retry-After") is not None


# ---------------------------------------------------------------------------
# Scenario 8 (provenance-blind verification): subsequent GET serves bytes.
# ---------------------------------------------------------------------------


def test_imagegen_generated_bytes_served_by_uploads_route(
    client: tuple[TestClient, str, str, Path, Engine],
) -> None:
    """Generated images are served by the existing ``GET /uploads/:ref`` route.

    The D-15-X-workspace-coordination invariant in binary form: generated
    images and uploaded images share ONE storage-and-serve path. After the
    POST /imagegen lands bytes at ``uploads/<ref>.png``, a GET
    ``/v1/personas/:id/uploads/:ref`` returns the same bytes the backend
    produced — no provenance branching at the GET site.
    """
    c, uid_a, _uid_b, _ws, _su = client
    pid = _create_persona(c, uid_a)

    resp = c.post(
        f"/v1/personas/{pid}/imagegen",
        json={"prompt": "a red bicycle"},
        headers=_auth(uid_a),
    )
    assert resp.status_code == 201, resp.text
    ref = resp.json()["images"][0]["workspace_path"]

    # GET the ref through the existing uploads route.
    get_resp = c.get(f"/v1/personas/{pid}/{ref}", headers=_auth(uid_a))
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.headers["content-type"].startswith("image/png")
    assert get_resp.content == _TINY_PNG


# ---------------------------------------------------------------------------
# Scenario 9 (validation): invalid count → 422.
# ---------------------------------------------------------------------------


def test_imagegen_count_above_cap_returns_422(
    client: tuple[TestClient, str, str, Path, Engine],
) -> None:
    """``count=3`` (above the D-15-3 ``le=2`` cap) → 422 Pydantic error."""
    c, uid_a, _uid_b, _ws, _su = client
    pid = _create_persona(c, uid_a)

    resp = c.post(
        f"/v1/personas/{pid}/imagegen",
        json={"prompt": "anything", "count": 3},
        headers=_auth(uid_a),
    )
    assert resp.status_code == 422


def test_imagegen_unknown_size_returns_422(
    client: tuple[TestClient, str, str, Path, Engine],
) -> None:
    """Unknown size preset (closed Literal surface) → 422 Pydantic error."""
    c, uid_a, _uid_b, _ws, _su = client
    pid = _create_persona(c, uid_a)

    resp = c.post(
        f"/v1/personas/{pid}/imagegen",
        json={"prompt": "anything", "size": "999x999"},
        headers=_auth(uid_a),
    )
    assert resp.status_code == 422


def test_imagegen_empty_prompt_returns_422(
    client: tuple[TestClient, str, str, Path, Engine],
) -> None:
    """Empty prompt (Field(min_length=1)) → 422 Pydantic error."""
    c, uid_a, _uid_b, _ws, _su = client
    pid = _create_persona(c, uid_a)

    resp = c.post(
        f"/v1/personas/{pid}/imagegen",
        json={"prompt": ""},
        headers=_auth(uid_a),
    )
    assert resp.status_code == 422


# Avoid an unused-import lint warning for ``threading``/``time`` — they
# are kept available for future timing-sensitive tests (parallel-fire
# proper test lives in T17).
_ = threading
_ = time
