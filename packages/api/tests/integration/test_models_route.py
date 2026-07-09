"""GET /v1/models — the M1 T5 model catalog route (auth + shape + fail-open).

Drives the real app (full lifespan, real Postgres via ``migrated_engine``) with a
fake JWT verifier — the same DB-backed ``TestClient`` pattern as
``test_calls_api.py``. This route serves platform-global catalog data (not
RLS-scoped, no seeding needed); the DB is required only because
``get_current_user`` -> ``CloudOwnerResolver.resolve`` JIT-provisions the caller's
``users`` row on the real admin engine (same as every other authenticated route).

No network: ``PERSONA_OPENROUTER_API_KEY`` is force-cleared and the module-scoped
catalog-client cache is reset around every test — regardless of what the ambient
shell / ``.env`` happens to have configured — so this suite deterministically
exercises the "no client configured" fail-open path and never makes a real
OpenRouter call (the plan's Global Constraint: "OpenRouterCatalogClient is never
allowed to fetch in CI"). The populated / priced happy path is unit-tested with a
fake client in ``tests/unit/services/test_model_catalog_service.py``; this file's
job is the HTTP-layer contract — auth, query-param validation, response shape,
and the fail-open envelope.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.services import model_catalog_service

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_FAIL_OPEN_BODY = {"models": [], "source": "openrouter", "stale": True}


@pytest.fixture(autouse=True)
def _no_real_openrouter_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Force the "not configured" fail-open path regardless of the ambient env.

    Guarantees this suite never makes a real network call even if a developer's
    shell / ``.env`` happens to export a real ``PERSONA_OPENROUTER_API_KEY``.
    """
    monkeypatch.delenv("PERSONA_OPENROUTER_API_KEY", raising=False)
    model_catalog_service._default_client.cache_clear()  # noqa: SLF001 — force env re-read
    yield
    model_catalog_service._default_client.cache_clear()  # noqa: SLF001 — don't leak into other tests


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema + persona_app grants
    embedder: HashEmbedder384,  # noqa: ARG001 — app lifespan wants an embedder
    tmp_path: Path,
) -> Iterator[TestClient]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(app_database_url=app_url, audit_root=str(tmp_path / "audit"))
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)  # token == user_id

    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        yield c


def _auth(uid: str = "user_models_route") -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def test_requires_auth(client: TestClient) -> None:
    assert client.get("/v1/models").status_code == 401


@pytest.mark.parametrize("params", [{}, {"scope": "recommended"}, {"scope": "all"}])
def test_fail_open_shape_for_every_scope(client: TestClient, params: dict[str, str]) -> None:
    """No OpenRouter key configured -> fail-open: 200, stale=True, empty models.

    Covers the default (omitted) scope plus both explicit values — none of them
    ever raise or 500; the envelope shape is identical because there is nothing
    to differentiate without a live catalog.
    """
    resp = client.get("/v1/models", headers=_auth(), params=params)
    assert resp.status_code == 200
    assert resp.json() == _FAIL_OPEN_BODY


def test_invalid_scope_is_422_not_500(client: TestClient) -> None:
    """An out-of-contract ``scope`` value is a clean validation error, not a crash."""
    resp = client.get("/v1/models", headers=_auth(), params={"scope": "bogus"})
    assert resp.status_code == 422


def test_openapi_schema_matches_the_t7_contract(client: TestClient) -> None:
    """The wire shape T7's web client depends on, pinned at the OpenAPI level.

    Exact field-name coverage independent of whether the catalog happens to be
    populated in this run (this suite is deliberately keyless/fail-open, see the
    module docstring) — the schema is generated from the route's Pydantic models
    regardless of runtime data.
    """
    schema = client.get("/openapi.json").json()
    model_out = schema["components"]["schemas"]["ModelOut"]["properties"]
    assert set(model_out) == {
        "id",
        "label",
        "provider",
        "input_price_per_1m",
        "output_price_per_1m",
        "context_length",
        "tools_supported",
        "recommended",
    }
    models_response = schema["components"]["schemas"]["ModelsResponse"]["properties"]
    assert set(models_response) == {"models", "source", "stale"}
