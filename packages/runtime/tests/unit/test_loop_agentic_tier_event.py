"""The agentic run stream says which tier its steps ran on (part3 F12 / R9-187).

The conversation loop has emitted ``RunEvent.tier`` since Spec 31, and the run viewer
has reduced it onto the run header for just as long. The agentic loop resolved a tier
for every step and told nobody, so the badge on a watched run stayed blank for the whole
run while the reopened record (``Step.tier_used``) knew the answer all along.

These tests pin the emit RULE, not just the presence of a frame: the first step
announces the tier, a later step announces only a CHANGE, and the sequence the live
frames spell out is the same sequence the persisted steps spell out.
"""

# ruff: noqa: SLF001, mirrors test_loop_agentic.py: the registry cache is pinned by hand.

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _fakes import FakeStore, ScriptedBackend  # type: ignore[import-not-found]
from persona.backends import BackendConfig, ChatResponse
from persona.backends.types import TokenUsage
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolResult
from persona.skills import SkillInjector
from persona.tools import Toolbox
from persona.tools.protocol import tool
from persona_runtime.agentic.loop import AgenticLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.agentic.run import Run
    from persona_runtime.agentic.step import StepType

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]


@tool(name="echo", description="Echo a message back.")
async def _echo_tool(message: str) -> ToolResult:
    return ToolResult(tool_name="echo", content=f"echoed: {message}", is_error=False)


def _resp(content: str = "") -> ChatResponse:
    return ChatResponse(
        content=content,
        tool_calls=[],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="claude-sonnet-4-6",
        provider="anthropic",
        latency_ms=1.0,
    )


def _make_loop(script: list[ChatResponse]) -> AgenticLoop:
    stores = {
        "identity": FakeStore(),
        "self_facts": FakeStore(),
        "worldview": FakeStore(),
        "episodic": FakeStore(),
    }
    registry = TierRegistry(
        {
            "frontier": TierConfig(name="frontier", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
        }
    )
    backend = ScriptedBackend([], chat_script=script)
    registry._cache = {"frontier": backend, "mid": backend, "small": backend}  # type: ignore[assignment]
    return AgenticLoop(
        persona=Persona(
            persona_id="astrid",
            identity=PersonaIdentity(
                name="Astrid",
                role="tenancy assistant",
                background="Knows husleieloven.",
                constraints=["Never give binding advice."],
            ),
        ),
        stores=stores,  # type: ignore[arg-type]
        toolbox=Toolbox([_echo_tool]),  # type: ignore[arg-type]
        skill_injector=SkillInjector(),
        scanned_skills=[],
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
    )


async def _run_with_tiers(
    per_step_tier: list[str],
    script: list[ChatResponse],
) -> tuple[Run, list[RunEvent]]:
    """Drive the real loop with a tier policy that grades each step differently.

    ``_tier_for_step`` is the loop's one tier-policy seam, and its signature keeps
    ``step_num``/``last_action`` precisely so a future re-grade needs no caller change.
    Replacing it here drives the production emit path with a run that switches tier,
    which today's uniform ``agentic_step`` policy cannot produce on its own. The step
    beyond the list repeats the last tier, so a short list means "and stay there".
    """
    loop = _make_loop(script)

    def _graded(step_num: int, last_action: StepType | None) -> str:
        del last_action
        return per_step_tier[min(step_num, len(per_step_tier) - 1)]

    loop._tier_for_step = _graded  # type: ignore[assignment,method-assign]

    events: list[RunEvent] = []

    async def on_event(ev: RunEvent) -> None:
        events.append(ev)

    run = await loop.run("draft a complaint", on_event=on_event)
    return run, events


def _tier_frames(events: list[RunEvent]) -> list[tuple[int, str]]:
    return [(e.step, str(e.data["tier"])) for e in events if e.type == "tier"]


class TestTierFrames:
    @pytest.mark.asyncio
    async def test_a_run_that_switches_tier_agrees_live_and_reopened(self) -> None:
        # The reopened half first: it is the half that already worked, and it is the
        # reference the live stream has to match. Three steps, the last on a different
        # tier from the first two.
        run, events = await _run_with_tiers(
            ["frontier", "frontier", "mid"],
            [_resp("thinking about it"), _resp("still thinking"), _resp("[FINAL] done")],
        )
        assert [s.tier_used for s in run.steps] == ["frontier", "frontier", "mid"]

        # And the live stream tells the same story: the tier at step 0, then the change
        # at the step that changed it. Collapsing the repeats out of the reopened
        # sequence leaves exactly the frames that went out.
        assert _tier_frames(events) == [(0, "frontier"), (2, "mid")]

    @pytest.mark.asyncio
    async def test_a_run_on_one_tier_emits_exactly_one_frame(self) -> None:
        # A repeat is not news. Three steps on one tier is one frame, not three, so the
        # badge is written once and the stream is not padded with restatements.
        _, events = await _run_with_tiers(
            ["frontier"],
            [_resp("thinking about it"), _resp("still thinking"), _resp("[FINAL] done")],
        )
        assert _tier_frames(events) == [(0, "frontier")]

    @pytest.mark.asyncio
    async def test_the_tier_frame_precedes_the_step_it_describes(self) -> None:
        # The badge lands right before the step's work shows up, the way the chat stream
        # puts the tier ahead of the answer; after the fact it is history.
        _, events = await _run_with_tiers(["frontier"], [_resp("[FINAL] done")])
        types = [e.type for e in events]
        assert types.index("started") < types.index("tier") < types.index("thinking")
