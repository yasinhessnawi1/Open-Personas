"""Integration — image-app adoption: assign-time cap + vetting re-check (Spec N6, T5b).

Proves the two T5b security conditions at the assign boundary:

- **assign-time cap (N6-D-3):** the (N+1)th image enable is denied with
  ``MCPRuntimeCapacityError`` BEFORE any row is written — never a silent spawn-storm;
- **vetting re-checked at assign (N6-D-4 TOCTOU):** an image not in the runnable set is
  refused (``MCPAppNotAdoptableError``), nothing written — fail-closed.

Needs Postgres + ``persona_app`` (``APP_DATABASE_URL``); ``integration``-marked.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from cryptography.fernet import Fernet
from persona.tools.mcp.catalog import MCPServerCatalogEntry
from persona_api.config import APIConfig, Edition
from persona_api.errors import MCPAppNotAdoptableError, MCPRuntimeCapacityError
from persona_api.mcp import store as mcp_store
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import adoption_service, catalog_service
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration

_KEY = Fernet.generate_key().decode()


def _image_entry(name: str) -> MCPServerCatalogEntry:
    return MCPServerCatalogEntry(
        name=name,
        description="x",
        kind="external",
        risk="medium",
        server_type="server",
        image=f"mcp/{name}",
        source_commit="abc123",
    )


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    _ = migrated_engine
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url)
    yield engine
    engine.dispose()


@contextmanager
def _acting_as(owner: str) -> Iterator[None]:
    token = current_user_id.set(owner)
    try:
        yield
    finally:
        current_user_id.reset(token)


def _seed(superuser: Engine, *, owner: str, persona_id: str) -> None:
    with superuser.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, :e) ON CONFLICT DO NOTHING"),
            {"o": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x')"),
            {"p": persona_id, "o": owner},
        )


def test_cap_denies_the_n_plus_1th_enable_at_assign(
    app_engine: Engine, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(migrated_engine, owner="owner_a", persona_id="p1")
    monkeypatch.setattr(
        catalog_service,
        "merged_mcp_catalog",
        lambda: [_image_entry("flights1"), _image_entry("flights2"), _image_entry("flights3")],
    )
    # Community so vetting passes (basis-only); cap = 2.
    config = APIConfig(mcp_credential_key=_KEY, edition=Edition.community)
    with _acting_as("owner_a"):
        for name in ("flights1", "flights2"):
            adoption_service.adopt_catalog_app(
                rls_engine=app_engine,
                config=config,
                owner_id="owner_a",
                persona_id="p1",
                catalog_name=name,
                credential=None,
                max_image_runtime=2,
            )
        # The 3rd (the (N+1)th) is denied AT ASSIGN.
        with pytest.raises(MCPRuntimeCapacityError) as ei:
            adoption_service.adopt_catalog_app(
                rls_engine=app_engine,
                config=config,
                owner_id="owner_a",
                persona_id="p1",
                catalog_name="flights3",
                credential=None,
                max_image_runtime=2,
            )
        assert ei.value.context.get("reason") == "runtime_capacity"
        # Nothing written for the 3rd — exactly the cap count remains (no spawn-storm).
        assert (
            mcp_store.count_image_servers_for_owner(rls_engine=app_engine, owner_id="owner_a") == 2
        )


def test_unvetted_image_is_refused_at_assign_nothing_written(
    app_engine: Engine, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(migrated_engine, owner="owner_a", persona_id="p1")
    monkeypatch.setattr(catalog_service, "merged_mcp_catalog", lambda: [_image_entry("flights1")])
    # Cloud with an EMPTY runnable allow-list → deny-all (fail-closed, the TOCTOU re-check).
    config = APIConfig(mcp_credential_key=_KEY, edition=Edition.cloud, mcp_run_vetted="")
    with _acting_as("owner_a"), pytest.raises(MCPAppNotAdoptableError):
        adoption_service.adopt_catalog_app(
            rls_engine=app_engine,
            config=config,
            owner_id="owner_a",
            persona_id="p1",
            catalog_name="flights1",
            credential=None,
        )
    with _acting_as("owner_a"):
        assert (
            mcp_store.count_image_servers_for_owner(rls_engine=app_engine, owner_id="owner_a") == 0
        )
