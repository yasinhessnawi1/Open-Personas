"""The error frame says whether its message is safe to show (R9-097 remainder).

A consumer in ANOTHER PROCESS cannot tell a sanitised sentence from a raw
stringified exception, because both arrive as a string on the same field. The
connector proved the cost: it received the good message R9-097 produces and
discarded every one in favour of generic copy, so a capacity blip read as a
transient hiccup in the web app and as a broken product on Telegram.

The marker travels beside the message. These pin both directions, because the
value is only real if a consumer can trust it: marked exactly when the mapper
rewrote the text, unmarked whenever it did not.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pytest
from persona.backends import AllModelsFailedError
from persona_api.services.user_facing_errors import CAPACITY_BUSY_MESSAGE
from test_chat_turn_worker import (  # type: ignore[import-not-found]
    _drain,
    _RecordingSink,
    _registry,
    _start,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends import StreamChunk


def _boom(exc: Exception) -> object:
    class _BoomLoop:
        async def turn(self, *_a: object, **_k: object) -> AsyncIterator[StreamChunk]:
            raise exc
            yield  # pragma: no cover - makes this an async generator

    return _BoomLoop()


async def _error_frame(exc: Exception) -> dict[str, Any]:
    handle = _start(_registry(_RecordingSink()), _boom(exc))
    assert handle.task is not None
    await handle.task
    frames = [it for it in _drain(handle) if it is not None and it[0] == "error"]  # type: ignore[index]
    assert frames, "the worker must emit exactly one error frame"
    return cast("dict[str, Any]", frames[0][1])  # type: ignore[index]


@pytest.mark.asyncio
async def test_a_sanitised_message_is_marked_safe() -> None:
    """THE regression: without the marker the good sentence is unusable remotely."""
    frame = await _error_frame(
        AllModelsFailedError(
            "every backend in MultiModelChatBackend exhausted",
            context={"tier": "frontier", "final_error_class": "RateLimitError"},
        )
    )

    assert frame["message"] == CAPACITY_BUSY_MESSAGE
    assert frame["user_facing"] is True


@pytest.mark.asyncio
async def test_a_raw_exception_is_marked_unsafe() -> None:
    """The guard: an unmapped message passes through, so it must NOT be marked.

    This is the direction that matters for security. A marker that were always
    true would hand every raw exception straight to a connector user, which is
    worse than the defect it was added to fix.
    """
    frame = await _error_frame(RuntimeError("psycopg: connection to host 10.0.0.4 failed"))

    assert frame["message"] == "psycopg: connection to host 10.0.0.4 failed"
    assert frame["user_facing"] is False
