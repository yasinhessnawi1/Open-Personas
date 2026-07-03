"""Spec 23 T11 — IntelligentRouter wired through the ConversationLoop.

The load-bearing test here is the **backward-compat contract** (criterion 11 /
the merge-safety gate): a persona with no ``routing.intelligent`` block produces a
byte-identical routing decision whether or not an IntelligentRouter is injected.
The positive test proves the opt-in path enriches the decision + records it on the
TurnLog (criteria 1, 5, 10) end-to-end through the loop.

Scripted backend, no real model / network — lives in tests/unit/.
"""

from __future__ import annotations

import pytest
from _fakes import FakeStore, ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from persona.backends import BackendConfig
from persona.backends.model_metadata import ModelMetadata
from persona.backends.multi_model import MultiModelChatBackend
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import IntelligentRoutingConfig, Persona, PersonaIdentity, RoutingConfig
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.routing import HeuristicRouter, IntelligentRouter
from persona_runtime.tier import TierConfig, TierRegistry

_CFG = BackendConfig(provider="anthropic", model="primary", api_key="sk-test")


class _MapResolver:
    def __init__(self, table: dict[str, ModelMetadata]) -> None:
        self._table = table

    def resolve(self, model_id: str) -> ModelMetadata | None:
        return self._table.get(model_id)


def _md(*, quality: float, cost: float = 0.1) -> ModelMetadata:
    return ModelMetadata(
        cost_input_per_1k_tokens=cost,
        cost_output_per_1k_tokens=cost,
        latency_p50_ms=300.0,
        quality_benchmark=quality,
        tools_supported=True,
        vision_supported=True,
        context_length=200_000,
    )


def _multi_model_registry() -> TierRegistry:
    """A frontier tier whose backend is a 2-model wrapper (cheap, then good)."""
    subs = [
        ScriptedBackend(
            [ScriptedRound(text="cheap says hi")], provider_name="deepseek", model_name="cheap"
        ),
        ScriptedBackend(
            [ScriptedRound(text="good says hi")], provider_name="anthropic", model_name="good"
        ),
    ]
    wrapper = MultiModelChatBackend(subs, tier_name="frontier")  # type: ignore[arg-type]
    return TierRegistry(
        {
            "frontier": TierConfig(
                name="frontier", backend_config=_CFG, preconstructed_backend=wrapper
            )
        }
    )


def _persona(intelligent: IntelligentRoutingConfig | None = None) -> Persona:
    routing = RoutingConfig(intelligent=intelligent) if intelligent is not None else RoutingConfig()
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(name="Astrid", role="assistant", background="bg", constraints=[]),
        routing=routing,
    )


def _build_loop(
    *, persona: Persona, registry: TierRegistry, intelligent_router: IntelligentRouter | None
) -> tuple[ConversationLoop, MemoryTurnLogWriter]:
    writer = MemoryTurnLogWriter()
    loop = ConversationLoop(
        persona=persona,
        stores={k: FakeStore() for k in ("identity", "self_facts", "worldview", "episodic")},  # type: ignore[arg-type, misc]
        toolbox=Toolbox([], allow_list=None),  # type: ignore[arg-type]
        skill_scanner=SkillScanner([]),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
        prompt_builder=PromptBuilder(),
        router=HeuristicRouter(tier_registry=registry),
        tier_registry=registry,
        turn_log_writer=writer,
        intelligent_router=intelligent_router,
    )
    return loop, writer


def _conversation() -> Conversation:
    return Conversation(conversation_id="c1", persona_id="astrid", messages=[])


class TestBackwardCompatContract:
    """Criterion 11 / merge-safety gate: feature OFF ⇒ byte-identical decision."""

    @pytest.mark.asyncio
    async def test_no_intelligent_router_leaves_decision_unchanged(self) -> None:
        loop, writer = _build_loop(
            persona=_persona(), registry=_multi_model_registry(), intelligent_router=None
        )
        async for _ in loop.turn(_conversation(), "hello"):
            pass
        d = writer.logs[0].routing_decision
        assert d is not None
        # No model-selection happened: all Spec 23 fields at their defaults.
        assert d.model_candidates == ()
        assert d.score_vector == {}
        assert d.weights_used == {}
        assert d.model_fallback_engaged is False
        assert d.model_fallback_reason is None

    @pytest.mark.asyncio
    async def test_router_present_but_disabled_is_identical_to_absent(self) -> None:
        # A persona WITHOUT a routing.intelligent block defaults enabled=False, so
        # injecting a router must change nothing — proves the gate, not just the
        # default wiring.
        resolver = _MapResolver(
            {"anthropic/good": _md(quality=0.95), "deepseek/cheap": _md(quality=0.5)}
        )

        loop_off, w_off = _build_loop(
            persona=_persona(), registry=_multi_model_registry(), intelligent_router=None
        )
        loop_disabled, w_disabled = _build_loop(
            persona=_persona(),
            registry=_multi_model_registry(),
            intelligent_router=IntelligentRouter(
                tier_registry=_multi_model_registry(), metadata_resolver=resolver
            ),
        )
        async for _ in loop_off.turn(_conversation(), "hello"):
            pass
        async for _ in loop_disabled.turn(_conversation(), "hello"):
            pass

        d_off = w_off.logs[0].routing_decision
        d_disabled = w_disabled.logs[0].routing_decision
        assert d_off is not None
        assert d_disabled is not None
        # Byte-identical decision: same tier + model + all model-selection fields.
        assert d_off.tier == d_disabled.tier
        assert d_off.model == d_disabled.model
        assert d_disabled.model_fallback_engaged is False
        assert d_disabled.model_candidates == ()


class TestOptInPath:
    """Criterion 1 / 5 / 10: enabled ⇒ metadata-driven model choice, recorded."""

    @pytest.mark.asyncio
    async def test_enabled_picks_highest_scorer_and_records_it(self) -> None:
        resolver = _MapResolver(
            {"anthropic/good": _md(quality=0.95), "deepseek/cheap": _md(quality=0.50)}
        )
        registry = _multi_model_registry()
        loop, writer = _build_loop(
            persona=_persona(IntelligentRoutingConfig(enabled=True)),
            registry=registry,
            intelligent_router=IntelligentRouter(
                tier_registry=registry, metadata_resolver=resolver
            ),
        )
        async for _ in loop.turn(_conversation(), "hello"):
            pass
        d = writer.logs[0].routing_decision
        assert d is not None
        # Default (quality-led) weights → the higher-quality model wins, even
        # though it sits at slot 1 in the MODELS list (reorder_primary moved it).
        assert d.model == "anthropic/good"
        assert d.model_fallback_engaged is False
        assert set(d.model_candidates) == {"anthropic/good", "deepseek/cheap"}
        assert set(d.score_vector) == {"cost", "quality", "latency"}
        assert d.weights_used == {"cost": 0.40, "quality": 0.50, "latency": 0.10}

    @pytest.mark.asyncio
    async def test_metadata_miss_degrades_gracefully(self) -> None:
        # Empty resolver → every candidate misses → degrade to rule-based slot-0,
        # turn still completes (criterion 9).
        registry = _multi_model_registry()
        loop, writer = _build_loop(
            persona=_persona(IntelligentRoutingConfig(enabled=True)),
            registry=registry,
            intelligent_router=IntelligentRouter(
                tier_registry=registry, metadata_resolver=_MapResolver({})
            ),
        )
        chunks = [c async for c in loop.turn(_conversation(), "hello")]
        assert chunks[-1].is_final is True
        d = writer.logs[0].routing_decision
        assert d is not None
        assert d.model_fallback_engaged is True
        assert d.model_fallback_reason == "metadata_miss"


class TestPerDayBudgetDischarged:
    """Spec R7 (R7-D-1) discharges D-23-X: a configured per-day cap NO LONGER fails
    loud at construction — the durable cross-session spend source now exists
    (``turn_logs`` via an injected provider), so the soft per-day ramp runs for real."""

    def test_per_day_cap_with_intelligent_routing_constructs_cleanly(self) -> None:
        """The inverse of the old D-23-7 fail-loud: construction now SUCCEEDS."""
        from persona.schema.persona import RoutingBudgetConfig

        registry = _multi_model_registry()
        persona = Persona(
            persona_id="astrid",
            identity=PersonaIdentity(
                name="Astrid", role="assistant", background="bg", constraints=[]
            ),
            routing=RoutingConfig(
                intelligent=IntelligentRoutingConfig(enabled=True),
                budget=RoutingBudgetConfig(max_cents_per_day=500.0),
            ),
        )
        # No raise — a persona with a per-day cap + intelligent routing builds fine.
        loop = _build_loop(
            persona=persona,
            registry=registry,
            intelligent_router=IntelligentRouter(
                tier_registry=registry, metadata_resolver=_MapResolver({})
            ),
        )
        assert loop is not None

    def test_per_day_provider_feeds_the_soft_ramp(self) -> None:
        """A rising day-spend from the injected provider shifts the ramp toward cost
        (the soft per-day bias) — the discharge's functional half. The provider here
        stands in for the runtime_factory turn_logs reader; the integration test
        proves the real recorded-cost transition end-to-end."""
        from persona_runtime.routing import routing_budget
        from persona_runtime.routing.scoring import ProfileWeights

        base = ProfileWeights(cost=0.34, quality=0.33, latency=0.33)
        cap = 500.0
        # Below the 80% soft threshold → no bias yet.
        low = routing_budget.effective_weights(base, day_spent_cents=100.0, max_cents_per_day=cap)
        # Near/over the cap → bias moves toward cost.
        high = routing_budget.effective_weights(base, day_spent_cents=480.0, max_cents_per_day=cap)
        assert low.cost == base.cost, "no bias below the soft threshold"
        assert high.cost > low.cost, "the ramp must bias toward cost as day-spend rises"

    def test_per_day_cap_inert_when_intelligent_disabled(self) -> None:
        # Feature off → budget is inert by design; no error.
        from persona.schema.persona import RoutingBudgetConfig

        persona = Persona(
            persona_id="astrid",
            identity=PersonaIdentity(
                name="Astrid", role="assistant", background="bg", constraints=[]
            ),
            routing=RoutingConfig(budget=RoutingBudgetConfig(max_cents_per_day=500.0)),
        )
        # No raise.
        _build_loop(persona=persona, registry=_multi_model_registry(), intelligent_router=None)


class TestSpec31WireSummary:
    """Spec 31 (D-31-1/2): the model-decision + budget reach the wire additively."""

    @staticmethod
    async def _capture(loop: ConversationLoop) -> list[object]:
        events: list[object] = []

        async def on_event(ev: object) -> None:
            events.append(ev)

        async for _ in loop.turn(_conversation(), "hello", on_event):  # type: ignore[arg-type]
            pass
        return events

    @pytest.mark.asyncio
    async def test_tier_event_carries_routing_summary_when_intelligent(self) -> None:
        resolver = _MapResolver(
            {"anthropic/good": _md(quality=0.95), "deepseek/cheap": _md(quality=0.50)}
        )
        registry = _multi_model_registry()
        loop, _ = _build_loop(
            persona=_persona(IntelligentRoutingConfig(enabled=True)),
            registry=registry,
            intelligent_router=IntelligentRouter(
                tier_registry=registry, metadata_resolver=resolver
            ),
        )
        events = await self._capture(loop)
        tier_events = [e for e in events if e.type == "tier"]  # type: ignore[attr-defined]
        assert len(tier_events) == 1
        summary = tier_events[0].data.get("routing")  # type: ignore[attr-defined]
        assert summary is not None
        assert summary["chosen_model"] == "anthropic/good"
        # Default weights are quality-led (0.50) → quality is the dominant axis.
        assert summary["dominant_factor"] == "quality"
        assert summary["model_fallback_engaged"] is False
        assert summary["model_fallback_reason"] is None
        # The raw score vector is NEVER on the wire (D-31-1).
        assert "score_vector" not in summary

    @pytest.mark.asyncio
    async def test_tier_event_omits_routing_summary_when_rule_based(self) -> None:
        loop, _ = _build_loop(
            persona=_persona(), registry=_multi_model_registry(), intelligent_router=None
        )
        events = await self._capture(loop)
        tier_events = [e for e in events if e.type == "tier"]  # type: ignore[attr-defined]
        assert len(tier_events) == 1
        # Back-compat: no routing key on a rule-based turn ⇒ bare-tier payload.
        assert "routing" not in tier_events[0].data  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    async def test_fallback_summary_reports_metadata_miss_and_no_dominant(self) -> None:
        registry = _multi_model_registry()
        loop, _ = _build_loop(
            persona=_persona(IntelligentRoutingConfig(enabled=True)),
            registry=registry,
            intelligent_router=IntelligentRouter(
                tier_registry=registry, metadata_resolver=_MapResolver({})
            ),
        )
        events = await self._capture(loop)
        summary = [e for e in events if e.type == "tier"][0].data["routing"]  # type: ignore[attr-defined]
        assert summary["model_fallback_engaged"] is True
        assert summary["model_fallback_reason"] == "metadata_miss"
        # No weights were used on the fallback ⇒ no dominant factor (honest).
        assert summary["dominant_factor"] is None

    @pytest.mark.asyncio
    async def test_session_spend_and_budget_snapshot_include_current_turn(self) -> None:
        from persona.schema.persona import RoutingBudgetConfig

        resolver = _MapResolver(
            {"anthropic/good": _md(quality=0.95), "deepseek/cheap": _md(quality=0.50)}
        )
        registry = _multi_model_registry()
        persona = Persona(
            persona_id="astrid",
            identity=PersonaIdentity(
                name="Astrid", role="assistant", background="bg", constraints=[]
            ),
            routing=RoutingConfig(
                intelligent=IntelligentRoutingConfig(enabled=True),
                budget=RoutingBudgetConfig(max_cents_per_session=50.0),
            ),
        )
        loop, _ = _build_loop(
            persona=persona,
            registry=registry,
            intelligent_router=IntelligentRouter(
                tier_registry=registry, metadata_resolver=resolver
            ),
        )
        assert loop.session_spent_cents == 0.0
        async for _ in loop.turn(_conversation(), "hello"):
            pass
        snap = loop.budget_snapshot()
        assert snap is not None
        assert snap["max_cents_per_session"] == 50.0
        assert "max_cents_per_turn" not in snap  # unset caps omitted
        assert "max_cents_per_day" not in snap
        # Spend reflects the just-completed turn (read post-turn).
        assert snap["session_spent_cents"] == loop.session_spent_cents

    def test_budget_snapshot_none_when_disabled_or_no_cap(self) -> None:
        from persona.schema.persona import RoutingBudgetConfig

        registry = _multi_model_registry()
        # Enabled but no cap configured ⇒ no indicator.
        loop_no_cap, _ = _build_loop(
            persona=_persona(IntelligentRoutingConfig(enabled=True)),
            registry=registry,
            intelligent_router=IntelligentRouter(
                tier_registry=registry, metadata_resolver=_MapResolver({})
            ),
        )
        assert loop_no_cap.budget_snapshot() is None

        # A cap set but intelligent routing OFF ⇒ inert ⇒ no indicator.
        persona = Persona(
            persona_id="a",
            identity=PersonaIdentity(name="A", role="r", background="b", constraints=[]),
            routing=RoutingConfig(budget=RoutingBudgetConfig(max_cents_per_turn=5.0)),
        )
        loop_off, _ = _build_loop(persona=persona, registry=registry, intelligent_router=None)
        assert loop_off.budget_snapshot() is None


class TestTurnLogJsonlCarriesModelSelection:
    """Criterion 10: the model-selection audit trail survives JSONL serialisation."""

    @pytest.mark.asyncio
    async def test_jsonl_round_trip_carries_model_selection_fields(self, tmp_path: object) -> None:
        import json
        from pathlib import Path

        from persona_runtime.logging import JSONLTurnLogWriter

        root = Path(str(tmp_path))
        resolver = _MapResolver(
            {"anthropic/good": _md(quality=0.95), "deepseek/cheap": _md(quality=0.50)}
        )
        registry = _multi_model_registry()
        writer = JSONLTurnLogWriter(root)
        loop = ConversationLoop(
            persona=_persona(IntelligentRoutingConfig(enabled=True)),
            stores={k: FakeStore() for k in ("identity", "self_facts", "worldview", "episodic")},  # type: ignore[arg-type, misc]
            toolbox=Toolbox([], allow_list=None),  # type: ignore[arg-type]
            skill_scanner=SkillScanner([]),
            skill_injector=SkillInjector(),
            scanned_skills=[],
            history_manager=ConversationHistoryManager(compact_every=10, keep_recent=5),
            prompt_builder=PromptBuilder(),
            router=HeuristicRouter(tier_registry=registry),
            tier_registry=registry,
            turn_log_writer=writer,
            intelligent_router=IntelligentRouter(
                tier_registry=registry, metadata_resolver=resolver
            ),
        )
        async for _ in loop.turn(_conversation(), "hello"):
            pass

        line = (root / "c1.jsonl").read_text(encoding="utf-8").strip()
        payload = json.loads(line)
        rd = payload["routing_decision"]
        assert rd["model"] == "anthropic/good"
        assert rd["model_fallback_engaged"] is False
        assert set(rd["model_candidates"]) == {"anthropic/good", "deepseek/cheap"}
        assert set(rd["score_vector"]) == {"cost", "quality", "latency"}
        assert rd["weights_used"] == {"cost": 0.40, "quality": 0.50, "latency": 0.10}
