"""Cloud-boot engine selection for the embedded connectors (Spec I1 T2, D-I1-18).

Lives in the integration leg because a cloud boot PROBES its RLS engine for real: the
R2-D-1 guard refuses to serve if the request-path role is a superuser, so this needs a
genuine non-superuser DSN rather than a fake one. That probe is also what makes the fold
safe by construction, so exercising it here is the point rather than an inconvenience.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from loguru import logger as _loguru_logger
from persona_api.app import create_app
from persona_api.config import APIConfig, Edition

if TYPE_CHECKING:
    from pathlib import Path

    from persona.stores.embedder import Embedder

pytestmark = pytest.mark.integration


@pytest.fixture
def loguru_warnings() -> Iterator[list[str]]:
    """Capture WARNING+ through loguru (persona.logging wraps it; caplog sees nothing)."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


def test_cloud_refuses_to_embed_without_a_real_cross_tenant_dispatch_engine(
    tmp_path: Path,
    loguru_warnings: list[str],
    monkeypatch: pytest.MonkeyPatch,
    embedder: Embedder,
) -> None:
    """D-I1-18's other half: the two engines have DIFFERENT jobs, and cloud cannot share one.

    Connector inbound arrives from an unauthenticated platform identity, so resolving
    ``(platform, sender_id)`` to an owner and redeeming a link token are reads that precede
    any owner scope: they need the cross-tenant engine. In community that is legitimately
    the same engine (single owner, RLS inert). In cloud, silently falling back to the
    ``persona_app`` engine would run those pre-auth reads RLS-scoped with no owner set, so
    every inbound would resolve to nothing and the connectors would sit there doing nothing.
    Fail-closed rather than leaky, but silent, which is the failure this refuses.
    """
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (the non-superuser persona_app role) not set")
    from persona_api.services import persona_service

    monkeypatch.setattr(persona_service, "default_embedder", lambda *_a, **_k: embedder)
    config = APIConfig(
        edition=Edition.cloud,
        embed_connectors=True,
        # A REAL persona_app DSN, so the R2-D-1 non-superuser probe genuinely runs and
        # passes. No superuser DSN, so there is no cross-tenant engine to dispatch on.
        app_database_url=app_url.replace("+asyncpg", "+psycopg"),
        database_url="",
        workspace_root=tmp_path / "work",
        audit_root=str(tmp_path / "audit"),
        jwt_audience="persona-api",
        jwt_public_key="unused-in-this-boot",
    )
    app = create_app(config)
    with TestClient(app) as client:
        assert client.app.state.embedded_connectors is None
    assert any("cross_tenant_dispatch_engine=False" in line for line in loguru_warnings), (
        f"cloud must refuse loudly rather than share one engine. Captured: {loguru_warnings}"
    )
