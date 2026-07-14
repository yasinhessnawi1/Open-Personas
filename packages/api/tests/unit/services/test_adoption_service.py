"""Unit tests — N7-T3a: the catalog-oauth adoption branch (no live DB).

``_adopt_remote_app``'s oauth branch is exercised directly (the DB-hitting
``mcp_store.create_server``/``assign_to_persona`` calls are monkeypatched out) so the
branch logic — no credential ever forwarded, the catalog's own ``oauth_provider``
passed through, ``auth_method="oauth"`` — is pinned without Postgres. The full
security spine (owner-scoped authz, vetted gate, double-adopt) is already covered at
the service layer by the N4 integration suite; the real synthetic-AS round-trip
(adopt → authorize → callback → decrypted header) is proven at the api integration
layer (test_mcp_oauth_catalog_adoption.py, :5436).
"""

from __future__ import annotations

from typing import Any

import pytest
from persona.tools.mcp.catalog import MCPCatalog, MCPServerCatalogEntry
from persona_api.config import APIConfig, Edition
from persona_api.mcp import store as mcp_store
from persona_api.services import adoption_service


def _oauth_entry(*, oauth_provider: str = "github") -> MCPServerCatalogEntry:
    return MCPServerCatalogEntry(
        name="github",
        description="",
        kind="external",
        risk="low",
        server_type="remote",
        remote_url="https://api.githubcopilot.com/mcp/",
        auth_method="oauth",
        oauth_provider=oauth_provider,
    )


def _bearer_entry() -> MCPServerCatalogEntry:
    from persona.tools.mcp.catalog import MCPSecretField

    return MCPServerCatalogEntry(
        name="notion-remote",
        description="",
        kind="external",
        risk="low",
        server_type="remote",
        remote_url="https://mcp.notion.com/mcp",
        secrets=(MCPSecretField(name="notion.token", env="NOTION_TOKEN"),),
    )


def test_adopt_remote_app_oauth_branch_writes_no_credential(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry = _oauth_entry()
    catalog = MCPCatalog(servers={entry.name: entry})
    calls: dict[str, Any] = {}

    def fake_create_server(**kwargs: object) -> dict[str, Any]:
        calls.update(kwargs)
        return {"id": "srv_1", "catalog_source": entry.name}

    def fake_assign_to_persona(**kwargs: object) -> None:
        calls["assign"] = kwargs

    monkeypatch.setattr(mcp_store, "create_server", fake_create_server)
    monkeypatch.setattr(mcp_store, "assign_to_persona", fake_assign_to_persona)

    sentinel_engine = object()
    detail = adoption_service._adopt_remote_app(
        rls_engine=sentinel_engine,  # type: ignore[arg-type] — never touched, both calls monkeypatched
        config=APIConfig(edition=Edition.community),
        owner_id="u1",
        persona_id="p1",
        entry=entry,
        catalog=catalog,
        # The caller-supplied credential must NEVER reach the oauth branch's write.
        credential="a-token-the-caller-should-never-be-able-to-smuggle-in",  # noqa: S106
    )

    assert detail == {"id": "srv_1", "catalog_source": entry.name}
    assert calls["auth_method"] == "oauth"
    assert calls["credential"] is None
    assert calls["oauth_provider"] == "github"
    assert calls["url"] == "https://api.githubcopilot.com/mcp/"
    assert calls["catalog_source"] == "github"
    assert calls["assign"] == {
        "rls_engine": sentinel_engine,
        "persona_id": "p1",
        "server_id": "srv_1",
    }


def test_adopt_remote_app_bearer_branch_is_unaffected(monkeypatch: pytest.MonkeyPatch) -> None:
    # Non-oauth entries take the pre-existing derive path byte-identically (refactor proof).
    entry = _bearer_entry()
    catalog = MCPCatalog(servers={entry.name: entry})
    calls: dict[str, Any] = {}

    def fake_create_server(**kwargs: object) -> dict[str, Any]:
        calls.update(kwargs)
        return {"id": "srv_2", "catalog_source": entry.name}

    monkeypatch.setattr(mcp_store, "create_server", fake_create_server)
    monkeypatch.setattr(mcp_store, "assign_to_persona", lambda **_kw: None)

    adoption_service._adopt_remote_app(
        rls_engine=object(),  # type: ignore[arg-type]
        config=APIConfig(edition=Edition.community),
        owner_id="u1",
        persona_id="p1",
        entry=entry,
        catalog=catalog,
        credential="notion-secret-token",  # noqa: S106
    )

    assert calls["auth_method"] == "bearer"
    assert calls["credential"] == "notion-secret-token"  # noqa: S105
    assert "oauth_provider" not in calls  # kwarg omitted on this branch, as before
