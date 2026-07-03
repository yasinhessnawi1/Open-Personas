"""R5-D-2 end-to-end wiring proof: backend=postgres reaches a real store mutation.

The unit + concurrency tests prove the factory selects the right logger and the
Postgres logger is concurrency-safe. This closes the loop the
synthetic-harness-must-drive-the-real-transition rule demands: boot the WHOLE app
with ``PERSONA_API_AUDIT_BACKEND=postgres``, create a persona through the real
HTTP route, and assert the store-mutation audit landed in ``store_audit_events``
(NOT a JSONL file). That exercises the actual thread: lifespan builds
``app.state.audit_logger`` → the route passes it to ``persona_service`` → the
typed stores emit through it. A JSONL-wired build would leave the table empty.
"""

from __future__ import annotations

# ruff: noqa: ARG001 — `migrated_engine` is a schema-at-head fixture dependency.
import os
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.db.models import store_audit_events
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import func, select, text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_USER = "u_r5_audit_wiring"
_VALID_YAML = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: Norwegian tenancy law assistant
  background: |
    Helps tenants understand husleieloven.
  language_default: en
  constraints: []
self_facts:
  - fact: I help tenants understand husleieloven.
"""


@pytest.fixture
def pg_backed_client(
    migrated_engine: Engine,
    embedder: HashEmbedder384,
    tmp_path: Path,
) -> Iterator[tuple[TestClient, Engine]]:
    """The app booted with the POSTGRES audit backend + a real Postgres."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")

    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),  # JSONL fallback root — must stay empty
        workspace_root=tmp_path / "workspace",
        audit_backend="postgres",  # R5-D-2: select the multi-worker-safe backend
    )
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        app.state.embedder = embedder
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": _USER, "e": f"{_USER}@x.test"},
            )
        yield c, su
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": _USER})
        su.dispose()


def test_persona_create_writes_store_audit_to_postgres(
    pg_backed_client: tuple[TestClient, Engine],
    tmp_path: Path,
) -> None:
    c, su = pg_backed_client
    resp = c.post(
        "/v1/personas", json={"yaml": _VALID_YAML}, headers={"Authorization": f"Bearer {_USER}"}
    )
    assert resp.status_code == 201, resp.text
    persona_id = resp.json()["id"]

    # The self_facts write (initial persona load — identity uses a privileged
    # audit-bypassing path, so the YAML carries a self_fact to force a real
    # ``store.write``) emitted a store-mutation AuditEvent. It must land in the
    # Postgres table (the multi-worker-safe path), routed there by app.state wiring.
    with su.begin() as conn:
        rows = conn.execute(
            select(func.count())
            .select_from(store_audit_events)
            .where(store_audit_events.c.persona_id == persona_id)
        ).scalar_one()
    assert rows >= 1, "persona-create store mutation did not reach store_audit_events"

    # And the JSONL fallback file was NOT written — proving the backend actually
    # switched (a byte-unchanged JSONL build would have created it here).
    jsonl_path = tmp_path / "audit" / f"{persona_id}.jsonl"
    assert not jsonl_path.exists(), "JSONL file written despite backend=postgres"
