"""M1-T8 — end-to-end proof: a persona's ``preferred_model`` actually serves a REAL turn.

Drives the REAL trigger chain per ``spec_M1_plan.md`` Task 8 (no hand-forced state): a
persona is seeded in Postgres with a YAML ``routing.preferred_model`` (or without one),
``RuntimeFactory.build_conversation_loop`` — the SAME composition-root method every
``/v1/conversations/*`` chat request calls (spec 08 KEYSTONE 1; T4 wires it) — builds a
real :class:`~persona_runtime.loop.ConversationLoop`, and ONE real turn is driven through
it via ``loop.turn(...)``. The ONLY monkeypatch is the outermost provider seam the
factory imports — ``persona_api.services.runtime_factory.build_openrouter_passthrough``
(T4's injection point, ``preferred_backend_provider=build_openrouter_passthrough`` at
``runtime_factory.py:1525``). Nothing loop-internal is ever touched: no
``decision.model`` set by hand, no ``_finalize_routing`` called directly, no forced
attribute. The tier backend is a scripted fake (no network) — mirrors
``test_runtime_factory.py``'s T10 ``_ScriptedBackend``/``_ScriptedRegistry`` precedent.

Four scenarios (the plan's three (a)/(b)/(c), plus one bonus (b2) proving a second,
distinct fail-open path):

(a) A persona with a capable ``preferred_model`` + a working passthrough ⇒ the
    passthrough serves the turn, provenance ``model_fallback_reason ==
    "preferred_model"``, the tier default is never reached.
(b) The SAME shape, but the fronted passthrough backend RAISES a provider error ⇒ the
    composed ``MultiModelChatBackend`` chain (M1-T3's ``_front_preferred_backend``) falls
    through per D-20-9 and the tier default serves the turn — the turn still succeeds.
(b2) The provider itself returns ``None`` (e.g. an unconfigured OpenRouter key, T2's
    fail-open contract) ⇒ ``_front_preferred_backend`` leaves the tier backend unchanged
    and it serves the turn directly (never wrapped in a ``MultiModelChatBackend`` at
    all) — a SECOND, distinct fail-open path from (b)'s runtime-error fallback.
(c) A persona WITHOUT ``preferred_model`` ⇒ byte-identical-to-today routing: the tier
    default serves it and ``build_openrouter_passthrough`` is NEVER CALLED, even though
    every loop the factory builds carries it (T4) — the additive invariant
    (criterion 11).

Integration-marked (real Postgres, RLS-scoped) — mirrors ``test_runtime_factory.py``'s
seeding idiom exactly; skips when ``APP_DATABASE_URL`` is unset.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.backends import StreamChunk, TokenUsage
from persona.backends.errors import ModelNotFoundError
from persona.schema.conversation import Conversation
from persona.stores.postgres import PostgresBackend
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services.runtime_factory import RuntimeFactory
from persona_runtime.logging import MemoryTurnLogWriter
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends import ToolSpec
    from persona.backends.protocol import ChatBackend
    from persona.schema.conversation import ConversationMessage
    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

# A fake OpenRouter catalog id — never registered in any tier's
# ``PERSONA_<TIER>_MODELS`` list, so ``_front_preferred_backend`` can only reach it
# through the injected ``preferred_backend_provider`` seam (never ``reorder_primary``).
_PREFERRED_ID = "openrouter/m1-e2e-fake-preferred"

_YAML_NO_ROUTING = """\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: |
    A helper.
  language_default: en
  constraints: []
self_facts:
  - fact: knows things
    confidence: 1.0
"""

_YAML_WITH_PREFERRED = f"""\
schema_version: "1.0"
identity:
  name: Astrid
  role: assistant
  background: |
    A helper.
  language_default: en
  constraints: []
self_facts:
  - fact: knows things
    confidence: 1.0
routing:
  preferred_model: "{_PREFERRED_ID}"
"""

_AUDIT_ROOT = Path("/tmp/persona-m1e2e-audit")  # noqa: S108 — mirrors T10/T4's own /tmp audit_root


# ----------------------------------------------------------------------------------- #
# Fakes — no network, ChatBackend Protocol implementations.
# ----------------------------------------------------------------------------------- #


class _ScriptedBackend:
    """The tier default: a minimal ChatBackend (mirrors ``test_runtime_factory.py`` T10)."""

    provider_name = "anthropic"
    model_name = "scripted"
    max_tokens = 4096

    def __init__(self) -> None:
        self.chat_stream_calls = 0

    @property
    def supports_native_tools(self) -> bool:
        return True

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_: object) -> object:  # noqa: ARG002
        raise NotImplementedError

    async def chat_stream(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002
        *,
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
        **_: object,
    ) -> AsyncIterator[StreamChunk]:
        self.chat_stream_calls += 1
        yield StreamChunk(delta="Tier default: ", is_final=False)
        yield StreamChunk(
            delta="Jeg er Astrid.",
            is_final=True,
            usage=TokenUsage(prompt_tokens=12, completion_tokens=7, total_tokens=19),
        )


class _ScriptedRegistry:
    """A TierRegistry stand-in returning ONE scripted backend for any tier (T10 precedent)."""

    def __init__(self) -> None:
        self.backend = _ScriptedBackend()

    def get(self, _tier_name: str) -> _ScriptedBackend:
        return self.backend

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        return ("frontier", "mid", "small")

    def supports_vision_for(self, _tier_name: str) -> bool:
        return self.backend.supports_vision

    def metadata_for(self, _tier_name: str) -> None:
        return None

    def model_name_for(self, _tier_name: str) -> str:
        return "scripted"

    async def aclose(self) -> None:
        pass


class _RecordingPassthroughBackend:
    """The scripted fake ``build_openrouter_passthrough`` returns for scenario (a).

    Records every ``chat_stream`` call so the test can assert the passthrough — and
    only the passthrough — served the turn.
    """

    provider_name = "openrouter"
    max_tokens = 4096

    def __init__(self, model_id: str) -> None:
        self.model_name = model_id
        self.chat_stream_calls = 0

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_: object) -> object:  # noqa: ARG002
        raise NotImplementedError

    async def chat_stream(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002
        *,
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
        **_: object,
    ) -> AsyncIterator[StreamChunk]:
        self.chat_stream_calls += 1
        yield StreamChunk(delta="Preferred: ", is_final=False)
        yield StreamChunk(
            delta="served by the passthrough.",
            is_final=True,
            usage=TokenUsage(prompt_tokens=9, completion_tokens=6, total_tokens=15),
        )


class _RaisingPassthroughBackend:
    """A fake passthrough whose ``chat_stream`` raises before its first chunk (scenario b).

    ``ModelNotFoundError`` is a D-20-9 FALLBACK-NO-RETRY ``ProviderError`` — the composed
    ``MultiModelChatBackend`` chain falls through to the next backend immediately (no
    retry sleep), proving the SAME cross-provider fallback machinery a real misbehaving
    OpenRouter passthrough would hit in production.
    """

    provider_name = "openrouter"
    max_tokens = 4096

    def __init__(self, model_id: str) -> None:
        self.model_name = model_id
        self.chat_stream_calls = 0

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **_: object) -> object:  # noqa: ARG002
        raise NotImplementedError

    async def chat_stream(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002
        *,
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
        **_: object,
    ) -> AsyncIterator[StreamChunk]:
        self.chat_stream_calls += 1
        raise ModelNotFoundError(
            "fake passthrough model not found",
            context={"provider": "openrouter", "model": self.model_name},
        )
        yield  # pragma: no cover — unreached; keeps this an async generator


# ----------------------------------------------------------------------------------- #
# Seed / cleanup — mirrors ``test_runtime_factory.py``'s ``_seed_persona``/``_cleanup``.
# ----------------------------------------------------------------------------------- #


def _seed_persona(
    database_url: str,
    embedder: HashEmbedder384,
    owner: str,
    persona_id: str,
    yaml_doc: str,
) -> None:
    su = make_rls_engine(database_url)
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:i, :o, :y)"),
            {"i": persona_id, "o": owner, "y": yaml_doc},
        )
    backend = PostgresBackend(engine=su, embedder=embedder)
    from persona.schema.chunks import PersonaChunk

    backend.upsert(
        persona_id=persona_id,
        store_kind="self_facts",
        chunks=[
            PersonaChunk(
                id=f"{persona_id}::self_facts::0000",
                text="self_fact: knows things",
                metadata={},
                created_at=datetime.now(UTC),
            )
        ],
    )
    su.dispose()


def _cleanup(su_url: str, owner: str) -> None:
    su = make_rls_engine(su_url)
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :i"), {"i": owner})
    su.dispose()


def _require_db_urls() -> tuple[str, str]:
    """``(app_url, su_url)``, or skip when the integration DSNs aren't configured."""
    import os

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    return app_url, os.environ["DATABASE_URL"]


# ----------------------------------------------------------------------------------- #
# (a) Capable preferred model + working passthrough serves the turn.
# ----------------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_preferred_model_serves_the_turn_via_the_real_factory_loop(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_url, su_url = _require_db_urls()
    owner, persona_id = "user_m1e2e_a", "persona_m1e2e_a"
    _seed_persona(su_url, embedder, owner, persona_id, _YAML_WITH_PREFERRED)

    passthrough = _RecordingPassthroughBackend(_PREFERRED_ID)

    def _fake_provider(model_id: str) -> ChatBackend | None:
        return passthrough if model_id == _PREFERRED_ID else None  # type: ignore[return-value]

    monkeypatch.setattr(
        "persona_api.services.runtime_factory.build_openrouter_passthrough", _fake_provider
    )

    registry = _ScriptedRegistry()
    rls_engine = make_rls_engine(app_url)
    writer = MemoryTurnLogWriter()
    factory = RuntimeFactory(
        rls_engine=rls_engine,
        embedder=embedder,
        tier_registry=registry,  # type: ignore[arg-type]
        turn_log_writer=writer,  # type: ignore[arg-type]
        audit_root=_AUDIT_ROOT,
    )

    token = current_user_id.set(owner)
    try:
        loop = await factory.build_conversation_loop(persona_id)
        conv = Conversation(conversation_id="c_m1e2e_a", persona_id=persona_id, messages=[])
        deltas = [c.delta async for c in loop.turn(conv, "Hvem er du?")]
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()
        _cleanup(su_url, owner)

    text_out = "".join(deltas)
    # (a) the passthrough — and ONLY the passthrough — served the turn.
    assert "served by the passthrough" in text_out
    assert passthrough.chat_stream_calls == 1
    assert registry.backend.chat_stream_calls == 0
    decision = writer.logs[0].routing_decision
    assert decision is not None
    assert decision.model == _PREFERRED_ID
    assert decision.model_fallback_reason == "preferred_model"


# ----------------------------------------------------------------------------------- #
# (b) The fronted passthrough RAISES -> the tier chain falls through and still serves.
# ----------------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_preferred_model_raising_falls_back_to_tier_chain(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_url, su_url = _require_db_urls()
    owner, persona_id = "user_m1e2e_b", "persona_m1e2e_b"
    _seed_persona(su_url, embedder, owner, persona_id, _YAML_WITH_PREFERRED)

    raiser = _RaisingPassthroughBackend(_PREFERRED_ID)

    def _fake_provider(model_id: str) -> ChatBackend | None:
        return raiser if model_id == _PREFERRED_ID else None  # type: ignore[return-value]

    monkeypatch.setattr(
        "persona_api.services.runtime_factory.build_openrouter_passthrough", _fake_provider
    )

    registry = _ScriptedRegistry()
    rls_engine = make_rls_engine(app_url)
    writer = MemoryTurnLogWriter()
    factory = RuntimeFactory(
        rls_engine=rls_engine,
        embedder=embedder,
        tier_registry=registry,  # type: ignore[arg-type]
        turn_log_writer=writer,  # type: ignore[arg-type]
        audit_root=_AUDIT_ROOT,
    )

    token = current_user_id.set(owner)
    try:
        loop = await factory.build_conversation_loop(persona_id)
        conv = Conversation(conversation_id="c_m1e2e_b", persona_id=persona_id, messages=[])
        deltas = [c.delta async for c in loop.turn(conv, "Hvem er du?")]
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()
        _cleanup(su_url, owner)

    text_out = "".join(deltas)
    # (b) the passthrough was attempted (and raised)...
    assert raiser.chat_stream_calls == 1
    # ...and the D-20-9 chain fell through to the tier default, which served the turn.
    assert registry.backend.chat_stream_calls == 1
    assert "Jeg er Astrid." in text_out
    # Provenance still records what the turn ASKED for (the preferred choice).
    decision = writer.logs[0].routing_decision
    assert decision is not None
    assert decision.model_fallback_reason == "preferred_model"


# ----------------------------------------------------------------------------------- #
# (b2) bonus: the provider itself returns None (unconfigured key) -> tier serves it.
# ----------------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_preferred_model_provider_none_falls_back_to_tier_default(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_url, su_url = _require_db_urls()
    owner, persona_id = "user_m1e2e_d", "persona_m1e2e_d"
    _seed_persona(su_url, embedder, owner, persona_id, _YAML_WITH_PREFERRED)

    calls: list[str] = []

    def _always_none(model_id: str) -> ChatBackend | None:
        calls.append(model_id)
        return None

    monkeypatch.setattr(
        "persona_api.services.runtime_factory.build_openrouter_passthrough", _always_none
    )

    registry = _ScriptedRegistry()
    rls_engine = make_rls_engine(app_url)
    writer = MemoryTurnLogWriter()
    factory = RuntimeFactory(
        rls_engine=rls_engine,
        embedder=embedder,
        tier_registry=registry,  # type: ignore[arg-type]
        turn_log_writer=writer,  # type: ignore[arg-type]
        audit_root=_AUDIT_ROOT,
    )

    token = current_user_id.set(owner)
    try:
        loop = await factory.build_conversation_loop(persona_id)
        conv = Conversation(conversation_id="c_m1e2e_d", persona_id=persona_id, messages=[])
        deltas = [c.delta async for c in loop.turn(conv, "Hvem er du?")]
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()
        _cleanup(su_url, owner)

    text_out = "".join(deltas)
    # The provider WAS asked for the preferred id (unlike scenario c)...
    assert calls == [_PREFERRED_ID]
    # ...returned None, so the tier backend served it UNFRONTED (never wrapped).
    assert registry.backend.chat_stream_calls == 1
    assert "Jeg er Astrid." in text_out
    decision = writer.logs[0].routing_decision
    assert decision is not None
    assert decision.model_fallback_reason == "preferred_model"


# ----------------------------------------------------------------------------------- #
# (c) No preferred_model -> byte-identical routing; the provider is NEVER called.
# ----------------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_persona_without_preferred_model_never_touches_the_provider(
    migrated_engine: Engine,  # noqa: ARG001
    embedder: HashEmbedder384,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app_url, su_url = _require_db_urls()
    owner, persona_id = "user_m1e2e_c", "persona_m1e2e_c"
    _seed_persona(su_url, embedder, owner, persona_id, _YAML_NO_ROUTING)

    calls: list[str] = []

    def _spy_provider(model_id: str) -> ChatBackend | None:
        calls.append(model_id)
        return None

    monkeypatch.setattr(
        "persona_api.services.runtime_factory.build_openrouter_passthrough", _spy_provider
    )

    registry = _ScriptedRegistry()
    rls_engine = make_rls_engine(app_url)
    writer = MemoryTurnLogWriter()
    factory = RuntimeFactory(
        rls_engine=rls_engine,
        embedder=embedder,
        tier_registry=registry,  # type: ignore[arg-type]
        turn_log_writer=writer,  # type: ignore[arg-type]
        audit_root=_AUDIT_ROOT,
    )

    token = current_user_id.set(owner)
    try:
        loop = await factory.build_conversation_loop(persona_id)
        # The factory wires the (patched) provider into EVERY loop it builds (T4) —
        # confirm it IS present before proving it is never invoked below.
        assert loop._preferred_backend_provider is _spy_provider  # noqa: SLF001
        conv = Conversation(conversation_id="c_m1e2e_c", persona_id=persona_id, messages=[])
        deltas = [c.delta async for c in loop.turn(conv, "Hvem er du?")]
    finally:
        current_user_id.reset(token)
        rls_engine.dispose()
        _cleanup(su_url, owner)

    text_out = "".join(deltas)
    assert "Jeg er Astrid." in text_out
    assert registry.backend.chat_stream_calls == 1
    # (c) the provider seam was wired but NEVER invoked — byte-identical routing.
    assert calls == []
    decision = writer.logs[0].routing_decision
    assert decision is not None
    assert decision.model_fallback_reason is None
    assert decision.model_candidates == ()
    assert decision.score_vector == {}
    assert decision.weights_used == {}
