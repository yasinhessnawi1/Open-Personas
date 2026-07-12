"""M2-T6 BINDING test — the turn path performs ZERO catalog fetches (D-M2-6 / M2 §2).

The spec's binding gate: "prove the turn path never fetches: test asserts zero
client calls during a turn." Driven at the REAL trigger level — a real
:class:`ConversationLoop` turn whose ``cost_source`` is the REAL chain
(static + OpenRouter resolver over a COUNTING transport):

* **cold**: a full turn with a catalog-only model performs 0 HTTP requests —
  the cold index is an honest miss (``unpriced``), never a blocking fetch
  (the F5 hazard, closed);
* **warm off-turn, then turn**: one off-turn ``refresh_if_stale`` +
  ``reindex`` (exactly 1 request), then the turn prices the catalog-only
  model (``estimate_catalog``) with the request count STILL 1 — the catalog
  arm serves at turn time fetch-free.

``@pytest.mark.integration`` — excluded from the default run. No network:
the transport is an ``httpx.MockTransport``.
"""

# ruff: noqa: SLF001
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import pytest
from _fakes import FakeStore, ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from persona.backends import BackendConfig
from persona.backends.metadata import (
    ChainedModelMetadataResolver,
    OpenRouterModelMetadataResolver,
    StaticModelMetadataResolver,
)
from persona.backends.openrouter_catalog import OpenRouterCatalogClient
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from persona_runtime.cost import CostSource

pytestmark = pytest.mark.integration

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]

#: A model that exists ONLY in the (scripted) OpenRouter catalog — the static
#: tables cannot price it, so any estimate must come through the catalog arm.
_CATALOG_ONLY_MODEL = "longtail/only-in-catalog"

_CATALOG_PAYLOAD: dict[str, Any] = {
    "data": [
        {
            "id": _CATALOG_ONLY_MODEL,
            "context_length": 128000,
            "pricing": {"prompt": "0.000001", "completion": "0.000002"},
            "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            "supported_parameters": ["tools"],
        }
    ]
}


class _Counter:
    def __init__(self) -> None:
        self.requests = 0


def _counting_chain() -> tuple[
    CostSource, _Counter, OpenRouterCatalogClient, OpenRouterModelMetadataResolver
]:
    """The REAL resolver chain over a counting transport."""
    counter = _Counter()

    def handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
        counter.requests += 1
        return httpx.Response(200, json=_CATALOG_PAYLOAD)

    client = OpenRouterCatalogClient(
        "sk-or-test", transport=httpx.MockTransport(handler), ttl_s=3600.0
    )
    or_resolver = OpenRouterModelMetadataResolver(client)
    chain = ChainedModelMetadataResolver(
        static=StaticModelMetadataResolver(), openrouter=or_resolver
    )
    return chain, counter, client, or_resolver


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(name="A", role="r", background="b", constraints=["c"]),
    )


def _make_loop(
    backend: ScriptedBackend, cost_source: CostSource
) -> tuple[ConversationLoop, MemoryTurnLogWriter]:
    toolbox = Toolbox([], allow_list=None)  # type: ignore[arg-type]
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
        cost_source=cost_source,
    )
    return loop, writer


def _conv() -> Conversation:
    return Conversation(conversation_id="c1", persona_id="astrid", messages=[])


def _or_backend() -> ScriptedBackend:
    return ScriptedBackend(
        [ScriptedRound(text="hello")],
        provider_name="openrouter",
        model_name=_CATALOG_ONLY_MODEL,
    )


@pytest.mark.asyncio
async def test_cold_index_turn_performs_zero_fetches() -> None:
    """THE binding assert: a real turn drives ZERO catalog HTTP requests."""
    chain, counter, _client, _resolver = _counting_chain()
    loop, writer = _make_loop(_or_backend(), chain)

    _ = [c async for c in loop.turn(_conv(), "hi")]

    assert counter.requests == 0  # pricing NEVER blocks or delays a turn
    log = writer.logs[-1]
    # Cold catalog + static miss = the honest degrade, not a fetch.
    assert log.cost_basis == "unpriced"
    assert log.cost_cents == 0.0


@pytest.mark.asyncio
async def test_warmed_index_serves_catalog_estimates_fetch_free() -> None:
    """Warm OFF-turn (1 fetch), then the turn prices via the catalog arm —
    request count unchanged."""
    chain, counter, client, or_resolver = _counting_chain()
    # The lifespan freshness step, exactly as production runs it (off-turn).
    assert client.refresh_if_stale() is True
    or_resolver.reindex()
    assert counter.requests == 1

    loop, writer = _make_loop(_or_backend(), chain)
    _ = [c async for c in loop.turn(_conv(), "hi")]

    assert counter.requests == 1  # the turn added ZERO requests
    log = writer.logs[-1]
    assert log.cost_basis == "estimate_catalog"
    # 10/5 tokens at 0.1 / 0.2 cents-per-1k (the scripted catalog pricing).
    assert log.cost_cents == pytest.approx((10 / 1000) * 0.1 + (5 / 1000) * 0.2)
