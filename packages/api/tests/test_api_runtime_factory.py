"""RuntimeFactory lifecycle + composition (spec 08, T10).

No DB. Verifies the lifecycle contract (aclose → tier_registry.aclose() + MCP
disconnect, D-05-4) and that the factory exposes the per-request loop builders.
The full real-loop end-to-end (scripted backend through the tier registry) is
exercised in the T15 integration suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from persona_api.services.runtime_factory import RuntimeFactory


class _SpyTierRegistry:
    def __init__(self) -> None:
        self.aclose_called = False

    async def aclose(self) -> None:
        self.aclose_called = True


class _SpyMCPClient:
    def __init__(self) -> None:
        self.disconnected = False

    async def disconnect(self) -> None:
        self.disconnected = True


class _FakeEmbedder:
    model_name = "fake"
    dimension = 384

    def encode(self, _texts: object) -> list[list[float]]:  # pragma: no cover - unused here
        return []


def _factory() -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=object(),  # type: ignore[arg-type] — not used by aclose
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        tier_registry=_SpyTierRegistry(),  # type: ignore[arg-type]
        turn_log_writer=object(),  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-audit-test"),
    )


class _SpyScorer:
    def score(self, _text: str) -> float:  # pragma: no cover — identity is what matters here
        return 0.0


def test_factory_holds_the_injected_crisis_encoder() -> None:
    """R6 T8: the composition root's encoder is held on the app-scoped factory, so every
    chat + agentic loop it builds shares the SAME instance (``build_*`` pass
    ``self._crisis_encoder`` — proven wired into the loop in the runtime suite)."""
    scorer = _SpyScorer()
    factory = RuntimeFactory(
        rls_engine=object(),  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        tier_registry=_SpyTierRegistry(),  # type: ignore[arg-type]
        turn_log_writer=object(),  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-audit-test"),
        crisis_encoder=scorer,  # type: ignore[arg-type]
    )
    assert factory._crisis_encoder is scorer  # noqa: SLF001


def test_factory_defaults_to_no_encoder_which_is_v11_lexical_only() -> None:
    assert _factory()._crisis_encoder is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_aclose_closes_tier_registry_and_mcp_clients() -> None:
    factory = _factory()
    registry: _SpyTierRegistry = factory._tier_registry  # type: ignore[assignment]
    client = _SpyMCPClient()
    factory._mcp_clients.append(client)  # type: ignore[arg-type]

    await factory.aclose()

    assert registry.aclose_called is True
    assert client.disconnected is True


@pytest.mark.asyncio
async def test_aclose_with_no_mcp_clients_still_closes_registry() -> None:
    factory = _factory()
    registry: _SpyTierRegistry = factory._tier_registry  # type: ignore[assignment]
    await factory.aclose()
    assert registry.aclose_called is True


def test_factory_exposes_loop_builders() -> None:
    factory = _factory()
    assert callable(factory.build_conversation_loop)
    assert callable(factory.build_agentic_loop)


# Section: per-request file-tool sandbox scope (SECURITY — cross-context leak)
#
# The provider the factory hands to ``build_default_toolbox`` must resolve the
# file_read / file_write sandbox root from the CURRENT request's
# SandboxRequestContext to ``<workspace_root>/<owner_id>/<persona_id>`` — the
# same scope uploads + code_execution + the workspace persister use. With no
# bound context it must return None so the file tools fail closed.


def _scoped_factory(workspace_root: Path) -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=object(),  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        tier_registry=_SpyTierRegistry(),  # type: ignore[arg-type]
        turn_log_writer=object(),  # type: ignore[arg-type]
        audit_root=workspace_root / "audit",
        workspace_root=workspace_root,
    )


def test_file_sandbox_provider_scopes_to_request_owner_and_persona(tmp_path: Path) -> None:
    from persona_api.sandbox import (
        SandboxRequestContext,
        reset_sandbox_request_context,
        set_sandbox_request_context,
    )

    factory = _scoped_factory(tmp_path)
    provider = factory._build_file_sandbox_root_provider("personaA")
    assert provider is not None

    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="ownerA", conversation_id="conv1")
    )
    try:
        assert provider() == tmp_path / "ownerA" / "personaA"
    finally:
        reset_sandbox_request_context(token)

    # A DIFFERENT request context resolves to a DIFFERENT root (no cross-context
    # bleed): the SAME cached provider returns ownerB's path under ownerB's ctx.
    token2 = set_sandbox_request_context(
        SandboxRequestContext(owner_id="ownerB", conversation_id="conv2")
    )
    try:
        assert provider() == tmp_path / "ownerB" / "personaA"
        assert provider() != tmp_path / "ownerA" / "personaA"
    finally:
        reset_sandbox_request_context(token2)


def test_file_sandbox_provider_returns_none_without_context(tmp_path: Path) -> None:
    factory = _scoped_factory(tmp_path)
    provider = factory._build_file_sandbox_root_provider("personaA")
    assert provider is not None
    # No bound request context ⇒ None ⇒ file tools fail closed (deny).
    assert provider() is None


def test_file_sandbox_provider_absent_without_workspace_root() -> None:
    # CLI / test path: no workspace_root ⇒ no provider ⇒ build_default_toolbox
    # falls back to the (single-tenant) config.tools_sandbox_root.
    factory = _factory()
    assert factory._build_file_sandbox_root_provider("personaA") is None


# --- Spec P4: the filesystem MCP subprocess scope root --------------------------
# `_resolve_filesystem_scope_root` is the value threaded into the builtin
# `filesystem` MCP child at spawn. It MUST be the same source of truth as the
# in-process file tools (it delegates to `_build_file_sandbox_root_provider`), so
# the subprocess and the in-process tools can never disagree for a request.


def test_filesystem_scope_root_matches_in_process_provider_when_bound(tmp_path: Path) -> None:
    # Caution #2 (spawn-time == turn-time scope): under a bound request context the
    # subprocess scope root is byte-identical to what the in-process file tools
    # resolve — same owner/persona root, no divergence.
    from persona_api.sandbox import (
        SandboxRequestContext,
        reset_sandbox_request_context,
        set_sandbox_request_context,
    )

    factory = _scoped_factory(tmp_path)
    provider = factory._build_file_sandbox_root_provider("personaA")
    assert provider is not None

    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="ownerA", conversation_id="conv1")
    )
    try:
        in_process_root = provider()
        subprocess_root = factory._resolve_filesystem_scope_root("personaA")
        assert subprocess_root == in_process_root == tmp_path / "ownerA" / "personaA"
    finally:
        reset_sandbox_request_context(token)


def test_filesystem_scope_root_none_when_hosted_but_unbound(tmp_path: Path) -> None:
    # Caution #3 (the safety net under the implicit contextvar): hosted (workspace_root
    # configured) but NO request context bound ⇒ None ⇒ the child is spawned without a
    # scope and fails closed (serve-and-deny). NEVER the shared root (which here would
    # be a cross-tenant leak).
    factory = _scoped_factory(tmp_path)
    assert factory._resolve_filesystem_scope_root("personaA") is None


def test_filesystem_scope_root_falls_back_to_flat_root_on_cli_path() -> None:
    # CLI / single-tenant path (no workspace_root): the legitimate flat root, mirroring
    # the in-process fallback — there is no owner/persona to scope to and no other
    # tenant to leak across. Fail-closed is the HOSTED rule, not the CLI rule.
    factory = _factory()
    assert factory._resolve_filesystem_scope_root("personaA") == (
        factory._core_config.tools_sandbox_root
    )
