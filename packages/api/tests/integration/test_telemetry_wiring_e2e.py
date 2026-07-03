"""R5-D-3 end-to-end wiring proof: a real request lands a request_telemetry row.

The unit tests prove the middleware captures the template and the buffer is
fail-soft/drop-oldest. This proves the LIFESPAN wiring: booting the whole app
builds + starts the telemetry buffer, the ``add_middleware`` records into it, and
the shutdown ``aclose`` flushes it to Postgres. A request to ``/livez`` (DB-free)
must therefore leave exactly one ``request_telemetry`` row with the route template
— proving telemetry reaches production, not just the unit harness.
"""

from __future__ import annotations

# ruff: noqa: ARG001 — `migrated_engine` is a schema-at-head fixture dependency.
import os
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.config import APIConfig
from persona_api.db.models import request_telemetry
from sqlalchemy import create_engine, func, select

if TYPE_CHECKING:
    from pathlib import Path

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration


@pytest.fixture
def telemetry_client(
    migrated_engine: Engine,
    tmp_path: Path,
) -> tuple[TestClient, str]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),
        workspace_root=tmp_path / "workspace",
        telemetry_enabled=True,
    )
    app = create_app(cfg)
    return TestClient(app), os.environ["DATABASE_URL"]


def test_livez_request_lands_a_telemetry_row(
    telemetry_client: tuple[TestClient, str],
) -> None:
    client, db_url = telemetry_client
    reader = create_engine(db_url)
    try:
        with reader.begin() as conn:
            conn.execute(request_telemetry.delete())  # isolate this test's rows
        # The TestClient context runs lifespan startup AND shutdown; shutdown's
        # buffer.aclose() does the final flush, so query AFTER the `with` block.
        with client as c:
            assert c.get("/livez").status_code == 200
        with reader.begin() as conn:
            rows = conn.execute(
                select(
                    request_telemetry.c.route_template,
                    request_telemetry.c.method,
                    request_telemetry.c.status_code,
                ).where(request_telemetry.c.route_template == "/livez")
            ).all()
            total = conn.execute(select(func.count()).select_from(request_telemetry)).scalar_one()
    finally:
        reader.dispose()

    assert total >= 1, "no telemetry row flushed by the lifespan buffer"
    assert ("/livez", "GET", 200) in [tuple(r) for r in rows]
