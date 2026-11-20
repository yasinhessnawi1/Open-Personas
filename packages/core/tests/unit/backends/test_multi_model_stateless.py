"""The chain keeps no per-call state: the shared fallback ledger is gone (R9-223).

A tier's :class:`MultiModelChatBackend` is one object shared by every conversation on
the tier. Anything a call stores on it can be overwritten by a concurrent call before
the first call reads it, which is how turns came to be billed at the wrong model. The
served identity and the attempt history now ride each call's own result
(``ServedCall``), so the chain must hold nothing between calls:

* no ``last_attempts`` accessor, and no reader of one anywhere in the packages;
* a call (success, fallback, retry, exhaustion; chat or stream) leaves the chain's
  instance state exactly as it found it.
"""

from __future__ import annotations

import copy
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends.errors import AllModelsFailedError, RateLimitError
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.types import ChatResponse, StreamChunk, TokenUsage
from persona.schema.conversation import ConversationMessage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

_PACKAGES = Path(__file__).resolve().parents[4]
_GONE = re.compile(r"\blast_attempts\b|\bresolve_served_model\b")


def _quota() -> RateLimitError:
    return RateLimitError("429", context={"status_code": "429", "reason": "insufficient_quota"})


def _transient() -> RateLimitError:
    return RateLimitError("429", context={"status_code": "429", "retry_after_s": "0.01"})


class _Member:
    supports_native_tools = False
    supports_vision = False

    def __init__(self, model: str, script: list[object]) -> None:
        self.provider_name = "vendor"
        self.model_name = model
        self.script = list(script)

    def _next(self) -> None:
        outcome = self.script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome

    async def chat(self, messages: Any, **_: Any) -> ChatResponse:  # noqa: ANN401, ARG002
        self._next()
        return ChatResponse(
            content="ok",
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model=self.model_name,
            provider=self.provider_name,
            latency_ms=1.0,
        )

    async def chat_stream(self, messages: Any, **_: Any) -> AsyncIterator[StreamChunk]:  # noqa: ANN401, ARG002
        self._next()
        yield StreamChunk(delta="ok")
        yield StreamChunk(
            delta="",
            is_final=True,
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        )


def _messages() -> list[ConversationMessage]:
    return [ConversationMessage(role="user", content="hi", created_at=datetime.now(UTC))]


def _state(chain: MultiModelChatBackend) -> dict[str, object]:
    return {k: copy.copy(v) for k, v in vars(chain).items()}


_SCRIPTS = {
    "primary": (["ok"], []),
    "fallback": ([_quota()], ["ok"]),
    "retry": ([_transient(), "ok"], []),
    "exhausted": ([_quota()], [_quota()]),
}


def test_the_chain_has_no_ledger_accessor() -> None:
    assert not hasattr(MultiModelChatBackend, "last_attempts")


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("shape", sorted(_SCRIPTS))
@pytest.mark.asyncio
async def test_a_call_leaves_the_shared_chain_exactly_as_it_found_it(
    shape: str, *, stream: bool
) -> None:
    primary_script, fallback_script = _SCRIPTS[shape]
    chain = MultiModelChatBackend(
        [_Member("primary", list(primary_script)), _Member("fallback", list(fallback_script))]
    )
    before = _state(chain)
    try:
        if stream:
            _ = [chunk async for chunk in chain.chat_stream(_messages())]
        else:
            await chain.chat(_messages())
    except AllModelsFailedError:
        assert shape == "exhausted"
    after = _state(chain)
    assert after.keys() == before.keys()
    for key, value in before.items():
        assert after[key] == value, key


def test_no_package_reads_the_removed_ledger() -> None:
    """Source AND tests, the CI-only integration tests included: no reader, no fake."""
    hits = [
        f"{path.relative_to(_PACKAGES)}:{number}"
        for path in _PACKAGES.glob("*/**/*.py")
        if path.name != Path(__file__).name and "node_modules" not in path.parts
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _GONE.search(line)
    ]
    assert hits == []
