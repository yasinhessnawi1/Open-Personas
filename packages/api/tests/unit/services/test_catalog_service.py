"""Unit tests for the N7 (D-N7-2) catalog-listability C-filter.

``catalog_service.is_listable`` is the pure predicate the ``GET /v1/mcp-catalog``
route applies server-side (the owner-ruled C-filter, Phase-3 gate): an image-type
catalog entry (``server_type == "server"``) that neither the per-tenant runtime nor
an operator gateway could ever serve is excluded from the listing outright — "what
this deployment can actually offer," not "what exists that you'll be refused."

No I/O; a plain truth table over the three inputs that matter (``server_type``,
``per_tenant_runtime``, ``gateway``).
"""

from __future__ import annotations

import pytest
from persona.tools.mcp.catalog import MCPServerCatalogEntry
from persona_api.services.catalog_service import is_listable


def _entry(server_type: str) -> MCPServerCatalogEntry:
    return MCPServerCatalogEntry(
        name="thing",
        description="d",
        kind="external",
        risk="low",
        server_type=server_type,  # type: ignore[arg-type]
    )


class TestImageEntryCFilter:
    """``server_type == "server"`` (the Docker-registry image taxonomy) is the ONLY
    shape this predicate ever excludes — and only when NEITHER mechanism could run
    it."""

    def test_no_runtime_no_gateway_is_excluded(self) -> None:
        assert is_listable(_entry("server"), per_tenant_runtime=False, gateway=False) is False

    def test_runtime_present_is_listed(self) -> None:
        assert is_listable(_entry("server"), per_tenant_runtime=True, gateway=False) is True

    def test_gateway_present_is_listed(self) -> None:
        # The catalog can't see what the operator enabled ON the gateway — stays
        # listed with an honest gateway-managed note (T4), never hard-excluded.
        assert is_listable(_entry("server"), per_tenant_runtime=False, gateway=True) is True

    def test_both_mechanisms_present_is_listed(self) -> None:
        assert is_listable(_entry("server"), per_tenant_runtime=True, gateway=True) is True


class TestNonImageEntriesAlwaysListed:
    """``remote`` / ``builtin`` / ``external`` entries are ALWAYS listed — the
    filter is scoped strictly to the unrunnable image case, regardless of
    capabilities (a remote/builtin app doesn't need a per-tenant runtime or a
    gateway to be adoptable/enableable)."""

    @pytest.mark.parametrize("server_type", ["remote", "builtin", "external"])
    def test_listed_with_no_capabilities(self, server_type: str) -> None:
        assert is_listable(_entry(server_type), per_tenant_runtime=False, gateway=False) is True

    @pytest.mark.parametrize("server_type", ["remote", "builtin", "external"])
    def test_listed_with_both_capabilities(self, server_type: str) -> None:
        assert is_listable(_entry(server_type), per_tenant_runtime=True, gateway=True) is True
