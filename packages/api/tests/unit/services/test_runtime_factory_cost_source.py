"""Composition-root proof for M2-T1 — the factory wires the shared cost source.

Spec M2 (D-M2-1): turn-cost estimation must work with intelligent routing OFF —
the ``routing_intelligent_enabled`` flag gates only the router CONSUMER, never
the pricing source. Pinned here:

* the metadata chain exists on a factory built with NO api_config (flag off),
* every loop the factory builds carries THAT instance as ``cost_source``
  (never a stub, never omitted), and
* when the router IS enabled, it consults the SAME instance (routing and
  pricing can never disagree about a model's metadata).

Style: the sibling composition proofs' recording stand-in
(``test_runtime_factory_preferred_model.py``) — a real
``build_conversation_loop`` needs a live persona row, so ``_load_persona`` is
patched and ``ConversationLoop`` is captured at its construction site.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from persona.backends.errors import TierNotConfiguredError
from persona.backends.metadata import ChainedModelMetadataResolver
from persona.schema.persona import Persona, PersonaIdentity
from persona_api.services.runtime_factory import RuntimeFactory


class _FakeEmbedder:
    model_name = "fake"
    dimension = 384


class _NoTierRegistry:
    """Every ``get`` declines — keyless test environment (sibling-fixture style)."""

    def get(self, tier_name: str) -> object:
        raise TierNotConfiguredError("no tiers configured", context={"tier": tier_name})

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ()

    async def aclose(self) -> None:
        return None


class _RecordingConversationLoop:
    """Captures the composition root's kwargs (the sanctioned wiring-proof shortcut)."""

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.deferred_input_files: list[object] = []


def _persona() -> Persona:
    return Persona(
        persona_id="p_m2_t1",
        identity=PersonaIdentity(
            name="Cost Test",
            role="Fixture",
            background="A tool/skill-free persona used to prove the M2-T1 wiring.",
        ),
        tools=[],
        skills=[],
    )


def _factory() -> RuntimeFactory:
    return RuntimeFactory(
        rls_engine=object(),  # type: ignore[arg-type] — _load_persona is patched below
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        tier_registry=_NoTierRegistry(),  # type: ignore[arg-type]
        turn_log_writer=object(),  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-audit-test"),
    )


def test_metadata_resolver_exists_with_routing_flag_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No api_config at all (flag can't even be read) → the chain still exists."""
    monkeypatch.delenv("PERSONA_OPENROUTER_API_KEY", raising=False)
    factory = _factory()
    assert isinstance(factory._metadata_resolver, ChainedModelMetadataResolver)  # noqa: SLF001
    assert factory._intelligent_router is None  # noqa: SLF001 — the flag gates ONLY this
    # Static-only chain (no key → no catalog client), still pricing curated models
    # fetch-free — the turn-path arm.
    assert factory._catalog_client is None  # noqa: SLF001
    hit = factory._metadata_resolver.resolve_with_source(  # noqa: SLF001
        "anthropic/claude-sonnet-4-6", allow_fetch=False
    )
    assert hit is not None
    assert hit[1] == "static"


def test_key_present_builds_catalog_client_without_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A configured key composes the client (construction is network-free, D-22-11)."""
    monkeypatch.setenv("PERSONA_OPENROUTER_API_KEY", "sk-or-test-not-real")
    factory = _factory()
    assert factory._catalog_client is not None  # noqa: SLF001
    # The cost path stays fetch-free: a cold catalog index is a miss; static
    # still answers for curated models.
    hit = factory._metadata_resolver.resolve_with_source(  # noqa: SLF001
        "anthropic/claude-sonnet-4-6", allow_fetch=False
    )
    assert hit is not None
    assert hit[1] == "static"


@pytest.mark.asyncio
async def test_build_conversation_loop_carries_the_shared_cost_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every loop the factory builds prices turns through the factory's chain."""
    monkeypatch.delenv("PERSONA_OPENROUTER_API_KEY", raising=False)
    factory = _factory()
    persona = _persona()
    monkeypatch.setattr(factory, "_load_persona", lambda persona_id: persona)  # noqa: ARG005
    monkeypatch.setattr(
        "persona_api.services.runtime_factory.ConversationLoop",
        _RecordingConversationLoop,
    )

    loop = await factory.build_conversation_loop(persona.persona_id)  # type: ignore[arg-type]

    assert loop.kwargs["cost_source"] is factory._metadata_resolver  # type: ignore[attr-defined]  # noqa: SLF001


def test_enabled_router_shares_the_same_resolver_instance() -> None:
    """Routing and pricing read ONE chain — they can never disagree (D-M2-1)."""

    class _RoutingOnConfig:
        routing_intelligent_enabled = True

    factory = RuntimeFactory(
        rls_engine=object(),  # type: ignore[arg-type]
        embedder=_FakeEmbedder(),  # type: ignore[arg-type]
        tier_registry=_NoTierRegistry(),  # type: ignore[arg-type]
        turn_log_writer=object(),  # type: ignore[arg-type]
        audit_root=Path("/tmp/persona-audit-test"),
        api_config=_RoutingOnConfig(),  # type: ignore[arg-type]
    )
    router = factory._intelligent_router  # noqa: SLF001
    assert router is not None
    assert router._resolver is factory._metadata_resolver  # noqa: SLF001
