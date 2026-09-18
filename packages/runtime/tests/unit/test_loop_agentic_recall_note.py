"""The run's memory recall survives the run (part3 F10; the R9-157 notes channel).

The loop has emitted one ``memory_recall`` event per typed store since P2, but the
terminal write persists STEPS, not the event log, so the recall existed only while
someone was watching. These tests pin both halves at once: the live events stay exactly
as they were, and the same recall is on step 0's ``notes`` of the returned ``Run``, which
is what a reopened run is rebuilt from.
"""

# ruff: noqa: SLF001 — mirrors test_loop_agentic.py: the registry cache is pinned by hand.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _fakes import FakeStore, ScriptedBackend  # type: ignore[import-not-found]
from persona.backends import BackendConfig, ChatResponse
from persona.backends.types import TokenUsage
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolCall, ToolResult
from persona.skills import SkillInjector
from persona.tools import Toolbox
from persona.tools.protocol import tool
from persona_runtime.agentic.events import RunEvent  # noqa: TC002 — used in a fixture signature
from persona_runtime.agentic.loop import AgenticLoop
from persona_runtime.agentic.step import MemoryRecallNote, StepType
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]


@tool(name="echo", description="Echo a message back.")
async def _echo_tool(message: str) -> ToolResult:
    return ToolResult(tool_name="echo", content=f"echoed: {message}", is_error=False)


def _chunk(text: str) -> PersonaChunk:
    now = datetime.now(UTC)
    return PersonaChunk(
        id=f"astrid::self_facts::{abs(hash(text)) % 10000:04d}",
        text=text,
        created_at=now,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM,
            logical_id="l1",
            version=1,
            written_at=now,
            written_by="test",
        ),
    )


def _resp(content: str = "", *, tool_calls: list[ToolCall] | None = None) -> ChatResponse:
    return ChatResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="claude-sonnet-4-6",
        provider="anthropic",
        latency_ms=1.0,
    )


def _make_loop(script: list[ChatResponse]) -> AgenticLoop:
    """A loop whose typed stores return different, non-uniform recall counts."""
    stores: dict[str, FakeStore] = {
        "identity": FakeStore(),
        "self_facts": FakeStore(query_results=[_chunk("prefers plain Norwegian")]),
        "worldview": FakeStore(),
        "episodic": FakeStore(query_results=[_chunk("earlier run"), _chunk("older run")]),
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


async def _run(script: list[ChatResponse]) -> tuple[object, list[RunEvent]]:
    events: list[RunEvent] = []

    async def on_event(ev: RunEvent) -> None:
        events.append(ev)

    run = await _make_loop(script).run("draft a complaint", on_event=on_event)
    return run, events


class TestRecallNote:
    @pytest.mark.asyncio
    async def test_recall_is_on_step_zero_and_on_the_live_stream(self) -> None:
        # The record and the stream tell the same story: one entry per typed store,
        # same order, same counts. Losing either half is the F10 defect.
        run, events = await _run(
            [
                _resp(tool_calls=[ToolCall(name="echo", args={"message": "x"}, call_id="c1")]),
                _resp("[FINAL] done"),
            ]
        )

        live = [(e.data["store"], e.data["count"]) for e in events if e.type == "memory_recall"]
        assert live == [
            ("identity", 0),
            ("self_facts", 1),
            ("worldview", 0),
            ("episodic", 2),
        ]

        notes = [n for n in run.steps[0].notes if isinstance(n, MemoryRecallNote)]  # type: ignore[attr-defined]
        assert [(n.store, n.count) for n in notes] == live

    @pytest.mark.asyncio
    async def test_recall_lands_on_the_first_step_only(self) -> None:
        # The recall feeds the run's whole initial context, which step 0 consumes; a
        # later step never read it, so it must not claim it.
        run, _ = await _run(
            [
                _resp(tool_calls=[ToolCall(name="echo", args={"message": "x"}, call_id="c1")]),
                _resp("[FINAL] done"),
            ]
        )
        later = [
            n
            for step in run.steps[1:]  # type: ignore[attr-defined]
            for n in step.notes
            if isinstance(n, MemoryRecallNote)
        ]
        assert later == []

    @pytest.mark.asyncio
    async def test_the_note_survives_the_json_round_trip(self) -> None:
        # The terminal record is ``Step.model_dump(mode="json")``, so what the web can
        # ever read is what that dump carries.
        run, _ = await _run([_resp("[FINAL] done")])
        dumped = run.steps[0].model_dump(mode="json")  # type: ignore[attr-defined]
        assert {"kind": "memory_recall", "store": "self_facts", "count": 1} in dumped["notes"]

    @pytest.mark.asyncio
    async def test_a_guard_note_on_the_first_step_is_not_displaced(self) -> None:
        # Step 0 can already carry its own notes (the ledger, the pruner). The recall is
        # appended to them, never written over them.
        run, _ = await _run(
            [
                _resp(
                    tool_calls=[
                        ToolCall(name="nope", args={}, call_id="c1"),
                        ToolCall(name="nope", args={}, call_id="c2"),
                    ]
                ),
                _resp("[FINAL] done"),
            ]
        )
        kinds = [n.kind for n in run.steps[0].notes]  # type: ignore[attr-defined]
        assert "call_skipped" in kinds
        assert kinds.count("memory_recall") == 4
        assert run.steps[0].type is StepType.TOOL_CALL  # type: ignore[attr-defined]
