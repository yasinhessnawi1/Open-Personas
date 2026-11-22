"""The agentic loop compacts against the served model's real window (R9-314).

No production backend exposes ``max_tokens``, so the loop used to assume a 4,096 token
window for every run and compact once the context passed about 3,300 tokens. On a
tool-heavy run that is a compaction on most steps, and since R9-299 each one is a
metered, charged model call. These tests pin the replacement: the window comes from
the model metadata the runtime already prices with, the threshold is sized from it,
and the old 4,096 survives only as the last resort, logged once.

Nothing here reaches a provider: the backends are scripted and the metadata source is
an in-memory fake.
"""

# ruff: noqa: SLF001 - tests reset the once-only log memory and read the loop's source.

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


class _FakeWindows:
    """An in-memory metadata source: model id to context window, nothing else."""

    def __init__(self, windows: dict[str, int]) -> None:
        self._windows = windows
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
    """The fallback logs once per process; each test starts from a clean memory."""
    context_window._warned_unknown.clear()
    context_window._warned_failed.clear()
    yield
    context_window._warned_unknown.clear()
    context_window._warned_failed.clear()


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


# ----- the budget itself ----------------------------------------------------


def test_a_known_window_sizes_the_budget_and_3300_tokens_no_longer_compacts() -> None:
    source = _FakeWindows({"acme/wide-model": _WIDE})
    step = _backend("acme", "wide-model")

    budget = compaction_budget(step, None, source=source)

    assert budget == _WIDE - REPLY_RESERVE_TOKENS
    compactor = StepHistoryCompactor()
    context = _context_of(3_400)
    assert _rendered_tokens(context) > _threshold(FALLBACK_CONTEXT_WINDOW)  # the old trigger
    assert compactor.should_compact(context, budget) is False
    # The lookup is the pricer's: the canonical id, and never a network fetch.
    assert source.lookups == [("acme/wide-model", False)]


def test_a_chain_is_sized_by_its_smallest_member() -> None:
    """The next step can land on any member of the chain, so the context must fit them all."""
    source = _FakeWindows({"acme/wide-model": _WIDE, "acme/narrow-model": _NARROW})
    chain = MultiModelChatBackend(
        [_backend("acme", "wide-model"), _backend("acme", "narrow-model")],  # type: ignore[list-item]
        max_retries_per_backend=0,
    )

    assert compaction_budget(chain, None, source=source) == _NARROW - REPLY_RESERVE_TOKENS


def test_the_summariser_window_caps_the_budget() -> None:
    """A compaction hands the middle of the context to the background tier, so a context
    that outgrew the summariser's window could not be compacted at all."""
    source = _FakeWindows({"acme/wide-model": _WIDE, "acme/small-model": _NARROW})

    budget = compaction_budget(
        _backend("acme", "wide-model"), _backend("acme", "small-model"), source=source
    )

    assert budget == _NARROW - REPLY_RESERVE_TOKENS


def test_an_unknown_member_does_not_hide_the_known_ones() -> None:
    source = _FakeWindows({"acme/wide-model": _WIDE})
    chain = MultiModelChatBackend(
        [_backend("acme", "wide-model"), _backend("acme", "mystery-model")],  # type: ignore[list-item]
        max_retries_per_backend=0,
    )

    assert compaction_budget(chain, None, source=source) == _WIDE - REPLY_RESERVE_TOKENS


def test_an_unknown_window_falls_back_to_4096_and_logs_once(warnings_seen: list[str]) -> None:
    source = _FakeWindows({})
    step = _backend("acme", "mystery-model")

    first = compaction_budget(step, None, source=source)
    second = compaction_budget(step, None, source=source)

    assert first == second == FALLBACK_CONTEXT_WINDOW == 4096
    fallback_lines = [line for line in warnings_seen if "context window" in line]
    assert len(fallback_lines) == 1, warnings_seen
    assert "mystery-model" in fallback_lines[0]


def test_a_failing_source_falls_back_instead_of_failing_the_run(
    warnings_seen: list[str],
) -> None:
    budget = compaction_budget(_backend("acme", "wide-model"), None, source=_BrokenSource())

    assert budget == FALLBACK_CONTEXT_WINDOW
    assert any("context window" in line for line in warnings_seen), warnings_seen


def test_an_explicit_backend_max_tokens_still_wins() -> None:
    """A backend that states its own window is believed over the tables, unchanged."""
    source = _FakeWindows({"acme/wide-model": _WIDE})
    step = _backend("acme", "wide-model")
    step.max_tokens = 30  # type: ignore[attr-defined]

    assert compaction_budget(step, None, source=source) == 30


def test_without_a_source_the_static_tables_answer() -> None:
    """Bare loops (CLI, tests) get the zero-network static tables, like the pricer."""
    from persona.backends.metadata import STATIC_MODEL_METADATA

    expected = STATIC_MODEL_METADATA["anthropic/claude-sonnet-4-6"].context_length
    budget = compaction_budget(_backend("anthropic", "claude-sonnet-4-6"), None)

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
    summariser: ScriptedBackend,
    *,
    source: _FakeWindows | None,
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
    )


@pytest.mark.asyncio
async def test_a_run_on_a_128k_model_does_not_compact_past_3300_tokens() -> None:
    script = _reasoning_then_final(steps=4, tokens_per_step=1_500)
    step = _backend("acme", "wide-model", script)
    summariser = _backend("acme", "small-model")
    source = _FakeWindows({"acme/wide-model": _WIDE, "acme/small-model": _WIDE})

    run = await _loop(step, summariser, source=source).run("research it")

    assert run.status is RunStatus.COMPLETED
    biggest = max(_rendered_tokens(c) for c in step.chat_contexts)
    assert biggest > _threshold(FALLBACK_CONTEXT_WINDOW), "never passed 3,300; measures nothing"
    assert summariser.chat_calls == 0  # no compaction, so no charge for one


@pytest.mark.asyncio
async def test_the_same_run_on_an_unknown_model_keeps_the_old_behaviour() -> None:
    """The control for the test above: the last resort is today's 4,096 window."""
    script = _reasoning_then_final(steps=4, tokens_per_step=1_500)
    step = _backend("acme", "mystery-model", script)
    summariser = _backend("acme", "small-model")

    run = await _loop(step, summariser, source=_FakeWindows({})).run("research it")

    assert run.status is RunStatus.COMPLETED
    assert summariser.chat_calls >= 1


@pytest.mark.asyncio
async def test_a_run_compacts_near_the_sized_threshold() -> None:
    script = _reasoning_then_final(steps=8, tokens_per_step=2_500)
    step = _backend("acme", "narrow-model", script)
    summariser = _backend("acme", "small-model")
    source = _FakeWindows({"acme/narrow-model": _NARROW, "acme/small-model": _WIDE})
    threshold = _threshold(_NARROW - REPLY_RESERVE_TOKENS)

    run = await _loop(step, summariser, source=source).run("research it")

    assert run.status is RunStatus.COMPLETED
    assert summariser.chat_calls >= 1, "never compacted; the threshold was not reached"
    sizes = [_rendered_tokens(c) for c in step.chat_contexts]
    # Compaction let the context grow well past the old 3,300 trigger...
    assert max(sizes) > 3 * _threshold(FALLBACK_CONTEXT_WINDOW)
    # ...and caught it within one step of the sized threshold.
    assert max(sizes) <= threshold + 2_600, (threshold, sizes)


@pytest.mark.asyncio
async def test_the_fallback_logs_once_across_runs(warnings_seen: list[str]) -> None:
    for _ in range(2):
        script = _reasoning_then_final(steps=4, tokens_per_step=1_500)
        step = _backend("acme", "mystery-model", script)
        await _loop(step, _backend("acme", "mystery-model"), source=_FakeWindows({})).run("go")

    fallback_lines = [line for line in warnings_seen if "context window" in line]
    assert len(fallback_lines) == 1, warnings_seen
