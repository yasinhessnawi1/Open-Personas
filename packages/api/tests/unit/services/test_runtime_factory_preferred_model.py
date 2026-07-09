"""Composition-root proof for M1-T4 — the factory injects the OpenRouter passthrough.

Spec M1 (T2 + T3 + T4): a persona's ``preferred_model`` (T1, additive-optional) is
served through ``ConversationLoop``'s ``preferred_backend_provider`` hook (T3) — here
the composition root (T4) wires that hook to the real T2 provider,
``build_openrouter_passthrough`` (any OpenRouter catalog id, cached, fail-open). Every
loop the factory builds must carry this SAME provider — never a stub, never omitted —
so a persona's chosen model is actually reachable in production.

Proven by intercepting ``ConversationLoop`` at its construction site rather than
driving a real ``build_conversation_loop`` call end-to-end: a real call needs a live
persona row (``_load_persona``'s DB read), which is heavyweight for a unit test.
``_load_persona`` is patched to hand back an in-memory ``Persona`` (skip the DB), and
``ConversationLoop`` is patched to a recording stand-in that captures its constructor
kwargs — mirroring the sentinel-dependency style the sibling composition tests use
(``test_runtime_factory_recognition_tier.py``, ``test_runtime_factory_intelligent_gate
.py``, ``test_runtime_factory_file_tool_run_coherence.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from persona.backends.errors import TierNotConfiguredError
from persona.backends.openrouter_passthrough import build_openrouter_passthrough
from persona.schema.persona import Persona, PersonaIdentity
from persona_api.services.runtime_factory import RuntimeFactory


class _FakeEmbedder:
    model_name = "fake"
    dimension = 384


class _NoTierRegistry:
    """A tier registry whose ``get`` always declines (keyless test environment).

    Mirrors ``test_runtime_factory_file_tool_run_coherence.py``'s fake: every
    optional tier-backed extra (``text_summarize``, the A4/A8 interpreters, the A5
    initiative dial) fails soft to absent, so ``build_conversation_loop`` reaches the
    ``ConversationLoop(...)`` construction site without a real tier / API key.
    """

    def get(self, tier_name: str) -> object:
        raise TierNotConfiguredError("no tiers configured", context={"tier": tier_name})

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ()

    async def aclose(self) -> None:
        return None


class _RecordingConversationLoop:
    """Stands in for ``ConversationLoop``; captures the composition root's kwargs.

    Real construction is unnecessary for this wiring proof (the brief's sanctioned
    shortcut for a heavyweight real loop) — only WHICH ``preferred_backend_provider``
    the factory hands in matters. Supports the post-construction
    ``loop.deferred_input_files = ...`` assignment ``build_conversation_loop`` makes
    (Spec 16 M1a wiring), since it is a plain object with no ``__slots__``.
    """

    def __init__(self, **kwargs: object) -> None:
        self.kwargs = kwargs
        self.deferred_input_files: list[object] = []


def _persona() -> Persona:
    """A tool-free, skill-free persona — keeps ``_build_toolbox`` a fast no-op."""
    return Persona(
        persona_id="p_m1_t4",
        identity=PersonaIdentity(
            name="Router Test",
            role="Fixture",
            background="A tool/skill-free persona used to prove the M1-T4 wiring.",
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


@pytest.mark.asyncio
async def test_build_conversation_loop_injects_the_openrouter_passthrough(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every loop the factory builds carries T2's real provider (M1-T4)."""
    factory = _factory()
    persona = _persona()
    monkeypatch.setattr(factory, "_load_persona", lambda persona_id: persona)  # noqa: ARG005
    monkeypatch.setattr(
        "persona_api.services.runtime_factory.ConversationLoop",
        _RecordingConversationLoop,
    )

    loop = await factory.build_conversation_loop(persona.persona_id)  # type: ignore[arg-type]

    assert (
        loop.kwargs["preferred_backend_provider"]  # type: ignore[attr-defined]
        is build_openrouter_passthrough
    )
