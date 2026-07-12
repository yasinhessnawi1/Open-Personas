"""T22 — Spec 25 cross-spec integration sweep.

Verifies the new Spec 25 telemetry surfaces wire end-to-end through the turn
loop into the TurnLog, and that the additive fields coexist with the Spec 18
fields (D-18-1 NOT reopened). Marked ``integration`` (excluded from the default
run); uses scripted fakes — no network.

Surfaces swept:
- ``cost_basis`` (T13) populated per turn from the backend's provider/model.
- ``sandbox_session_recreated`` (T22 wiring of T09's tool-metadata flag).
- ``tool_refusal_detected`` (T11/T12) + ``fallback_rate_alert`` (T12) +
  ``refusal_retry_engaged`` (T21) all coexist + JSON round-trip.
- The full additive field set does not perturb the Spec 18 fallback fields.
"""

# ruff: noqa: SLF001, ARG001
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from _fakes import FakeStore, ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from persona.backends import BackendConfig
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolResult
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona.tools.protocol import tool
from persona_runtime.logging import MemoryTurnLogWriter, TurnLog
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

pytestmark = pytest.mark.integration

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]


@tool(name="recovering_tool", description="A tool that reports a recovered sandbox session.")
async def _recovering_tool(x: str = "y") -> ToolResult:
    # Mimics the sandbox wrapper's auto-recovery telemetry (T09): the per-call
    # flag is a string in the dict[str,str] metadata.
    return ToolResult(
        tool_name="recovering_tool",
        content="ok",
        is_error=False,
        metadata={"sandbox_session_recreated": "True"},
    )


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(name="A", role="r", background="b", constraints=["c"]),
    )


def _make_loop(backend: ScriptedBackend) -> tuple[ConversationLoop, MemoryTurnLogWriter]:
    toolbox = Toolbox([_recovering_tool], allow_list=None)  # type: ignore[arg-type]
    registry = TierRegistry({"mid": TierConfig(name="mid", backend_config=_DUMMY_CFG)})
    registry._cache = {"mid": backend}  # type: ignore[assignment]
    writer = MemoryTurnLogWriter()
    loop = ConversationLoop(
        persona=_persona(),
        stores={k: FakeStore() for k in ("identity", "self_facts", "worldview", "episodic")},  # type: ignore[arg-type]
        toolbox=toolbox,
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        turn_log_writer=writer,
    )
    return loop, writer


def _conv() -> Conversation:
    return Conversation(conversation_id="c1", persona_id="astrid", messages=[])


@pytest.mark.asyncio
async def test_cost_basis_estimate_static_for_nvidia_turn() -> None:
    # Spec M2 (D-M2-1): the bare NVIDIA model name canonicalises to the
    # provider-prefixed static-table id — basis is the resolver provenance
    # (the old "verify-at-deploy" flag lived in the deleted ``_PRICE_TABLE``).
    backend = ScriptedBackend(
        [ScriptedRound(text="hi")],
        provider_name="nvidia",
        model_name="llama-3.3-nemotron-super-49b-v1.5",
    )
    loop, writer = _make_loop(backend)
    _ = [c async for c in loop.turn(_conv(), "hello")]
    assert writer.logs[-1].cost_basis == "estimate_static"


@pytest.mark.asyncio
async def test_cost_basis_estimate_static_for_anthropic_turn() -> None:
    # The M2-T1 parity row (anthropic/claude-sonnet-4-6) prices the deployed
    # frontier fallback through the static chain — a bare loop with no
    # injected cost_source uses the zero-network static-only default.
    backend = ScriptedBackend(
        [ScriptedRound(text="hi")], provider_name="anthropic", model_name="claude-sonnet-4-6"
    )
    loop, writer = _make_loop(backend)
    _ = [c async for c in loop.turn(_conv(), "hello")]
    assert writer.logs[-1].cost_basis == "estimate_static"
    assert writer.logs[-1].cost_cents > 0.0


@pytest.mark.asyncio
async def test_cost_basis_unpriced_for_unknown_model_turn() -> None:
    # Honest degrade (M2 §2): no metadata anywhere → unpriced + cost 0.0,
    # never a guess.
    backend = ScriptedBackend(
        [ScriptedRound(text="hi")], provider_name="acme", model_name="mystery-sweep-model"
    )
    loop, writer = _make_loop(backend)
    _ = [c async for c in loop.turn(_conv(), "hello")]
    assert writer.logs[-1].cost_basis == "unpriced"
    assert writer.logs[-1].cost_cents == 0.0


@pytest.mark.asyncio
async def test_sandbox_session_recreated_flows_to_turnlog() -> None:
    # Round 1 calls the recovering tool; round 2 produces final text.
    backend = ScriptedBackend(
        [
            ScriptedRound(tool_name="recovering_tool", tool_args={"x": "y"}),
            ScriptedRound(text="done"),
        ]
    )
    loop, writer = _make_loop(backend)
    _ = [c async for c in loop.turn(_conv(), "run code")]
    assert writer.logs[-1].sandbox_session_recreated is True


@pytest.mark.asyncio
async def test_no_session_recreation_default_false() -> None:
    backend = ScriptedBackend([ScriptedRound(text="hi")])
    loop, writer = _make_loop(backend)
    _ = [c async for c in loop.turn(_conv(), "hello")]
    assert writer.logs[-1].sandbox_session_recreated is False


def test_all_spec25_fields_coexist_and_roundtrip() -> None:
    # Construct a TurnLog with every additive Spec 25 field set + the Spec 18
    # fallback fields, and confirm JSON round-trip preserves all of them
    # (D-18-1 fields untouched, invariants still hold).
    log = TurnLog(
        conversation_id="c1",
        turn_index=0,
        tier_used="mid",
        model_name="m",
        provider="nvidia",
        prompt_tokens=10,
        completion_tokens=5,
        latency_ms=1.0,
        cost_cents=0.04,
        cost_basis="estimate_static",
        fallback_rate_alert=True,
        tool_refusal_detected=["generate_image"],
        refusal_retry_engaged=True,
        sandbox_session_recreated=True,
        timestamp=datetime.now(UTC),
        tier_fallback_count=1,
        tier_fallback_reasons=["RateLimitError"],
        tier_fallback_providers=["nvidia"],
        fallback_engaged=True,
    )
    restored = TurnLog.model_validate_json(log.model_dump_json())
    assert restored.cost_basis == "estimate_static"
    assert restored.fallback_rate_alert is True
    assert restored.tool_refusal_detected == ["generate_image"]
    assert restored.refusal_retry_engaged is True
    assert restored.sandbox_session_recreated is True
    # Spec 18 fields intact (D-18-1 invariants).
    assert restored.fallback_engaged is True
    assert restored.tier_fallback_count == 1
    assert restored.tier_fallback_reasons == ["RateLimitError"]
