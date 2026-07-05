"""Real-loop composition tests for the P9 chat policy (T2; criterion 1).

Proves chat-resolves-frontier END TO END — through the composed
:class:`ConversationLoop` with the production router
(:class:`PolicyRouter`), not just a unit of the resolver: turn 1, turn N,
a boilerplate acknowledgement, and an identity-sensitive message all route
frontier; a pinned persona still routes by its pin (back-compat, both the
decision and the honored path proven from the TurnLog the loop wrote).

Mirrors the ``test_loop.py`` harness (`ScriptedBackend` + a real
:class:`TierRegistry` with every tier cached to the scripted backend) so the
turn exercises the real `_decide_routing` → generation → TurnLog write path.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _fakes import FakeStore, ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from persona.backends import BackendConfig
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation, ConversationMessage
from persona.schema.persona import Persona, PersonaIdentity, RoutingConfig
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.routing import PolicyRouter
from persona_runtime.tier import TierConfig, TierRegistry

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]


def _persona(*, pin: str = "auto") -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(
            name="Astrid",
            role="tenancy assistant",
            background="Knows husleieloven.",
            constraints=["Never give binding advice."],
        ),
        routing=RoutingConfig(tier_for_generation=pin),  # type: ignore[arg-type]
    )


def _make_loop(persona: Persona) -> tuple[ConversationLoop, MemoryTurnLogWriter]:
    backend = ScriptedBackend([ScriptedRound(text="Hei.") for _ in range(3)])
    registry = TierRegistry(
        {
            "frontier": TierConfig(name="frontier", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
        }
    )
    registry._cache = {"frontier": backend, "mid": backend, "small": backend}  # type: ignore[assignment]
    writer = MemoryTurnLogWriter()
    loop = ConversationLoop(
        persona=persona,
        stores={  # type: ignore[arg-type]
            "identity": FakeStore(),
            "self_facts": FakeStore(),
            "worldview": FakeStore(),
            "episodic": FakeStore(),
        },
        toolbox=Toolbox([], allow_list=None),  # type: ignore[arg-type]
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        # The PRODUCTION composition (runtime_factory does exactly this).
        router=PolicyRouter(tier_registry=registry),
        tier_registry=registry,
        turn_log_writer=writer,
    )
    return loop, writer


def _conv(turns: int = 0) -> Conversation:
    msgs = [
        ConversationMessage(
            role="user" if i % 2 == 0 else "assistant",
            content=f"turn {i}",
            created_at=datetime.now(UTC),
        )
        for i in range(turns)
    ]
    return Conversation(conversation_id="c1", persona_id="astrid", messages=msgs)


async def _routed_tier(
    loop: ConversationLoop, writer: MemoryTurnLogWriter, conv: Conversation, msg: str
) -> str:
    async for _ in loop.turn(conv, msg):
        pass
    decision = writer.logs[-1].routing_decision
    assert decision is not None
    return decision.tier


class TestChatResolvesFrontierEndToEnd:
    @pytest.mark.asyncio
    async def test_turn_1_is_frontier(self) -> None:
        loop, writer = _make_loop(_persona())
        assert await _routed_tier(loop, writer, _conv(0), "hi") == "frontier"

    @pytest.mark.asyncio
    async def test_turn_n_is_frontier_no_mid_downgrade(self) -> None:
        # The Spec 05 behaviour this kills: turn_count > 0 → "default → mid".
        loop, writer = _make_loop(_persona())
        assert await _routed_tier(loop, writer, _conv(6), "tell me more") == "frontier"

    @pytest.mark.asyncio
    async def test_boilerplate_is_frontier_no_small_downgrade(self) -> None:
        # The Spec 05 behaviour this kills: "thanks" → small (P9-D-6).
        loop, writer = _make_loop(_persona())
        assert await _routed_tier(loop, writer, _conv(6), "ok thanks!") == "frontier"

    @pytest.mark.asyncio
    async def test_identity_sensitive_is_frontier_like_everything_else(self) -> None:
        loop, writer = _make_loop(_persona())
        assert await _routed_tier(loop, writer, _conv(6), "who are you really?") == "frontier"

    @pytest.mark.asyncio
    async def test_rationale_names_the_policy(self) -> None:
        loop, writer = _make_loop(_persona())
        await _routed_tier(loop, writer, _conv(6), "hello")
        decision = writer.logs[-1].routing_decision
        assert decision is not None
        assert decision.rationale == "policy: chat → frontier"


class TestPinnedPersonaStillRoutesByItsPin:
    @pytest.mark.asyncio
    async def test_mid_pin_holds_against_the_frontier_default(self) -> None:
        # Back-compat (criterion 1 + 5): a stored pin is a deliberate override
        # (P9-D-7) — honored, just no longer surfaced.
        loop, writer = _make_loop(_persona(pin="mid"))
        assert await _routed_tier(loop, writer, _conv(0), "hi") == "mid"

    @pytest.mark.asyncio
    async def test_pin_rationale_names_the_override_path(self) -> None:
        loop, writer = _make_loop(_persona(pin="mid"))
        await _routed_tier(loop, writer, _conv(0), "hi")
        decision = writer.logs[-1].routing_decision
        assert decision is not None
        assert decision.rationale == "persona_override → mid"

    @pytest.mark.asyncio
    async def test_frontier_pin_resolves_frontier(self) -> None:
        loop, writer = _make_loop(_persona(pin="frontier"))
        assert await _routed_tier(loop, writer, _conv(4), "hi") == "frontier"
