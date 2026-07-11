"""R9-020 — unit tests for the ``title_refresh`` A0 tenant.

Fast, no-DB (the episodic/consolidation handler test pattern): the handler with
a SCRIPTED generator — a good generation writes the sanitized title + publishes
``sidebar.changed``; a BAD generation (instruction echo / empty — the exact R4
bug class) is a keep-existing no-op with one WARNING and NEVER regresses the
title to first-words; a deleted conversation is a graceful no-op. Plus the
excerpt window (first 4 + last 12, per-message truncation) and registration.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

import pytest
from loguru import logger
from persona.jobs import JobRegistry
from persona_api.jobs.handlers.title_refresh import (
    TITLE_REFRESH_JOB_TYPE,
    TitleRefreshData,
    TitleRefreshHandler,
    TitleRefreshJobPayload,
    register_title_refresh_handler,
    transcript_excerpt,
)

if TYPE_CHECKING:
    from collections.abc import Iterator

_ECHO = "We need to output a title of at most 5 words, no quotes, no punctuation, no prose"


class _Repo:
    """Recording fake of the TitleRepository port."""

    def __init__(self, data: TitleRefreshData | None) -> None:
        self._data = data
        self.writes: list[tuple[str, str]] = []
        self.write_result = True

    def read(self, conn: object, *, conversation_id: str) -> TitleRefreshData | None:  # noqa: ARG002
        return self._data

    def write_title(self, conn: object, *, conversation_id: str, title: str) -> bool:  # noqa: ARG002
        self.writes.append((conversation_id, title))
        return self.write_result


class _Channel:
    """Duck-typed UserEventChannel: records every publish."""

    def __init__(self) -> None:
        self.published: list[tuple[str, object]] = []

    def publish(self, owner_id: str, event: object) -> None:
        self.published.append((owner_id, event))


class _Ctx:
    """Duck-typed JobContext: owner-scoped connection + meter recording."""

    def __init__(self) -> None:
        self.owner_id = "u1"
        self.metered: list[dict[str, Any]] = []

    def connection(self) -> contextlib.AbstractContextManager[object]:
        return contextlib.nullcontext(object())

    def meter(self, **kwargs: Any) -> None:  # noqa: ANN401
        self.metered.append(kwargs)


def _generator(raw: str) -> Any:  # noqa: ANN401
    async def generate(_excerpt: str) -> str:
        return raw

    return generate


def _data(*, title: str = "Old good title", turns: int = 3) -> TitleRefreshData:
    transcript: list[tuple[str, str]] = []
    for i in range(turns):
        transcript.append(("user", f"question {i}"))
        transcript.append(("assistant", f"answer {i}"))
    return TitleRefreshData(title=title, transcript=tuple(transcript))


@pytest.fixture
def captured_warnings() -> Iterator[list[str]]:
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(m), level="WARNING")
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def _run(handler: TitleRefreshHandler, ctx: _Ctx | None = None) -> _Ctx:
    context = ctx or _Ctx()
    payload = TitleRefreshJobPayload(conversation_id="conv_1", threshold=4)
    asyncio.run(handler.handle(payload, context))  # type: ignore[arg-type]
    return context


# ----- good generation: write + publish --------------------------------------


def test_good_generation_writes_the_sanitized_title_and_publishes() -> None:
    repo = _Repo(_data())
    channel = _Channel()
    handler = TitleRefreshHandler(
        generator=_generator('"Norwegian lease dispute."'),
        repository=repo,
        event_channel=channel,  # type: ignore[arg-type]
    )
    ctx = _run(handler)

    assert repo.writes == [("conv_1", "Norwegian lease dispute")]  # quotes/punct stripped
    assert len(channel.published) == 1
    owner, event = channel.published[0]
    assert owner == "u1"
    assert getattr(event, "type", None) == "sidebar.changed"
    assert getattr(event, "reason", None) == "conversation.title_updated"
    assert len(ctx.metered) == 1
    assert ctx.metered[0]["detail"]["surface"] == "title_refresh"


# ----- bad generation: keep-existing, WARNING, no publish ---------------------


def test_echo_generation_keeps_the_existing_title_with_one_warning(
    captured_warnings: list[str],
) -> None:
    # The refresh contract that DIFFERS from the first-turn hook: where the
    # sanitizer would fall back (echo), the job keeps the existing title —
    # never a regression to first-6-words.
    repo = _Repo(_data(title="A perfectly good title"))
    channel = _Channel()
    handler = TitleRefreshHandler(
        generator=_generator(_ECHO),
        repository=repo,
        event_channel=channel,  # type: ignore[arg-type]
    )
    ctx = _run(handler)

    assert repo.writes == []  # existing title untouched
    assert channel.published == []  # no ping for a no-op
    assert ctx.metered == []
    assert sum("title refresh kept the existing title" in m for m in captured_warnings) == 1


def test_empty_generation_keeps_the_existing_title(captured_warnings: list[str]) -> None:
    repo = _Repo(_data())
    handler = TitleRefreshHandler(generator=_generator("   "), repository=repo)
    _run(handler)
    assert repo.writes == []
    assert sum("title refresh kept the existing title" in m for m in captured_warnings) == 1


# ----- graceful no-ops --------------------------------------------------------


def test_conversation_gone_is_a_graceful_noop() -> None:
    repo = _Repo(None)  # deleted between enqueue and run
    channel = _Channel()
    handler = TitleRefreshHandler(
        generator=_generator("Should never be asked"),
        repository=repo,
        event_channel=channel,  # type: ignore[arg-type]
    )
    _run(handler)
    assert repo.writes == []
    assert channel.published == []


def test_empty_transcript_is_a_graceful_noop() -> None:
    repo = _Repo(TitleRefreshData(title="t", transcript=()))
    handler = TitleRefreshHandler(generator=_generator("Anything"), repository=repo)
    _run(handler)
    assert repo.writes == []


def test_unchanged_title_skips_the_write_and_the_ping() -> None:
    # Idempotent re-run: the temperature-0 regeneration converging on the same
    # title is a no-op, not a redundant write + sidebar refresh storm.
    repo = _Repo(_data(title="Norwegian lease dispute"))
    channel = _Channel()
    handler = TitleRefreshHandler(
        generator=_generator("Norwegian lease dispute"),
        repository=repo,
        event_channel=channel,  # type: ignore[arg-type]
    )
    _run(handler)
    assert repo.writes == []
    assert channel.published == []


def test_deleted_mid_generation_skips_the_ping() -> None:
    repo = _Repo(_data())
    repo.write_result = False  # UPDATE matched no row
    channel = _Channel()
    handler = TitleRefreshHandler(
        generator=_generator("Fresh title"),
        repository=repo,
        event_channel=channel,  # type: ignore[arg-type]
    )
    ctx = _run(handler)
    assert repo.writes == [("conv_1", "Fresh title")]
    assert channel.published == []
    assert ctx.metered == []


# ----- the excerpt window ------------------------------------------------------


def test_excerpt_keeps_short_transcripts_whole() -> None:
    excerpt = transcript_excerpt((("user", "hi"), ("assistant", "hello")))
    assert excerpt == "user: hi\nassistant: hello"


def test_excerpt_windows_first_4_plus_last_12() -> None:
    transcript = tuple(("user", f"m{i}") for i in range(40))
    excerpt = transcript_excerpt(transcript)
    lines = excerpt.splitlines()
    assert len(lines) == 4 + 1 + 12  # head + omission marker + tail
    assert lines[0] == "user: m0"
    assert lines[3] == "user: m3"
    assert "omitted" in lines[4]
    assert lines[5] == "user: m28"
    assert lines[-1] == "user: m39"


def test_excerpt_truncates_each_message_to_500_chars() -> None:
    excerpt = transcript_excerpt((("user", "x" * 2000),))
    (line,) = excerpt.splitlines()
    assert len(line) <= len("user: ") + 500 + 1  # +1 for the ellipsis
    assert line.endswith("…")


def test_excerpt_collapses_internal_whitespace() -> None:
    excerpt = transcript_excerpt((("user", "a\n\n  b\t c"),))
    assert excerpt == "user: a b c"


# ----- registration -------------------------------------------------------------


def test_registration_declares_type_payload_and_key() -> None:
    registry = JobRegistry()
    register_title_refresh_handler(registry, generator=_generator("t"))
    assert TITLE_REFRESH_JOB_TYPE in registry.types()
    spec = registry.get(TITLE_REFRESH_JOB_TYPE)
    assert spec.payload_model is TitleRefreshJobPayload
    payload = TitleRefreshJobPayload(conversation_id="c", threshold=10)
    assert spec.idempotency_key(payload) == "title:c:10"
