"""M2 review, finding I2 — multi-round usage/cost accumulation through the REAL loop.

Drives real :class:`ConversationLoop` turns (scripted backends, no network) whose
tool sub-loop runs MULTIPLE rounds — each a separately-billed request on
OpenRouter. Pre-fix, ``turn()`` kept only the LAST round's :class:`TokenUsage`
(``usage = round_usage``, last-writer-wins, never summed): a multi-round
agentic turn recorded/billed only its final round's tokens and actual,
understating both by up to N-1 rounds' worth. This file pins the fix's three
arms end to end, through the REAL tool sub-loop (no hand-forced state):

* tokens always SUM across every round (the general pin, >=3 rounds, no
  actual anywhere — also the "no-actuals arm unchanged" pin: the estimate
  path is unchanged mechanics, just fed the summed tokens);
* the ALL-ACTUALS arm: every round reports a ``cost_usd`` -> the turn's
  actual is their SUM, basis stays ``actual_openrouter``;
* the MIXED arm: round 1 serves cleanly, round 2 genuinely falls back
  mid-turn (a REAL :class:`MultiModelChatBackend` fallback, not a scripted
  stand-in) to a model that reports no cost -> the partial actual is
  DROPPED (never summed as if the missing round were free) and logged once;
* single-round turns are byte-identical (the pre-fix shape, unpinned by any
  ``usage=`` override in this file).

``@pytest.mark.integration`` — excluded from the default run (mirrors the
sibling M2 integration files).
"""

# ruff: noqa: SLF001, ARG001, ARG002 — registry._cache wiring, the unused-by-design
# ``prompt`` on the scripted image tool, and the ChatBackend Protocol's full kwarg
# surface on a scripted double all mirror the sibling M2 integration test files.
from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from _fakes import FakeStore, ScriptedBackend, ScriptedRound  # type: ignore[import-not-found]
from loguru import logger as _loguru_logger
from persona.backends import BackendConfig
from persona.backends.errors import RateLimitError
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import StreamChunk, TokenUsage, ToolCallDelta
from persona.history import ConversationHistoryManager
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolResult
from persona.skills import SkillInjector, SkillScanner
from persona.tools import Toolbox
from persona.tools.protocol import tool
from persona_runtime.logging import MemoryTurnLogWriter
from persona_runtime.loop import ConversationLoop
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from persona.backends import ChatBackend
    from persona.backends.types import ToolSpec
    from persona.schema.conversation import ConversationMessage

pytestmark = pytest.mark.integration

_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)  # type: ignore[arg-type]


@tool(name="echo", description="Echo a message back.")
async def _echo_tool(message: str) -> ToolResult:
    return ToolResult(tool_name="echo", content=f"echoed: {message}", is_error=False)


@tool(name="generate_image", description="YOU CAN generate images. Use this tool.")
async def _generate_image_tool(prompt: str) -> ToolResult:
    return ToolResult(tool_name="generate_image", content="ok", is_error=False)


# Reused verbatim from the proven ``test_refusal_auto_retry.py`` pattern — the
# all-actuals arm below drives its 2 rounds via the refusal-retry mechanism
# (round 0 + the corrective re-generation) rather than tool-calling, so it
# never touches ``format_tool_result``. That formatter has no ``"openrouter"``
# case, a separate pre-existing gap outside this fix's scope; no test in this
# suite had ever dispatched a REAL tool call on an OpenRouter-flavoured
# backend before. Refusal-retry rounds are plain text on both sides, so an
# ``openrouter``-provider turn is safe here.
_REFUSAL = "I'm sorry, but I can't generate images — I'm only a text-based assistant."
_CORRECTED = "Sure — generating that image now."


class _DegradingPrimary:
    """Serves round 0 cleanly (a tool call, non-OpenRouter usage — no
    ``cost_usd``, matching the convention that only OpenRouter populates it)
    then genuinely fails on every subsequent round — the T2
    ``_FailingBackend`` shape (see ``test_m2_openrouter_actuals.py``), but
    call-count-gated instead of unconditional, so it composes into a
    mid-turn fallback through a REAL :class:`MultiModelChatBackend`.
    """

    def __init__(self) -> None:
        self.stream_calls = 0

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-sonnet-4-6"

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: list[ConversationMessage], **kwargs: object) -> object:
        raise NotImplementedError  # the loop streams

    async def chat_stream(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: list[str] | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
        **kwargs: object,
    ) -> AsyncIterator[StreamChunk]:
        self.stream_calls += 1
        if self.stream_calls == 1:
            yield StreamChunk(
                delta="",
                tool_call_delta=ToolCallDelta(
                    call_id="c0", name_delta="echo", arguments_delta='{"message": "1"}'
                ),
            )
            yield StreamChunk(
                delta="",
                is_final=True,
                usage=TokenUsage(prompt_tokens=60, completion_tokens=15, total_tokens=75),
            )
            return
        raise RateLimitError(
            "scripted mid-turn degradation",
            context={"provider": "anthropic", "status_code": "429"},
        )
        yield  # pragma: no cover — makes this an async generator


def _openrouter_secondary(*, cost_usd: float) -> ScriptedBackend:
    return ScriptedBackend(
        [
            ScriptedRound(
                text="Wrapped up on a different model.",
                usage=TokenUsage(
                    prompt_tokens=45, completion_tokens=12, total_tokens=57, cost_usd=cost_usd
                ),
            )
        ],
        provider_name="openrouter",
        model_name="z-ai/glm-4.6",
    )


@pytest.fixture
def loguru_capture() -> Iterator[list[str]]:
    """Loguru sink capturing every emitted message string (>= WARNING).

    The project's logging surface (``persona.logging.get_logger``) wraps
    loguru, so pytest's stdlib-only ``caplog`` does not see records — mirrors
    the pattern in ``test_tier_registry_multimodel.py`` /
    ``test_chat_turn_worker_proportional.py``.
    """
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


def _persona() -> Persona:
    return Persona(
        persona_id="astrid",
        identity=PersonaIdentity(name="A", role="r", background="b", constraints=["c"]),
    )


def _conv() -> Conversation:
    return Conversation(conversation_id="c1", persona_id="astrid", messages=[])


def _make_loop(
    backend: ChatBackend, *, max_tool_rounds: int = 5
) -> tuple[ConversationLoop, MemoryTurnLogWriter]:
    toolbox = Toolbox([_echo_tool, _generate_image_tool], allow_list=None)  # type: ignore[arg-type]
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
        max_tool_rounds=max_tool_rounds,
    )
    return loop, writer


@pytest.mark.asyncio
async def test_tokens_sum_across_at_least_three_rounds() -> None:
    """General pin: >=3 rounds via the REAL tool sub-loop, distinct per-round
    token counts, no ``cost_usd`` anywhere (also the "no-actuals arm
    unchanged" pin — today's estimate semantics, now fed the summed tokens).

    The per-round counts are deliberately DISTINCT so a regression back to
    last-writer-wins (or an "N x last round" miscount) fails loudly instead
    of silently matching by coincidence.
    """
    rounds = [
        ScriptedRound(
            tool_name="echo",
            tool_args={"message": "1"},
            call_id="c0",
            usage=TokenUsage(prompt_tokens=100, completion_tokens=40, total_tokens=140),
        ),
        ScriptedRound(
            tool_name="echo",
            tool_args={"message": "2"},
            call_id="c1",
            usage=TokenUsage(prompt_tokens=220, completion_tokens=55, total_tokens=275),
        ),
        ScriptedRound(
            tool_name="echo",
            tool_args={"message": "3"},
            call_id="c2",
            usage=TokenUsage(prompt_tokens=310, completion_tokens=65, total_tokens=375),
        ),
        ScriptedRound(
            text="Here's what I found.",
            usage=TokenUsage(prompt_tokens=400, completion_tokens=30, total_tokens=430),
        ),
    ]
    backend = ScriptedBackend(rounds, provider_name="anthropic", model_name="claude-sonnet-4-6")
    loop, writer = _make_loop(backend, max_tool_rounds=5)

    chunks = [c async for c in loop.turn(_conv(), "use the echo tool three times")]

    assert backend.chat_stream_calls == 4  # 3 tool rounds + 1 final round; the cap never bites
    log = writer.logs[-1]
    assert log.tool_calls == 3
    assert log.prompt_tokens == 1030  # 100 + 220 + 310 + 400 — NOT 400 (last round alone)
    assert log.completion_tokens == 190  # 40 + 55 + 65 + 30 — NOT 30
    # No round reported an actual -> the honest estimate path, now over the
    # SUMMED tokens (anthropic/claude-sonnet-4-6 is in the static table since
    # M2-T1's F4 parity rows, so this is a real priced estimate, not unpriced).
    assert log.cost_basis == "estimate_static"
    # 1.030 * $0.30/1k (input) + 0.190 * $1.50/1k (output) == 0.594 cents.
    # The pre-fix bug would have priced only the last round: 0.400*0.30 +
    # 0.030*1.50 == 0.165 cents — a materially different (wrong) number.
    assert log.cost_cents == pytest.approx(0.594)
    # The final SSE chunk's usage carries the SAME summed totals — it feeds
    # off the identical aggregated ``usage`` the TurnLog row does.
    assert chunks[-1].is_final is True
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.prompt_tokens == 1030
    assert chunks[-1].usage.completion_tokens == 190


@pytest.mark.asyncio
async def test_all_actuals_sum_to_the_turns_actual(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every round carries an OpenRouter actual -> the turn's actual is their
    SUM, basis stays ``actual_openrouter`` (never a per-round overwrite).

    Drives its 2 rounds via the refusal-retry mechanism (D-25-T21) rather
    than tool-calling — see the ``_REFUSAL``/``_CORRECTED`` module comment
    for why (an unrelated pre-existing gap, out of this fix's scope). This
    is still the REAL trigger chain: ``PERSONA_REFUSAL_RETRY_ENABLED``
    armed, the model's own text genuinely refuses an available tool, and
    the loop's own refusal-retry logic re-generates — nothing hand-forced.
    """
    monkeypatch.setenv("PERSONA_REFUSAL_RETRY_ENABLED", "true")
    rounds = [
        ScriptedRound(
            text=_REFUSAL,
            usage=TokenUsage(
                prompt_tokens=50, completion_tokens=20, total_tokens=70, cost_usd=0.0001
            ),
        ),
        ScriptedRound(
            text=_CORRECTED,
            usage=TokenUsage(
                prompt_tokens=80, completion_tokens=25, total_tokens=105, cost_usd=0.0002
            ),
        ),
    ]
    backend = ScriptedBackend(rounds, provider_name="openrouter", model_name="z-ai/glm-4.6")
    loop, writer = _make_loop(backend)

    chunks = [c async for c in loop.turn(_conv(), "make me an image")]

    assert backend.chat_stream_calls == 2
    text = "".join(c.delta for c in chunks)
    assert _CORRECTED in text
    log = writer.logs[-1]
    assert log.refusal_retry_engaged is True
    assert log.tool_calls == 0
    assert log.prompt_tokens == 130
    assert log.completion_tokens == 45
    assert log.cost_basis == "actual_openrouter"
    # (0.0001 + 0.0002) USD * 100 == 0.03 cents.
    assert log.cost_cents == pytest.approx(0.03)


@pytest.mark.asyncio
async def test_mixed_round_actuals_are_dropped_and_logged_once(
    loguru_capture: list[str],
) -> None:
    """Genuine mid-turn fallback via a REAL :class:`MultiModelChatBackend`:
    round 0 serves cleanly on a non-OpenRouter primary (usage, no
    ``cost_usd``); the primary genuinely degrades on round 1's re-prompt and
    the wrapper falls back — for real, no hand-forced state — to an
    OpenRouter secondary that DOES report a ``cost_usd``.

    The served (last-round) pair is OpenRouter, so the M1 review's
    actual-arm provider gate alone would NOT have blocked an actual here —
    only the mixed-round honesty rule does: the partial actual is dropped,
    never summed as if the missing round were free.
    """
    primary = _DegradingPrimary()
    secondary = _openrouter_secondary(cost_usd=0.0004)
    wrapper = MultiModelChatBackend(
        [primary, secondary],  # type: ignore[list-item]
        tier_name="mid",
        max_retries_per_backend=0,
    )
    loop, writer = _make_loop(wrapper)  # type: ignore[arg-type]

    _ = [c async for c in loop.turn(_conv(), "use the echo tool then answer")]

    assert primary.stream_calls == 2  # served round 0 for real, genuinely failed round 1
    log = writer.logs[-1]
    assert log.tool_calls == 1
    assert log.fallback_engaged is True
    # Attribution stays last-round-served (unchanged scope, D-M2-2): the
    # SECONDARY served the final round, so it names the turn.
    assert log.model_name == "z-ai/glm-4.6"
    assert log.provider == "openrouter"
    # Tokens ALWAYS sum across both rounds regardless of the cost outcome.
    assert log.prompt_tokens == 60 + 45
    assert log.completion_tokens == 15 + 12
    # The partial actual (secondary's 0.0004) is DROPPED, not summed/kept as
    # if the missing round were free — basis falls to the honest estimate
    # path. z-ai/glm-4.6 is a catalog-only slug the bare loop's static-only
    # chain cannot resolve (mirrors the sibling "absent actual degrades
    # honestly" test), so this lands deterministically on unpriced rather
    # than a hand-computed static estimate.
    assert log.cost_basis == "unpriced"
    assert log.cost_cents == 0.0

    warnings = [m for m in loguru_capture if "mixed cost_usd report" in m]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_single_round_turn_is_byte_identical() -> None:
    """N=1 round: the aggregator must reproduce the pre-fix single-round shape
    exactly — the default ``ScriptedBackend`` usage, untouched by any
    per-round ``usage=`` override (every other test in this file overrides
    it)."""
    backend = ScriptedBackend([ScriptedRound(text="Hello, I'm Astrid.")])
    loop, writer = _make_loop(backend)

    chunks = [c async for c in loop.turn(_conv(), "hi")]

    assert backend.chat_stream_calls == 1
    log = writer.logs[-1]
    assert log.tool_calls == 0
    # The default ScriptedBackend usage shape (TokenUsage(10, 5, 15)), untouched.
    assert log.prompt_tokens == 10
    assert log.completion_tokens == 5
    assert log.cost_basis == "estimate_static"
    assert log.cost_cents == pytest.approx(0.0105)  # 0.010*0.30 + 0.005*1.50
    assert chunks[-1].usage is not None
    assert chunks[-1].usage.prompt_tokens == 10
    assert chunks[-1].usage.completion_tokens == 5
