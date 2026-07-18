"""Episodic-consolidation owner-billing (Spec M3, T5a).

The K8 summarizer's real cost is captured (via the ``UsageCollectingBackend`` the
root wraps + the handler's ``collect_llm_usage`` block) and billed to the persona
OWNER, idempotently keyed ``episodic_consolidation:{persona_id}:{watermark_bucket}``
so a re-fire does not double-charge (the ON CONFLICT gate is proven at the DB level
in ``test_credits_idempotent_billing``; here we prove the stable key + real price +
the no-LLM-call → no-charge path).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona_api.jobs.handlers.episodic_consolidation import (
    EpisodicConsolidationHandler,
    EpisodicConsolidationJobPayload,
)
from persona_api.services.llm_usage_collector import UsageCollectingBackend

_ENGINE: Any = object()


class _FakeBackend:
    provider_name = "openrouter"
    model_name = "m"
    supports_native_tools = False
    supports_vision = False

    async def chat(self, messages: object, **_kw: object) -> ChatResponse:  # noqa: ARG002
        return ChatResponse(
            content="summary",
            usage=TokenUsage(
                prompt_tokens=100, completion_tokens=1000, total_tokens=1100, cost_usd=0.03
            ),
            model="m",
            provider="openrouter",
            latency_ms=1.0,
        )


class _SummarizingEngine:
    """A fake engine that makes a real (wrapped) model call, like the K8 engine."""

    def __init__(self, backend: object, *, calls: int = 1) -> None:
        self._backend = backend
        self._calls = calls

    async def run(self, owner_id: str, persona_id: str) -> object:  # noqa: ARG002
        for _ in range(self._calls):
            await self._backend.chat([])  # records usage into the active sink
        return SimpleNamespace(windows_formed=1, gists_written=1, candidates_emitted=0, skipped=[])


class _NoLLMEngine:
    async def run(self, owner_id: str, persona_id: str) -> object:  # noqa: ARG002
        return SimpleNamespace(windows_formed=0, gists_written=0, candidates_emitted=0, skipped=[])


class _RecordingPolicy:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def capture_up_to_idempotent(self, **kw: object) -> tuple[int, int]:
        self.calls.append(kw)
        return int(kw["amount"]), 0  # type: ignore[call-overload]


def _handler(engine: object, policy: _RecordingPolicy | None) -> EpisodicConsolidationHandler:
    return EpisodicConsolidationHandler(
        engine=engine,  # type: ignore[arg-type]
        credits_policy=policy,  # type: ignore[arg-type]
        rls_engine=_ENGINE if policy is not None else None,
        cost_source=None,
    )


def _ctx() -> object:
    return SimpleNamespace(owner_id="u1", job_id="j1", meter=lambda **_k: None)


def _payload() -> EpisodicConsolidationJobPayload:
    return EpisodicConsolidationJobPayload(persona_id="p1", watermark_bucket="b1")


@pytest.mark.asyncio
async def test_summarizer_cost_is_owner_billed_with_the_stable_key() -> None:
    policy = _RecordingPolicy()
    backend = UsageCollectingBackend(_FakeBackend())  # type: ignore[arg-type]
    handler = _handler(_SummarizingEngine(backend), policy)
    await handler.handle(_payload(), _ctx())  # type: ignore[arg-type]
    assert len(policy.calls) == 1
    kw = policy.calls[0]
    assert kw["amount"] == 3  # ceil(3.0¢) actual
    assert kw["reason"] == "episodic_consolidation:actual_openrouter"
    assert kw["cost_basis"] == "actual_openrouter"
    assert kw["billing_key"] == "episodic_consolidation:p1:b1"
    assert kw["user_id"] == "u1"


@pytest.mark.asyncio
async def test_refire_uses_the_same_billing_key() -> None:
    # A re-fire re-runs the engine + re-accumulates the same cost, but the stable
    # billing_key means the DB ON CONFLICT gate charges once (proven at the DB level).
    policy = _RecordingPolicy()
    backend = UsageCollectingBackend(_FakeBackend())  # type: ignore[arg-type]
    handler = _handler(_SummarizingEngine(backend), policy)
    await handler.handle(_payload(), _ctx())  # type: ignore[arg-type]
    await handler.handle(_payload(), _ctx())  # type: ignore[arg-type]
    keys = {c["billing_key"] for c in policy.calls}
    assert keys == {"episodic_consolidation:p1:b1"}  # identical key both fires


@pytest.mark.asyncio
async def test_multiple_summarizer_calls_sum_the_cost() -> None:
    policy = _RecordingPolicy()
    backend = UsageCollectingBackend(_FakeBackend())  # type: ignore[arg-type]
    handler = _handler(_SummarizingEngine(backend, calls=3), policy)  # 3 windows
    await handler.handle(_payload(), _ctx())  # type: ignore[arg-type]
    assert policy.calls[0]["amount"] == 9  # 3 × 3.0¢ = 9.0¢ → 9 credits


@pytest.mark.asyncio
async def test_no_llm_call_charges_nothing() -> None:
    policy = _RecordingPolicy()
    handler = _handler(_NoLLMEngine(), policy)
    await handler.handle(_payload(), _ctx())  # type: ignore[arg-type]
    assert policy.calls == []


@pytest.mark.asyncio
async def test_unwired_billing_is_a_noop() -> None:
    backend = UsageCollectingBackend(_FakeBackend())  # type: ignore[arg-type]
    handler = _handler(_SummarizingEngine(backend), None)  # no policy
    await handler.handle(_payload(), _ctx())  # type: ignore[arg-type]  # must not raise
