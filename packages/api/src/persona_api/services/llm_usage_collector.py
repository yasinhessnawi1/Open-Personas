"""Per-op LLM usage collection for background owner-billing (Spec M3, T5).

The background surfaces (episodic consolidation, persona-voice auto-pick, the
initiative scan) each make a real ``backend.chat`` call, but the core component in
between (``Summarizer.summarize`` → ``str``, ``choose_voice`` → ``voice_id``,
``scan`` → candidates) DISCARDS the response usage before it reaches the handler
that holds the credits policy. Rather than change every core return type, we
intercept usage at the ONE common boundary — the :class:`ChatBackend` — with a
delegating wrapper that records each call's usage into a **contextvar-scoped**
sink. The handler opens a fresh sink per op (``with collect_llm_usage()``), runs
the op through the wrapped backend, then bills the summed real cost.

Contextvar scoping makes accumulation per-op even though the wrapped backend is
composed once and shared; the list is a shared mutable object, so usage recorded
inside ``asyncio.to_thread`` (the copied context sees the same list) is visible too.
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterator

    from persona.backends.protocol import ChatBackend
    from persona.backends.types import ChatResponse, StreamChunk, ToolSpec
    from persona.schema.conversation import ConversationMessage

__all__ = [
    "LLMUsageSink",
    "UsageCollectingBackend",
    "UsageTotals",
    "collect_llm_usage",
]


@dataclass(frozen=True)
class UsageTotals:
    """The summed usage of one op's model calls (the input to the billing helper)."""

    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None


@dataclass
class LLMUsageSink:
    """Accumulates each ``backend.chat`` call's usage for one op."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    provider: str = ""
    model: str = ""
    _costs: list[float | None] = field(default_factory=list)

    def record(
        self,
        *,
        provider: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float | None,
    ) -> None:
        self.prompt_tokens += max(0, prompt_tokens)
        self.completion_tokens += max(0, completion_tokens)
        self.provider = provider  # representative (last call); same-provider legs share it
        self.model = model
        self._costs.append(cost_usd)

    def totals(self) -> UsageTotals:
        """Summed usage; ``cost_usd`` is the summed actual iff EVERY call reported one."""
        cost_usd = (
            sum(c for c in self._costs if c is not None)
            if self._costs and all(c is not None for c in self._costs)
            else None
        )
        return UsageTotals(
            provider=self.provider,
            model=self.model,
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            cost_usd=cost_usd,
        )


_active_sink: ContextVar[LLMUsageSink | None] = ContextVar("llm_usage_sink", default=None)


@contextlib.contextmanager
def collect_llm_usage() -> Iterator[LLMUsageSink]:
    """Scope a fresh usage sink for one op; ``backend.chat`` calls record into it."""
    sink = LLMUsageSink()
    token = _active_sink.set(sink)
    try:
        yield sink
    finally:
        _active_sink.reset(token)


class UsageCollectingBackend:
    """A :class:`ChatBackend` decorator that records each call's usage (Spec M3, T5).

    Transparently delegates the whole ``ChatBackend`` surface; after each
    ``chat`` / ``chat_stream`` it records the response's usage into the active
    per-op sink (a no-op when none is active — the wrapper is inert outside a
    ``collect_llm_usage()`` block, so wrapping a shared backend is safe).
    """

    def __init__(self, inner: ChatBackend) -> None:
        self._inner = inner

    @property
    def provider_name(self) -> str:
        return self._inner.provider_name

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    @property
    def supports_native_tools(self) -> bool:
        return self._inner.supports_native_tools

    @property
    def supports_vision(self) -> bool:
        return self._inner.supports_vision

    def _record(self, usage: object) -> None:
        sink = _active_sink.get()
        if sink is None or usage is None:
            return
        sink.record(
            provider=self._inner.provider_name,
            model=self._inner.model_name,
            prompt_tokens=getattr(usage, "prompt_tokens", 0),
            completion_tokens=getattr(usage, "completion_tokens", 0),
            cost_usd=getattr(usage, "cost_usd", None),
        )

    async def chat(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: list[str] | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> ChatResponse:
        response = await self._inner.chat(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            top_p=top_p,
            top_k=top_k,
        )
        self._record(getattr(response, "usage", None))
        return response

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
    ) -> AsyncIterator[StreamChunk]:
        async for chunk in self._inner.chat_stream(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            top_p=top_p,
            top_k=top_k,
        ):
            if chunk.usage is not None:
                self._record(chunk.usage)
            yield chunk
