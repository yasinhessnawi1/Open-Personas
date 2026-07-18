"""Initiative-scan owner-billing (Spec M3, T5b).

The A5 scan's real model cost is captured (via the ``UsageCollectingBackend`` the
worker root wraps + the handler's ``collect_llm_usage`` block) and billed to the
persona OWNER, idempotently keyed
``initiative_scan:{persona_id}:{fire_time.isoformat()}`` — the SAME key the A0
tenant declares — so a re-fire does not double-charge (the ON CONFLICT gate is
proven at the DB level in ``test_credits_idempotent_billing``; here we prove the
stable key + real price + the dial-off / no-LLM → no-charge paths).
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.initiative import DEFAULT_INITIATIVE_DIAL, InitiativeDial
from persona_api.initiative.handler import InitiativeScanHandler, InitiativeScanPayload
from persona_api.services.llm_usage_collector import UsageCollectingBackend

_ENGINE: Any = object()
_FIRE = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)


class _FakeBackend:
    provider_name = "openrouter"
    model_name = "m"
    supports_native_tools = False
    supports_vision = False

    async def chat(self, messages: object, **_kw: object) -> ChatResponse:  # noqa: ARG002
        return ChatResponse(
            content="scan",
            usage=TokenUsage(
                prompt_tokens=100, completion_tokens=1000, total_tokens=1100, cost_usd=0.03
            ),
            model="m",
            provider="openrouter",
            latency_ms=1.0,
        )


class _ScanningScanner:
    """A fake scanner that makes a real (wrapped) model call, like the runtime scanner."""

    def __init__(self, backend: object, *, calls: int = 1) -> None:
        self._backend = backend
        self._calls = calls

    async def scan(self, owner_id: str, persona_id: str, *, fire_time: object) -> tuple[()]:  # noqa: ARG002
        for _ in range(self._calls):
            await self._backend.chat([])  # records usage into the active sink
        return ()


class _NoLLMScanner:
    async def scan(self, owner_id: str, persona_id: str, *, fire_time: object) -> tuple[()]:  # noqa: ARG002
        return ()


class _RecordingPolicy:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def capture_up_to_idempotent(self, **kw: object) -> tuple[int, int]:
        self.calls.append(kw)
        return int(kw["amount"]), 0  # type: ignore[call-overload]


def _handler(
    scanner: object,
    policy: _RecordingPolicy | None,
    *,
    dial: InitiativeDial = DEFAULT_INITIATIVE_DIAL,
) -> InitiativeScanHandler:
    return InitiativeScanHandler(
        scanner=scanner,  # type: ignore[arg-type]
        dial_reader=lambda _o, _p: dial,
        credits_policy=policy,  # type: ignore[arg-type]
        rls_engine=_ENGINE if policy is not None else None,
        cost_source=None,
    )


def _ctx() -> object:
    return SimpleNamespace(owner_id="u1", job_id="j1", meter=lambda **_k: None)


def _payload() -> InitiativeScanPayload:
    return InitiativeScanPayload(persona_id="p1", schedule_id="initsched:p1", fire_time=_FIRE)


@pytest.mark.asyncio
async def test_scan_cost_is_owner_billed_with_the_stable_key() -> None:
    policy = _RecordingPolicy()
    backend = UsageCollectingBackend(_FakeBackend())  # type: ignore[arg-type]
    handler = _handler(_ScanningScanner(backend), policy)
    await handler.handle(_payload(), _ctx())  # type: ignore[arg-type]
    assert len(policy.calls) == 1
    kw = policy.calls[0]
    assert kw["amount"] == 3  # ceil(3.0¢) actual
    assert kw["reason"] == "initiative_scan:actual_openrouter"
    assert kw["cost_basis"] == "actual_openrouter"
    assert kw["billing_key"] == f"initiative_scan:p1:{_FIRE.isoformat()}"
    assert kw["user_id"] == "u1"


@pytest.mark.asyncio
async def test_refire_uses_the_same_billing_key() -> None:
    policy = _RecordingPolicy()
    backend = UsageCollectingBackend(_FakeBackend())  # type: ignore[arg-type]
    handler = _handler(_ScanningScanner(backend), policy)
    await handler.handle(_payload(), _ctx())  # type: ignore[arg-type]
    await handler.handle(_payload(), _ctx())  # type: ignore[arg-type]
    keys = {c["billing_key"] for c in policy.calls}
    assert keys == {f"initiative_scan:p1:{_FIRE.isoformat()}"}  # identical key both fires


@pytest.mark.asyncio
async def test_dial_off_scans_nothing_and_charges_nothing() -> None:
    policy = _RecordingPolicy()
    backend = UsageCollectingBackend(_FakeBackend())  # type: ignore[arg-type]
    handler = _handler(_ScanningScanner(backend), policy, dial=InitiativeDial.OFF)
    await handler.handle(_payload(), _ctx())  # type: ignore[arg-type]
    assert policy.calls == []  # handler exits before the scan — no spend


@pytest.mark.asyncio
async def test_no_llm_call_charges_nothing() -> None:
    policy = _RecordingPolicy()
    handler = _handler(_NoLLMScanner(), policy)
    await handler.handle(_payload(), _ctx())  # type: ignore[arg-type]
    assert policy.calls == []


@pytest.mark.asyncio
async def test_unwired_billing_is_a_noop() -> None:
    backend = UsageCollectingBackend(_FakeBackend())  # type: ignore[arg-type]
    handler = _handler(_ScanningScanner(backend), None)  # no policy
    await handler.handle(_payload(), _ctx())  # type: ignore[arg-type]  # must not raise
