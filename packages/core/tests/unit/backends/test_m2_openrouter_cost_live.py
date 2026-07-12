"""M2-T7 — the ONE live OpenRouter leg (``@pytest.mark.external``, CI-excluded).

Proves the REAL wire contract the scripted T3 payloads encode: with usage
accounting opted in (``usage: {"include": true}`` — the D-M2-3 merge), a real
OpenRouter response's final usage carries ``cost``, and the backend surfaces
it as ``TokenUsage.cost_usd``. Run against a ``:free`` route so the leg costs
NOTHING (a ``:free`` actual is 0.0 — and that zero IS the assertion: the field
must be PRESENT, not None, which only happens when the opt-in reached the
wire and the parse read the answer).

NOT run in CI and not run by the implementing agent (no live spend by
machine); the owner runs it:

    PERSONA_OPENROUTER_API_KEY=sk-or-v1-... \
    uv run pytest packages/core/tests/unit/backends/test_m2_openrouter_cost_live.py \
        -o addopts="" -m external -q

Optionally override the model if the default free route rotates:
``PERSONA_M2_LIVE_FREE_MODEL=<some-current>:free``.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

import pytest
from persona.backends.config import BackendConfig
from persona.backends.openai_compat import OpenAICompatibleBackend
from persona.schema.conversation import ConversationMessage
from pydantic import SecretStr

pytestmark = pytest.mark.external

_DEFAULT_FREE_MODEL = "meta-llama/llama-3.3-70b-instruct:free"


def _backend() -> OpenAICompatibleBackend:
    api_key = os.environ.get("PERSONA_OPENROUTER_API_KEY", "").strip()
    if not api_key:
        pytest.skip("PERSONA_OPENROUTER_API_KEY not set; live leg needs a real key")
    model = os.environ.get("PERSONA_M2_LIVE_FREE_MODEL", "").strip() or _DEFAULT_FREE_MODEL
    return OpenAICompatibleBackend(
        BackendConfig(
            provider="openrouter",  # type: ignore[arg-type]
            model=model,
            api_key=SecretStr(api_key),
        )
    )


def _user(text: str) -> ConversationMessage:
    return ConversationMessage(role="user", content=text, created_at=datetime.now(UTC))


@pytest.mark.asyncio
async def test_live_free_route_reports_a_zero_actual_non_streaming() -> None:
    backend = _backend()
    response = await backend.chat([_user("Reply with the single word: ok")], max_tokens=8)
    # The wire contract: usage accounting was honoured — cost is PRESENT.
    assert response.usage.cost_usd is not None
    # ...and a :free route's actual is exactly 0.0 (recorded as an ACTUAL
    # downstream, basis actual_openrouter — never unpriced).
    assert response.usage.cost_usd == 0.0
    assert response.usage.total_tokens > 0


@pytest.mark.asyncio
async def test_live_free_route_reports_a_zero_actual_streaming() -> None:
    backend = _backend()
    final = None
    async for chunk in backend.chat_stream([_user("Reply with the single word: ok")], max_tokens=8):
        if chunk.is_final:
            final = chunk
    assert final is not None
    assert final.usage is not None
    # The final streaming usage chunk carries the actual too (the D-M2-3
    # streaming arm — same contract as non-streaming).
    assert final.usage.cost_usd is not None
    assert final.usage.cost_usd == 0.0
