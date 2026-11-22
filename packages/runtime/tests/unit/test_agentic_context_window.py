"""The agentic compaction budget: a cost knob, bounded by the served models' window (R9-314).

No production backend exposes ``max_tokens``, so the loop compacted every run against a
4,096 token window. Simply raising that to the real window turned out to cost MORE per
run: steps on the frontier tier then carry the whole uncompacted context, which outweighs
the small-tier compactions it saves. So the budget is now ``min(cost budget, window)``:

- the cost budget is a setting whose default (4,096) reproduces today's behaviour, so a
  default deploy pays exactly what it paid before;
- the window, read from the metadata the runtime prices with, is a hard upper bound, so
  a raised budget can never exceed what every model in the chain accepts;
- a model whose window is unknown pins that bound to the conservative 4,096, with one
  WARNING per model set that says whether the catalog is cold or the model is missing.

Nothing here reaches a provider: the backends are scripted and the metadata source is
an in-memory fake.
"""

# ruff: noqa: SLF001 - tests reset the once-only log memory.

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

import pytest
from _fakes import FakeStore, ScriptedBackend  # type: ignore[import-not-found]
from loguru import logger as _loguru_logger
from persona.backends import BackendConfig, ChatResponse
from persona.backends.model_metadata import ModelMetadata
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import TokenUsage
from persona.schema.persona import Persona, PersonaIdentity
from persona.skills import SkillInjector, count_tokens
from persona.tools import Toolbox
from persona_runtime.agentic import context_window
from persona_runtime.agentic.compactor import StepHistoryCompactor
from persona_runtime.agentic.context_window import (
    DEFAULT_COMPACTION_BUDGET_TOKENS,
    FALLBACK_CONTEXT_WINDOW,
    REPLY_RESERVE_TOKENS,
    compaction_budget,
)
from persona_runtime.agentic.loop import AgenticLoop
from persona_runtime.agentic.run import RunStatus
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persona.schema.conversation import ConversationMessage

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]
_WIDE = 128_000
_NARROW = 20_000
_RAISED = 200_000  # a budget far above any window here: the window must bound it


class _FakeWindows:
    """An in-memory metadata source: model id to context window, nothing else.

    ``catalog_warm`` mirrors the shared chain's report of its OpenRouter index; ``None``
    means a source with no catalog at all.
    """

    def __init__(self, windows: dict[str, int], *, catalog_warm: bool | None = None) -> None:
        self._windows = windows
        self.catalog_warm = catalog_warm
        self.lookups: list[tuple[str, bool]] = []

    def resolve_with_source(
        self, model_id: str, *, allow_fetch: bool = True
    ) -> tuple[ModelMetadata, Literal["static", "catalog"]] | None:
        self.lookups.append((model_id, allow_fetch))
        window = self._windows.get(model_id)
        if window is None:
            return None
        metadata = ModelMetadata(
            cost_input_per_1k_tokens=0.1,
            cost_output_per_1k_tokens=0.2,
            latency_p50_ms=100.0,
            quality_benchmark=0.5,
            tools_supported=True,
            vision_supported=False,
            context_length=window,
        )
        return metadata, "static"


class _BrokenSource:
    """A metadata source that fails: the loop must not fail with it."""

    def resolve_with_source(
        self, model_id: str, *, allow_fetch: bool = True
    ) -> tuple[ModelMetadata, Literal["static", "catalog"]] | None:
        raise RuntimeError(f"catalog index unavailable for {model_id} ({allow_fetch})")


@pytest.fixture(autouse=True)
def _fresh_log_memory() -> Iterator[None]:
    """The warnings log once per process; each test starts from a clean memory."""
    context_window._reset_warnings()
    yield
    context_window._reset_warnings()


@pytest.fixture
def warnings_seen() -> Iterator[list[str]]:
    """Loguru sink for WARNING and up (persona.logging wraps loguru; caplog is blind)."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


def _backend(
    provider: str, model: str, script: list[ChatResponse] | None = None
) -> ScriptedBackend:
    return ScriptedBackend([], provider_name=provider, model_name=model, chat_script=script)


def _chain(*models: str) -> MultiModelChatBackend:
    return MultiModelChatBackend(
        [_backend("acme", m) for m in models],  # type: ignore[list-item]
        max_retries_per_backend=0,
    )


def _threshold(budget: int) -> int:
    """The token count above which the compactor fires for ``budget`` (spec section 6)."""
    return int(budget * 0.8)


def _words(tokens: int) -> str:
    """Text of about ``tokens`` cl100k tokens (" alpha" is one token)."""
    return " ".join(["alpha"] * tokens)


def _context_of(tokens: int, *, messages: int = 8) -> list[ConversationMessage]:
    """A context of ``messages`` messages totalling about ``tokens`` tokens."""
    from datetime import UTC, datetime

    from persona.schema.conversation import ConversationMessage

    share = max(1, tokens // messages)
    return [
        ConversationMessage(
            role="system" if i == 0 else "user", content=_words(share), created_at=datetime.now(UTC)
        )
        for i in range(messages)
    ]


def _rendered_tokens(context: list[ConversationMessage]) -> int:
    return count_tokens("\n".join(f"{m.role}: {m.content}" for m in context))


def _fallback_lines(seen: list[str]) -> list[str]:
    return [line for line in seen if "context window" in line]


# ----- the budget itself ----------------------------------------------------


def test_the_default_budget_is_todays_4096() -> None:
    assert DEFAULT_COMPACTION_BUDGET_TOKENS == FALLBACK_CONTEXT_WINDOW == 4096


def test_by_default_a_known_wide_window_still_compacts_at_todays_threshold() -> None:
    """Cost neutrality: the default budget, not the 128k window, is what binds."""
    source = _FakeWindows({"acme/wide-model": _WIDE})
    step = _backend("acme", "wide-model")

    assert compaction_budget(step, step, source=source) == 4096


def test_a_raised_budget_sizes_the_threshold_below_the_window() -> None:
    source = _FakeWindows({"acme/wide-model": _WIDE})
    step = _backend("acme", "wide-model")

    budget = compaction_budget(step, step, source=source, cost_budget=50_000)

    assert budget == 50_000
    compactor = StepHistoryCompactor()
    context = _context_of(3_400)
    assert _rendered_tokens(context) > _threshold(FALLBACK_CONTEXT_WINDOW)  # the old trigger
    assert compactor.should_compact(context, budget) is False
    # The lookup is the pricer's: the canonical id, and never a network fetch.
    assert source.lookups == [("acme/wide-model", False)]


def test_the_window_bounds_a_raised_budget() -> None:
    source = _FakeWindows({"acme/wide-model": _WIDE})
    step = _backend("acme", "wide-model")

    budget = compaction_budget(step, step, source=source, cost_budget=_RAISED)

    assert budget == _WIDE - REPLY_RESERVE_TOKENS


def test_a_chain_is_bounded_by_its_smallest_member() -> None:
    """The next step can land on any member of the chain, so the context must fit them all."""
    source = _FakeWindows({"acme/wide-model": _WIDE, "acme/narrow-model": _NARROW})
    chain = _chain("wide-model", "narrow-model")

    budget = compaction_budget(chain, chain, source=source, cost_budget=_RAISED)

    assert budget == _NARROW - REPLY_RESERVE_TOKENS


def test_the_summariser_window_bounds_the_budget() -> None:
    """A compaction hands the middle of the context to the background tier, so a context
    that outgrew the summariser's window could not be compacted at all."""
    source = _FakeWindows({"acme/wide-model": _WIDE, "acme/small-model": _NARROW})

    budget = compaction_budget(
        _backend("acme", "wide-model"),
        _backend("acme", "small-model"),
        source=source,
        cost_budget=_RAISED,
    )

    assert budget == _NARROW - REPLY_RESERVE_TOKENS


def test_an_unknown_member_pins_the_window_bound_to_the_fallback(
    warnings_seen: list[str],
) -> None:
    """An unknown fallback could be the small one, so a raised budget cannot be trusted."""
    source = _FakeWindows({"acme/wide-model": _WIDE})
    chain = _chain("wide-model", "mystery-model")

    first = compaction_budget(chain, chain, source=source, cost_budget=_RAISED)
    second = compaction_budget(chain, chain, source=source, cost_budget=_RAISED)

    assert first == second == FALLBACK_CONTEXT_WINDOW
    (line,) = _fallback_lines(warnings_seen)  # once per model set
    assert "acme/mystery-model" in line
    assert "not in any metadata table" in line


def test_an_unknown_summariser_pins_the_window_bound_too(warnings_seen: list[str]) -> None:
    source = _FakeWindows({"acme/wide-model": _WIDE})

    budget = compaction_budget(
        _backend("acme", "wide-model"),
        _backend("acme", "mystery-small"),
        source=source,
        cost_budget=_RAISED,
    )

    assert budget == FALLBACK_CONTEXT_WINDOW
    assert any("acme/mystery-small" in line for line in _fallback_lines(warnings_seen))


def test_an_unresolvable_summariser_is_an_unknown_window() -> None:
    source = _FakeWindows({"acme/wide-model": _WIDE})

    budget = compaction_budget(
        _backend("acme", "wide-model"), None, source=source, cost_budget=_RAISED
    )

    assert budget == FALLBACK_CONTEXT_WINDOW


def test_a_cold_catalog_is_named_as_the_reason(warnings_seen: list[str]) -> None:
    """While the OpenRouter index is cold, a miss says so: the operator waits, not edits."""
    source = _FakeWindows({}, catalog_warm=False)
    step = _backend("openrouter", "moonshotai/kimi-k3")

    assert compaction_budget(step, step, source=source, cost_budget=_RAISED) == 4096

    (line,) = _fallback_lines(warnings_seen)
    assert "catalog not warm yet" in line
    assert "moonshotai/kimi-k3" in line


def test_the_same_miss_logs_again_once_the_catalog_is_warm(warnings_seen: list[str]) -> None:
    source = _FakeWindows({}, catalog_warm=False)
    step = _backend("openrouter", "moonshotai/kimi-k3")
    compaction_budget(step, step, source=source)
    source.catalog_warm = True
    compaction_budget(step, step, source=source)
    compaction_budget(step, step, source=source)

    lines = _fallback_lines(warnings_seen)
    assert len(lines) == 2, lines
    assert "catalog not warm yet" in lines[0]
    assert "not in any metadata table" in lines[1]


def test_a_failing_source_falls_back_instead_of_failing_the_run(
    warnings_seen: list[str],
) -> None:
    step = _backend("acme", "wide-model")

    budget = compaction_budget(step, step, source=_BrokenSource(), cost_budget=_RAISED)
    compaction_budget(step, step, source=_BrokenSource(), cost_budget=_RAISED)

    assert budget == FALLBACK_CONTEXT_WINDOW
    lookup_lines = [line for line in warnings_seen if "lookup failed" in line]
    assert len(lookup_lines) == 1, warnings_seen


def test_an_explicit_backend_max_tokens_is_the_budget_as_before() -> None:
    """A backend that states its own window is believed over the tables and the knob,
    exactly as before R9-314 (budget = ``max_tokens``)."""
    source = _FakeWindows({"acme/wide-model": _WIDE})
    step = _backend("acme", "wide-model")
    step.max_tokens = 30  # type: ignore[attr-defined]

    assert compaction_budget(step, step, source=source) == 30
    step.max_tokens = 60_000  # type: ignore[attr-defined]
    assert compaction_budget(step, step, source=source) == 60_000


def test_a_budget_that_is_not_positive_is_refused() -> None:
    step = _backend("acme", "wide-model")
    with pytest.raises(ValueError, match="compaction budget"):
        compaction_budget(step, step, cost_budget=0)


def test_without_a_source_the_static_tables_answer() -> None:
    """Bare loops (CLI, tests) get the zero-network static tables, like the pricer."""
    from persona.backends.metadata import STATIC_MODEL_METADATA

    expected = STATIC_MODEL_METADATA["anthropic/claude-sonnet-4-6"].context_length
    step = _backend("anthropic", "claude-sonnet-4-6")

    budget = compaction_budget(step, step, cost_budget=10_000_000)

    assert budget == expected - REPLY_RESERVE_TOKENS


# ----- the loop -------------------------------------------------------------


def _resp(content: str) -> ChatResponse:
    return ChatResponse(
        content=content,
        tool_calls=[],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="m",
        provider="acme",
        latency_ms=1.0,
    )


def _reasoning_then_final(steps: int, tokens_per_step: int) -> list[ChatResponse]:
    return [*(_resp(_words(tokens_per_step)) for _ in range(steps)), _resp("[FINAL] done")]


def _loop(
    step: ScriptedBackend,
    summariser: object,
    *,
    source: _FakeWindows | None,
    budget: int | None = None,
) -> AgenticLoop:
    registry = TierRegistry(
        {
            "frontier": TierConfig(name="frontier", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
        }
    )
    registry._cache = {"frontier": step, "mid": step, "small": summariser}  # type: ignore[assignment]
    stores: dict[str, FakeStore] = {
        kind: FakeStore() for kind in ("identity", "self_facts", "worldview", "episodic")
    }
    extra = {} if budget is None else {"compaction_budget_tokens": budget}
    return AgenticLoop(
        persona=Persona(
            persona_id="astrid",
            identity=PersonaIdentity(name="Astrid", role="researcher", background="b"),
        ),
        stores=stores,  # type: ignore[arg-type]
        toolbox=Toolbox([], allow_list=None),
        skill_injector=SkillInjector(),
        scanned_skills=[],
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=registry,
        metadata_source=source,
        **extra,  # type: ignore[arg-type]
    )


async def _sizes_and_compactions(
    model: str, source: _FakeWindows, *, budget: int | None = None, steps: int = 6
) -> tuple[list[int], int]:
    script = _reasoning_then_final(steps=steps, tokens_per_step=1_500)
    step = _backend("acme", model, script)
    summariser = _backend("acme", "small-model")
    run = await _loop(step, summariser, source=source, budget=budget).run("research it")
    assert run.status is RunStatus.COMPLETED
    return [_rendered_tokens(c) for c in step.chat_contexts], summariser.chat_calls


@pytest.mark.asyncio
async def test_with_the_default_budget_a_128k_run_compacts_exactly_as_today() -> None:
    """The regression guard that the default is cost-neutral: a known 128k window and
    the default budget produce the same step contexts and the same compactions as the
    old 4,096 assumption (an unknown model)."""
    wide = _FakeWindows({"acme/wide-model": _WIDE, "acme/small-model": _WIDE})
    today = _FakeWindows({})

    known = await _sizes_and_compactions("wide-model", wide)
    unknown = await _sizes_and_compactions("mystery-model", today)

    assert known == unknown
    assert known[1] >= 1, "never compacted; this guard measures nothing"


@pytest.mark.asyncio
async def test_a_raised_budget_compacts_near_the_budget() -> None:
    source = _FakeWindows({"acme/wide-model": _WIDE, "acme/small-model": _WIDE})
    budget = 9_000

    sizes, compactions = await _sizes_and_compactions("wide-model", source, budget=budget, steps=10)
    _today_sizes, today_compactions = await _sizes_and_compactions("wide-model", source, steps=10)

    assert compactions >= 1, "never compacted; the budget was not reached"
    assert compactions < today_compactions  # the knob is what the owner trades cost for
    assert max(sizes) > _threshold(FALLBACK_CONTEXT_WINDOW) + 2_000  # grew past today
    assert max(sizes) <= _threshold(budget) + 1_600, sizes  # caught within one step


@pytest.mark.asyncio
async def test_a_raised_budget_is_bounded_by_the_window_in_a_run() -> None:
    source = _FakeWindows({"acme/narrow-model": 8_000, "acme/small-model": _WIDE})
    bound = _threshold(8_000 - REPLY_RESERVE_TOKENS)

    sizes, compactions = await _sizes_and_compactions(
        "narrow-model", source, budget=_RAISED, steps=8
    )

    assert compactions >= 1
    assert max(sizes) <= bound + 1_600, (bound, sizes)


@pytest.mark.asyncio
async def test_a_raised_budget_with_an_unknown_model_keeps_todays_behaviour() -> None:
    source = _FakeWindows({"acme/small-model": _WIDE})

    raised = await _sizes_and_compactions("mystery-model", source, budget=_RAISED)
    default = await _sizes_and_compactions("mystery-model", source)

    assert raised == default


class _RaisingRegistryCache(dict[str, object]):
    """A tier cache whose background tier blows up when read."""

    def __getitem__(self, key: str) -> object:
        if key == "small":
            raise RuntimeError("background tier exploded")
        return super().__getitem__(key)


@pytest.mark.asyncio
async def test_a_failing_background_tier_never_fails_the_run(warnings_seen: list[str]) -> None:
    """The budget's read of the background tier is best-effort. The run stays small, so
    no compaction (which needs that tier for real) is ever attempted."""
    for _ in range(2):
        script = _reasoning_then_final(steps=4, tokens_per_step=300)
        step = _backend("acme", "wide-model", script)
        loop = _loop(
            step,
            _backend("acme", "small-model"),
            source=_FakeWindows({"acme/wide-model": _WIDE}),
            budget=_RAISED,
        )
        loop._tiers._cache = _RaisingRegistryCache(loop._tiers._cache)  # type: ignore[assignment]
        # The step tier is cached under "frontier"; only the summariser lookup fails.
        run = await loop.run("research it")
        assert run.status is RunStatus.COMPLETED, run.error

    lines = [line for line in warnings_seen if "background tier" in line]
    assert len(lines) == 1, warnings_seen


@pytest.mark.asyncio
async def test_the_fallback_logs_once_across_runs(warnings_seen: list[str]) -> None:
    for _ in range(2):
        await _sizes_and_compactions("mystery-model", _FakeWindows({}), steps=4)

    assert len(_fallback_lines(warnings_seen)) == 1, warnings_seen
