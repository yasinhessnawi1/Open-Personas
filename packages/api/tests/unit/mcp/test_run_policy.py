"""Unit — the runnable-image vetting policy + cloud ack guard (Spec N6, T4, N6-D-4/5).

Proves: the namespace+provenance basis, community-open vs cloud-allowlisted, the empty
allow-list = deny-all (fail-closed) default, and the ``PERSONA_ALLOW_PER_TENANT_MCP`` ack
(cloud refuses without it; community byte-unchanged).
"""

from __future__ import annotations

import pytest
from persona.tools.mcp.catalog import MCPCatalog, MCPServerCatalogEntry
from persona_api.config import APIConfig, Edition
from persona_api.editions.per_tenant_mcp_guard import check_per_tenant_mcp_posture
from persona_api.errors import PerTenantMCPNotAckedError
from persona_api.mcp.run_policy import is_runnable, runnable_catalog_names, runnable_images


def _entry(
    name: str, *, image: str, server_type: str = "server", commit: str = "abc123"
) -> MCPServerCatalogEntry:
    return MCPServerCatalogEntry(
        name=name,
        description="x",
        kind="external",
        risk="medium",
        server_type=server_type,  # type: ignore[arg-type]
        image=image,
        source_commit=commit,
    )


def _catalog() -> MCPCatalog:
    return MCPCatalog(
        servers={
            "google-flights": _entry("google-flights", image="mcp/google-flights"),
            "third-party": _entry("third-party", image="ghcr.io/someone/thing"),
            "no-provenance": _entry("no-provenance", image="mcp/thing", commit=""),
            "a-remote": _entry("a-remote", image="", server_type="remote"),
        }
    )


class TestBasis:
    def test_official_namespace_with_provenance_meets_basis(self) -> None:
        names = runnable_catalog_names(edition=Edition.community, vetted=[], catalog=_catalog())
        assert "google-flights" in names

    def test_non_mcp_namespace_is_excluded(self) -> None:
        names = runnable_catalog_names(edition=Edition.community, vetted=[], catalog=_catalog())
        assert "third-party" not in names  # ghcr.io/... is not the official mcp/ namespace

    def test_missing_provenance_is_excluded(self) -> None:
        names = runnable_catalog_names(edition=Edition.community, vetted=[], catalog=_catalog())
        assert "no-provenance" not in names  # no source_commit

    def test_remote_entry_is_excluded(self) -> None:
        names = runnable_catalog_names(edition=Edition.community, vetted=[], catalog=_catalog())
        assert "a-remote" not in names  # remote, not an image server


class TestEditionGating:
    def test_cloud_empty_allowlist_is_deny_all(self) -> None:
        names = runnable_catalog_names(edition=Edition.cloud, vetted=[], catalog=_catalog())
        assert names == frozenset()  # fail-closed default

    def test_cloud_honors_the_operator_allowlist(self) -> None:
        names = runnable_catalog_names(
            edition=Edition.cloud, vetted=["google-flights"], catalog=_catalog()
        )
        assert names == {"google-flights"}

    def test_cloud_allowlist_still_requires_the_basis(self) -> None:
        # Vetting a non-basis name (remote / wrong namespace) does NOT make it runnable.
        names = runnable_catalog_names(
            edition=Edition.cloud, vetted=["third-party", "a-remote"], catalog=_catalog()
        )
        assert names == frozenset()

    def test_is_runnable_and_runnable_images_agree(self) -> None:
        cat = _catalog()
        assert is_runnable(
            "google-flights", edition=Edition.cloud, vetted=["google-flights"], catalog=cat
        )
        imgs = runnable_images(edition=Edition.cloud, vetted=["google-flights"], catalog=cat)
        assert imgs == {"mcp/google-flights"}


class TestCloudAckGuard:
    def test_cloud_without_ack_refuses(self) -> None:
        cfg = APIConfig(edition=Edition.cloud, allow_per_tenant_mcp=False)
        with pytest.raises(PerTenantMCPNotAckedError):
            check_per_tenant_mcp_posture(cfg, runtime_configured=True)

    def test_cloud_with_ack_proceeds(self) -> None:
        cfg = APIConfig(edition=Edition.cloud, allow_per_tenant_mcp=True)
        check_per_tenant_mcp_posture(cfg, runtime_configured=True)  # no raise

    def test_community_is_never_gated(self) -> None:
        cfg = APIConfig(edition=Edition.community, allow_per_tenant_mcp=False)
        check_per_tenant_mcp_posture(cfg, runtime_configured=True)  # byte-unchanged: no raise

    def test_runtime_not_configured_is_a_noop(self) -> None:
        cfg = APIConfig(edition=Edition.cloud, allow_per_tenant_mcp=False)
        check_per_tenant_mcp_posture(cfg, runtime_configured=False)  # nothing to gate
