"""run_long_poll — the dev inbound transport (Spec C2 T7, D-C2-1).

Offline: a fake client serves canned batches; the loop must advance the offset
(ack), dispatch every update to on_update, and stop on should_continue.

R9-065 additions: a failing ``get_updates`` must be caught + logged (WARNING) and
the loop must continue (never propagate, never kill the service); a normal
shutdown (``asyncio.CancelledError``) must still propagate untouched.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import pytest
from loguru import logger as _loguru_logger
from persona_connectors.telegram.longpoll import run_long_poll

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture
def loguru_warning_capture() -> Iterator[list[str]]:
    """Loguru sink capturing WARNING+ lines — ``persona.logging`` wraps loguru, so
    stdlib ``caplog`` sees nothing (the established repo idiom, e.g.
    ``test_api_app_factory.py``'s ``loguru_info_capture``)."""
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="WARNING")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


class _FakeClient:
    """Serves a fixed sequence of getUpdates batches, recording the offsets it saw."""

    def __init__(self, batches: list[list[dict[str, object]]]) -> None:
        self._batches = batches
        self.offsets_seen: list[int | None] = []
        self._i = 0

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout: int,  # noqa: ARG002 — matches the client signature (the loop passes it)
        allowed_updates: list[str],  # noqa: ARG002 — matches the client signature
    ) -> list[dict[str, object]]:
        self.offsets_seen.append(offset)
        if self._i >= len(self._batches):
            return []
        batch = self._batches[self._i]
        self._i += 1
        return batch


@pytest.mark.asyncio
async def test_dispatches_updates_and_advances_offset() -> None:
    """Each update reaches on_update; offset advances to last_update_id + 1 (ack)."""
    client = _FakeClient(
        [
            [
                {"update_id": 10, "message": {"text": "a"}},
                {"update_id": 11, "message": {"text": "b"}},
            ],
            [{"update_id": 12, "message": {"text": "c"}}],
        ]
    )
    seen: list[dict[str, object]] = []
    calls = {"n": 0}

    async def on_update(update: dict[str, object]) -> None:
        seen.append(update)

    def should_continue() -> bool:
        calls["n"] += 1
        return calls["n"] <= 2  # two polling iterations, then stop

    await run_long_poll(client=client, on_update=on_update, should_continue=should_continue)  # type: ignore[arg-type]

    assert [u["update_id"] for u in seen] == [10, 11, 12]
    # First poll starts with no offset; second poll acks past update 11.
    assert client.offsets_seen == [None, 12]


@pytest.mark.asyncio
async def test_stops_immediately_when_should_continue_is_false() -> None:
    client = _FakeClient([[{"update_id": 1}]])
    seen: list[dict[str, object]] = []

    async def on_update(update: dict[str, object]) -> None:
        seen.append(update)

    await run_long_poll(client=client, on_update=on_update, should_continue=lambda: False)  # type: ignore[arg-type]
    assert seen == []
    assert client.offsets_seen == []


class _FlakyClient:
    """Raises on the first ``fail_first`` calls, then serves ``batches`` in order."""

    def __init__(self, batches: list[list[dict[str, object]]], *, fail_first: int = 1) -> None:
        self._batches = batches
        self._fail_first = fail_first
        self._calls = 0
        self.offsets_seen: list[int | None] = []

    async def get_updates(
        self,
        *,
        offset: int | None,
        timeout: int,  # noqa: ARG002 — matches the client signature
        allowed_updates: list[str],  # noqa: ARG002 — matches the client signature
    ) -> list[dict[str, object]]:
        self._calls += 1
        if self._calls <= self._fail_first:
            raise RuntimeError("simulated network blip")
        self.offsets_seen.append(offset)
        idx = self._calls - self._fail_first - 1
        if idx >= len(self._batches):
            return []
        return self._batches[idx]


class _CancellingClient:
    """Raises CancelledError from get_updates (simulates a normal shutdown mid-poll)."""

    async def get_updates(
        self,
        *,
        offset: int | None,  # noqa: ARG002
        timeout: int,  # noqa: ARG002
        allowed_updates: list[str],  # noqa: ARG002
    ) -> list[dict[str, object]]:
        raise asyncio.CancelledError


@pytest.mark.asyncio
async def test_get_updates_failure_is_caught_logged_and_the_loop_continues(
    loguru_warning_capture: list[str],
) -> None:
    """A get_updates that raises once then succeeds: the loop CONTINUES (no
    propagation), logs a WARNING, and a subsequent update is still processed."""
    client = _FlakyClient([[{"update_id": 5, "message": {"text": "x"}}]], fail_first=1)
    seen: list[dict[str, object]] = []

    async def on_update(update: dict[str, object]) -> None:
        seen.append(update)

    calls = {"n": 0}

    def should_continue() -> bool:
        calls["n"] += 1
        return calls["n"] <= 2  # the failing iteration + the succeeding iteration

    await run_long_poll(
        client=client,  # type: ignore[arg-type]
        on_update=on_update,
        should_continue=should_continue,
        error_backoff_seconds=0,  # no real delay in tests
    )

    assert [u["update_id"] for u in seen] == [5]
    assert any("get_updates failed" in line for line in loguru_warning_capture), (
        loguru_warning_capture
    )


@pytest.mark.asyncio
async def test_cancelled_error_from_get_updates_propagates() -> None:
    """A normal shutdown (CancelledError) is re-raised, never swallowed as a poll failure."""

    async def on_update(
        update: dict[str, object],  # noqa: ARG001 — never reached; matches on_update's signature
    ) -> None:
        raise AssertionError("on_update should not be called")

    with pytest.raises(asyncio.CancelledError):
        await run_long_poll(
            client=_CancellingClient(),  # type: ignore[arg-type]
            on_update=on_update,
            error_backoff_seconds=0,
        )


# --- the one fault retrying cannot fix: a second consumer on the same bot token ---


class _ConflictingClient:
    """Telegram's 409 on every poll — what a second consumer looks like from in here."""

    def __init__(self, *, conflicts: int) -> None:
        self._left = conflicts
        self.calls = 0

    async def get_updates(
        self,
        *,
        offset: int | None,  # noqa: ARG002 — matches the client signature
        timeout: int,  # noqa: ARG002 — matches the client signature
        allowed_updates: list[str],  # noqa: ARG002 — matches the client signature
    ) -> list[dict[str, object]]:
        self.calls += 1
        if self._left > 0:
            self._left -= 1
            raise RuntimeError(
                "telegram API error: Conflict: terminated by other getUpdates request; "
                "make sure that only one bot instance is running "
                "[method=getUpdates error_code=409]"
            )
        return []


async def _poll(client: object, *, iterations: int) -> None:
    budget = {"n": iterations}

    def should_continue() -> bool:
        budget["n"] -= 1
        return budget["n"] >= 0

    await run_long_poll(
        client=client,  # type: ignore[arg-type]
        on_update=lambda _u: asyncio.sleep(0),
        should_continue=should_continue,
        error_backoff_seconds=0.0,
    )


@pytest.fixture
def loguru_error_capture() -> Iterator[list[str]]:
    captured: list[str] = []
    sink_id = _loguru_logger.add(lambda msg: captured.append(str(msg)), level="ERROR")
    try:
        yield captured
    finally:
        _loguru_logger.remove(sink_id)


@pytest.mark.asyncio
async def test_a_sustained_conflict_is_escalated_and_names_the_remedy(
    loguru_error_capture: list[str],
) -> None:
    """A second consumer ran for forty hours logged only as a repeating WARNING, and was
    found by a person saying the bot felt "glitchy". Retrying cannot fix this one: the other
    consumer is not going away, so the loop has to say what is wrong in words someone can
    act on."""
    await _poll(_ConflictingClient(conflicts=20), iterations=20)

    assert loguru_error_capture, "a sustained 409 must escalate past WARNING"
    said = " ".join(loguru_error_capture)
    assert "TWO consumers" in said
    assert "PERSONA_API_EMBED_CONNECTORS" in said, "the error must name the usual cause"
    assert "connectors_fold_cutover" in said, "and where the remedy is written down"


@pytest.mark.asyncio
async def test_a_brief_conflict_stays_a_warning(loguru_error_capture: list[str]) -> None:
    """A deploy overlaps two consumers by design for a few seconds. Escalating on that
    would train everyone to ignore the error, which is how the real one got missed."""
    await _poll(_ConflictingClient(conflicts=2), iterations=6)

    assert loguru_error_capture == []


@pytest.mark.asyncio
async def test_the_conflict_run_resets_once_polling_recovers(
    loguru_error_capture: list[str],
) -> None:
    """Consecutive, not cumulative. Occasional conflicts spread over days are a different
    thing from two consumers running right now, and only the second is worth an ERROR."""
    client = _ConflictingClient(conflicts=4)  # under the bar, then succeeds
    await _poll(client, iterations=12)

    assert loguru_error_capture == []
    assert client.calls > 4, "the loop must keep polling after the conflicts clear"
