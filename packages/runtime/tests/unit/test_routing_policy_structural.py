"""Structural proof that the default hot path bypasses the cost machinery (T5; criterion 6).

The Spec-18 Layer-2 scorer (`scoring.score_tier`, `UnifiedRouter.route`) and
the Spec-23 `IntelligentRouter.select_model` are DORMANT (P9-D-4): a full
real-loop chat turn under the production composition (`PolicyRouter`, no
intelligent router — the factory's default with the global gate off) must
never touch them. Proven structurally: every scorer entry point is patched to
detonate; the turn completes anyway. The background-summary and title paths
route via ``tier_for("background")`` (criterion 4) — locked here at the
policy level.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import patch

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
from persona_runtime.routing import PolicyRouter, tier_for
from persona_runtime.routing.intelligent_router import IntelligentRouter
from persona_runtime.routing.unified import UnifiedRouter
from persona_runtime.tier import TierConfig, TierRegistry

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]


def _make_loop() -> ConversationLoop:
    backend = ScriptedBackend([ScriptedRound(text="Hei.")])
    registry = TierRegistry(
        {t: TierConfig(name=t, backend_config=_DUMMY_CFG) for t in ("frontier", "mid", "small")}
    )
    registry._cache = {"frontier": backend, "mid": backend, "small": backend}  # type: ignore[assignment]
    return ConversationLoop(
        persona=Persona(
            persona_id="astrid",
            identity=PersonaIdentity(
                name="Astrid",
                role="assistant",
                background="bg",
                constraints=["none"],
            ),
        ),
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
        # The factory's default composition: PolicyRouter, NO intelligent
        # router (the global gate is off by default — P9-D-4/D-7).
        router=PolicyRouter(tier_registry=registry),
        tier_registry=registry,
        turn_log_writer=MemoryTurnLogWriter(),
    )


def _boom(*_args: object, **_kwargs: object) -> object:
    raise AssertionError("the dormant cost machinery was invoked on the hot path")


class TestHotPathBypassesTheScorers:
    @pytest.mark.asyncio
    async def test_full_turn_never_touches_spec18_or_spec23_machinery(self) -> None:
        loop = _make_loop()
        conv = Conversation(
            conversation_id="c1",
            persona_id="astrid",
            messages=[
                ConversationMessage(role="user", content="tidligere", created_at=datetime.now(UTC))
            ],
        )
        with (
            patch("persona_runtime.routing.scoring.score_tier", _boom),
            patch.object(UnifiedRouter, "route", _boom),
            patch.object(IntelligentRouter, "select_model", _boom),
        ):
            chunks = [c async for c in loop.turn(conv, "hei!")]
        assert chunks  # the turn completed with every scorer entry point armed


class TestBackgroundSurfacesAreThePolicy:
    def test_background_resolves_small(self) -> None:
        # Criterion 4 at the policy level — the summary/title/text_summarize
        # sites all resolve their tier through this call now.
        assert tier_for("background") == "small"

    def test_env_synthesis_override_wins_over_the_table(self) -> None:
        # worker_root passes config.synthesis_tier as the override — an
        # operator's PERSONA_API_SYNTHESIS_TIER is honored above the table.
        assert tier_for("background", override="mid") == "mid"

    def test_agentic_step_resolves_frontier(self) -> None:
        # The P9-R-1 ruling for the agentic run surface (user-read output).
        assert tier_for("agentic_step") == "frontier"
