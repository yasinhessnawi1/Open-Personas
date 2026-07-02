"""Unit tests for the model-backed steering interpreter's parsing (Spec A4, composition-root).

Deterministic — a stub backend returns canned JSON; these pin the interpreter's *conservative*
resolution: a clean verb+valid-id resolves, but an unknown verb, a hallucinated/absent id, or an
empty active list never steer (the loop asks instead).
"""

from __future__ import annotations

from typing import Any

import pytest
from persona.backends.types import ChatResponse, TokenUsage
from persona.tasks import IntrospectionStatus, TaskSummary
from persona_runtime.task_origination import ModelSteeringInterpreter, SteeringVerb

_ACTIVE = (
    TaskSummary(task_id="task-fare", goal="track fares", status=IntrospectionStatus.PROGRESSING),
    TaskSummary(task_id="task-news", goal="watch the news", status=IntrospectionStatus.SCHEDULED),
)


class _StubBackend:
    def __init__(self, content: str) -> None:
        self._content = content
        self.calls = 0

    @property
    def provider_name(self) -> str:
        return "anthropic"

    @property
    def model_name(self) -> str:
        return "claude-haiku-4-5-20251001"

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return False

    async def chat(self, messages: object, **kwargs: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        self.calls += 1
        return ChatResponse(
            content=self._content,
            model=self.model_name,
            provider=self.provider_name,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            latency_ms=1.0,
        )


async def _interpret(content: str, message: str = "pause the fare check") -> object:
    return await ModelSteeringInterpreter(backend=_StubBackend(content)).interpret(message, _ACTIVE)


@pytest.mark.asyncio
async def test_clean_pause_resolves_to_the_matched_task() -> None:
    intent = await _interpret('{"verb": "pause", "task_id": "task-fare"}')
    assert intent is not None
    assert intent.verb is SteeringVerb.PAUSE
    assert intent.task_id == "task-fare"


@pytest.mark.asyncio
async def test_cancel_resolves() -> None:
    intent = await _interpret('{"verb": "cancel", "task_id": "task-news"}', "cancel the news one")
    assert intent is not None
    assert intent.verb is SteeringVerb.CANCEL
    assert intent.task_id == "task-news"


@pytest.mark.asyncio
async def test_null_verb_is_not_steering() -> None:
    assert await _interpret('{"verb": null}') is None


@pytest.mark.asyncio
async def test_hallucinated_task_id_is_declined() -> None:
    # A verb with an id NOT in the active set must never steer a task — ask instead.
    assert await _interpret('{"verb": "cancel", "task_id": "task-ghost"}') is None


@pytest.mark.asyncio
async def test_verb_without_a_task_id_is_declined() -> None:
    assert await _interpret('{"verb": "pause"}') is None


@pytest.mark.asyncio
async def test_unparseable_output_is_declined() -> None:
    assert await _interpret("sure, I'll pause it for you") is None


@pytest.mark.asyncio
async def test_empty_active_list_short_circuits_without_a_model_call() -> None:
    backend = _StubBackend('{"verb": "pause", "task_id": "task-fare"}')
    intent = await ModelSteeringInterpreter(backend=backend).interpret("pause it", ())
    assert intent is None
    assert backend.calls == 0  # no active tasks → no model call
