"""The judge reads its chain through the background wrappers (fifth review LOW-1, sixth LOW-4).

The initiative judge runs on ``UsageCollectingBackend(PlanScopedChatBackend(...))``.
Its head-of-chain rule needs the wrapped ``MultiModelChatBackend`` (its primary and its
attempt ledger). Both wrappers offer ``resolve_chain``, which reads the owner's plan ONCE
and returns that chain; without it every answer would read as the head's, and each
property read through the plan wrapper would be another plan read.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import ChatResponse, StreamChunk, TokenUsage
from persona.initiative import (
    CandidateSource,
    CitationKind,
    GroundingCitation,
    InitiativeCandidate,
    InitiativeTrigger,
    PlannedStep,
    Urgency,
)
from persona.schema.conversation import ConversationMessage
from persona.tools.categories import ActionCategory
from persona_api.services import model_tiers
from persona_api.services.llm_usage_collector import UsageCollectingBackend, collect_llm_usage
from persona_api.services.model_tiers import PlanScopedChatBackend
from persona_runtime.initiative.grounding import GroundingChecker, excerpts_fingerprint

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_EXCERPT = "The user's car insurance renews automatically on 4 October 2026."
_YES = '{"entailed": true, "quote": "car insurance renews automatically"}'


class _Member:
    """A chain member that answers ``content`` (an empty answer makes the chain fall back)."""

    supports_native_tools = False
    supports_vision = False

    def __init__(self, provider: str, model: str, content: str) -> None:
        self.provider_name, self.model_name, self.content = provider, model, content
        self.calls = 0

    async def chat(self, messages: Any, **_: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        self.calls += 1
        return ChatResponse(
            content=self.content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )

    async def chat_stream(self, messages: Any, **_: Any) -> AsyncIterator[StreamChunk]:  # noqa: ANN401, ARG002
        self.calls += 1
        if self.content:
            yield StreamChunk(delta=self.content)
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


class _Registry:
    def __init__(self, chain: MultiModelChatBackend) -> None:
        self._chain = chain

    def get(self, _tier: str) -> MultiModelChatBackend:
        return self._chain


class _Source:
    def node_content(self, owner_id: str, node_id: str) -> str | None:  # noqa: ARG002
        return _EXCERPT

    def conversation_content(self, owner_id: str, conversation_id: str) -> str | None:  # noqa: ARG002
        return None

    def task_content(self, owner_id: str, task_id: str) -> str | None:  # noqa: ARG002
        return None

    def node_context(self, owner_id: str, node_id: str, *, limit: int) -> list[Any]:  # noqa: ARG002
        return []


def _wrapped(
    chain: MultiModelChatBackend, monkeypatch: pytest.MonkeyPatch
) -> tuple[UsageCollectingBackend, list[int]]:
    """The production composition over ``chain``; the list counts plan reads."""
    reads: list[int] = []

    def _select(**_: Any) -> _Registry:  # noqa: ANN401
        reads.append(1)
        return _Registry(chain)

    monkeypatch.setattr(model_tiers, "select_plan_tier_registry", _select)
    plan_scoped = PlanScopedChatBackend(
        tier="small",
        rls_engine=None,  # type: ignore[arg-type]  # never read: the plan read is patched
        paid_tier_registry=None,  # type: ignore[arg-type]
        free_tier_registry=None,
    )
    return UsageCollectingBackend(plan_scoped), reads


def _candidate() -> InitiativeCandidate:
    return InitiativeCandidate(
        observation="The car insurance renews automatically.",
        citations=(GroundingCitation(kind=CitationKind.NODE, ref="node-1"),),
        trigger=InitiativeTrigger.APPROACHING_COMMITMENT,
        why_now="Your car insurance renews automatically.",
        plan=(PlannedStep(description="compare", categories=frozenset({ActionCategory.DRAFT})),),
        next_step="compare offers for you",
        value=0.8,
        acceptance=0.8,
        urgency=Urgency.BATCH,
        source=CandidateSource.SCAN,
        owner_id="u1",
        persona_id="p1",
        prompt_version="a5-scan-v12",
        scanned_at=datetime(2026, 9, 26, 5, 0, tzinfo=UTC),
    )


_MESSAGES = [ConversationMessage(role="user", content="hi", created_at=datetime.now(UTC))]


@pytest.mark.asyncio
async def test_a_fallback_is_visible_through_both_wrappers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = _Member("openrouter", "openai/gpt-oss-120b", "")  # empty: falls through
    spare = _Member("groq", "openai/gpt-oss-120b", "an answer")
    chain = MultiModelChatBackend([head, spare], max_retries_per_backend=0)  # type: ignore[list-item]
    wrapper, _ = _wrapped(chain, monkeypatch)
    response = await wrapper.chat(_MESSAGES)
    assert wrapper.resolve_chain() is chain
    # R9-223: the served identity rides the response through both wrappers.
    assert response.served is not None
    assert (response.served.provider, response.served.model) == ("groq", "openai/gpt-oss-120b")


@pytest.mark.asyncio
async def test_the_usage_sink_records_the_model_that_served_each_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """R9-296: the collector recorded the chain's PRIMARY, so every background op whose
    call a fallback served was billed at the primary's rate."""
    head = _Member("openrouter", "openai/gpt-oss-120b", "")  # empty: falls through
    spare = _Member("groq", "openai/gpt-oss-120b", "an answer")
    chain = MultiModelChatBackend([head, spare], max_retries_per_backend=0)  # type: ignore[list-item]
    wrapper, _ = _wrapped(chain, monkeypatch)
    with collect_llm_usage() as sink:
        await wrapper.chat(_MESSAGES)
    totals = sink.totals()
    assert (totals.provider, totals.model) == ("groq", "openai/gpt-oss-120b")
    assert (totals.prompt_tokens, totals.completion_tokens) == (1, 1)  # the answer only


@pytest.mark.asyncio
async def test_the_usage_sink_carries_a_failed_try_that_cost_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """D-ORCH-11-18-s: the head's empty completion was billed (1 in, 1 out); the sink keeps
    it, at the head's own model, for the background op to be charged for it."""
    head = _Member("openrouter", "openai/gpt-oss-120b", "")
    spare = _Member("groq", "openai/gpt-oss-120b", "an answer")
    chain = MultiModelChatBackend([head, spare], max_retries_per_backend=0)  # type: ignore[list-item]
    wrapper, _ = _wrapped(chain, monkeypatch)
    with collect_llm_usage() as sink:
        await wrapper.chat(_MESSAGES)
    (wasted,) = sink.totals().failed_tries
    assert (wasted.provider, wasted.model, wasted.error_class) == (
        "openrouter",
        "openai/gpt-oss-120b",
        "EmptyCompletionError",
    )
    assert wasted.usage is not None
    assert (wasted.usage.prompt_tokens, wasted.usage.completion_tokens) == (1, 1)


@pytest.mark.asyncio
async def test_the_usage_sink_records_the_served_model_of_a_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    head = _Member("openrouter", "openai/gpt-oss-120b", "")
    spare = _Member("groq", "openai/gpt-oss-120b", "an answer")
    chain = MultiModelChatBackend([head, spare], max_retries_per_backend=0)  # type: ignore[list-item]
    wrapper, _ = _wrapped(chain, monkeypatch)
    with collect_llm_usage() as sink:
        _ = [chunk async for chunk in wrapper.chat_stream(_MESSAGES)]
    assert (sink.provider, sink.model) == ("groq", "openai/gpt-oss-120b")


def test_a_bare_backend_is_its_own_chain() -> None:
    bare = _Member("anthropic", "claude-haiku", "x")
    assert UsageCollectingBackend(bare).resolve_chain() is bare  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_a_judge_call_reads_the_plan_twice_and_a_keep_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sixth review LOW-4: one plan read for the call itself and one for the served
    identity; a keep reads it once for the head and makes no call."""
    head = _Member("openrouter", "openai/gpt-oss-120b", _YES)
    chain = MultiModelChatBackend([head], max_retries_per_backend=0)  # type: ignore[list-item]
    wrapper, reads = _wrapped(chain, monkeypatch)
    checker = GroundingChecker(source=_Source(), backend=wrapper)
    admitted = await checker.check(_candidate())
    assert admitted.admitted is True
    assert admitted.served_model == "openrouter/openai/gpt-oss-120b"
    assert len(reads) == 2
    reads.clear()
    kept = await checker.check(
        _candidate(),
        admitted_fingerprint=excerpts_fingerprint([_EXCERPT]),
        admitted_served_model=admitted.served_model,
    )
    assert (kept.kept, head.calls, len(reads)) == (True, 1, 1)
