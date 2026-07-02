"""Unit test for the chat-loop K6 wiring (Spec K6, K6-D-6 / K6-D-4 addendum).

Proves the loop resolves the user's name via its provider, threads it into the
PromptBuilder (so the persona speaks it), and fires the self-node sync with the
resolved name — and that an un-wired loop (no providers) is byte-identical.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from _fakes import FakeStore, ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from persona.backends import BackendConfig
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation, ConversationMessage
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from persona.backends import StreamChunk

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]
_CLEAR = "What time is the hearing tomorrow?"  # a non-ambiguous turn → generates


class _CapturingBackend(ScriptedBackend):
    last_messages: list[ConversationMessage]

    def __init__(self, rounds: list[ScriptedRound]) -> None:
        super().__init__(rounds)
        self.last_messages = []

    async def chat_stream(
        self,
        messages: list[ConversationMessage],
        **kwargs: Any,  # noqa: ANN401 — test passthrough mirroring the backend signature
    ) -> AsyncIterator[StreamChunk]:
        self.last_messages = list(messages)
        async for chunk in super().chat_stream(messages, **kwargs):
            yield chunk


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(name="Astrid", role="assistant", background="bg"),
    )


def _make_loop(
    backend: ScriptedBackend,
    *,
    user_name_provider: Callable[[], str | None] | None = None,
    self_node_sync: Callable[[str | None], None] | None = None,
) -> ConversationLoop:
    stores = {k: FakeStore() for k in ("identity", "self_facts", "worldview", "episodic")}
    registry = TierRegistry(
        {t: TierConfig(name=t, backend_config=_DUMMY_CFG) for t in ("frontier", "mid", "small")}
    )
    registry._cache = {"frontier": backend, "mid": backend, "small": backend}  # type: ignore[assignment]  # noqa: SLF001
    return ConversationLoop(
        persona=_persona(),
        stores=stores,  # type: ignore[arg-type]
        toolbox=Toolbox([], allow_list=None),  # type: ignore[arg-type]
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        turn_log_writer=MemoryTurnLogWriter(),
        user_name_provider=user_name_provider,
        self_node_sync=self_node_sync,
    )


def _conv() -> Conversation:
    return Conversation(conversation_id="c1", persona_id="astrid", messages=[])


async def _run(loop: ConversationLoop, message: str) -> None:
    async for _ in loop.turn(_conv(), message):
        pass


@pytest.mark.asyncio
async def test_loop_threads_name_into_prompt_and_fires_self_sync() -> None:
    backend = _CapturingBackend([ScriptedRound(text="ok")])
    synced: list[str | None] = []
    loop = _make_loop(
        backend,
        user_name_provider=lambda: "Ada Lovelace",
        self_node_sync=synced.append,
    )

    await _run(loop, _CLEAR)

    system = next(m for m in backend.last_messages if m.role == "system")
    assert "You are speaking with Ada Lovelace." in system.content
    # The runtime prompt path is the SELF node's creation trigger (K6-D-4 addendum).
    assert synced == ["Ada Lovelace"]


@pytest.mark.asyncio
async def test_unwired_loop_speaks_no_name_and_never_syncs() -> None:
    backend = _CapturingBackend([ScriptedRound(text="ok")])
    loop = _make_loop(backend)  # no providers — the additive default

    await _run(loop, _CLEAR)

    system = next(m for m in backend.last_messages if m.role == "system")
    assert "speaking with" not in system.content


@pytest.mark.asyncio
async def test_nameless_turn_skips_the_self_sync_name() -> None:
    backend = _CapturingBackend([ScriptedRound(text="ok")])
    synced: list[str | None] = []
    loop = _make_loop(backend, user_name_provider=lambda: None, self_node_sync=synced.append)

    await _run(loop, _CLEAR)

    system = next(m for m in backend.last_messages if m.role == "system")
    assert "speaking with" not in system.content
    # Sync is still invoked (once per turn), but with None — the factory closure
    # is what declines to materialise a self node for a nameless user.
    assert synced == [None]
