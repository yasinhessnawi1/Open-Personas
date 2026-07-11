"""The ``title_refresh`` job handler — dynamic self-improving chat titles (R9-020).

The first turn's auto-title only ever sees the FIRST user message; as a
conversation grows, that title stops describing it. This durable A0 tenant
re-reads the transcript at message-count thresholds (the producer half lives in
``persona_api.services.title_trigger``) and regenerates a ≤5-word title over the
WHOLE conversation on the TITLE tier (P9 surface ``title`` — mid by default,
``PERSONA_API_TITLE_TIER`` overridable; the backend is resolved ONCE at worker
composition and closed over, the ``worker_root`` synthesis pattern).

Refresh semantics differ deliberately from the first-turn hook: on a BAD
generation (the strict sanitizer — :func:`sanitize_title_candidate` — rejects
it), the job KEEPS the existing title untouched (a no-op + one WARNING). A
refresh must never regress an existing good title to a first-6-words fallback;
only the first-turn path composes a fallback (better than an empty title there).

Every successful title write publishes ``sidebar.changed``
(reason=``conversation.title_updated``) post-commit, so open tabs pick the new
title up live over the R9-012 me-events channel.

Idempotency: the enqueue key is ``title:{conversation_id}:{threshold}`` (A0's
``ON CONFLICT`` dedup — one refresh per conversation per threshold); the handler
itself is convergent (temperature-0 regeneration + same-title short-circuit), the
second line behind the key.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.jobs import MEDIUM_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update

from persona_api.db.models import conversations, messages

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from persona.backends import ChatBackend
    from persona.jobs import JobContext, JobRegistry
    from sqlalchemy import Connection

    from persona_api.jobs.queue import JobQueue, JobRecord
    from persona_api.realtime.channel import UserEventChannel

__all__ = [
    "TITLE_REFRESH_JOB_TYPE",
    "PgTitleRepository",
    "TitleRefreshData",
    "TitleRefreshHandler",
    "TitleRefreshJobPayload",
    "TitleRepository",
    "build_title_refresh_generator",
    "enqueue_title_refresh",
    "register_title_refresh_handler",
    "title_refresh_idempotency_key",
]

TITLE_REFRESH_JOB_TYPE = "title_refresh"

_logger = get_logger("jobs.title_refresh")

# Roles whose text forms the titling transcript (the synthesis `_TRANSCRIPT_ROLES`
# discipline — system nudges / tool frames never steer the title).
_TRANSCRIPT_ROLES = ("user", "assistant")

# The excerpt window (R9-020 fix design): the OPENING anchors what the chat set
# out to be, the tail is what it became — first 4 + last 12 messages, each
# truncated so one pasted wall of text cannot blow the prompt budget.
_EXCERPT_HEAD_MESSAGES = 4
_EXCERPT_TAIL_MESSAGES = 12
_EXCERPT_MESSAGE_CHARS = 500


class TitleRefreshJobPayload(JobPayload):
    """Which conversation to re-title, keyed by the threshold that fired it."""

    conversation_id: str
    threshold: int


def title_refresh_idempotency_key(payload: TitleRefreshJobPayload) -> str:
    """``title:{conversation_id}:{threshold}`` — one refresh per threshold crossing."""
    return f"title:{payload.conversation_id}:{payload.threshold}"


class TitleRefreshData(BaseModel):
    """What the repository reads for one refresh (current title + transcript)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    title: str
    transcript: tuple[tuple[str, str], ...]


@runtime_checkable
class TitleRepository(Protocol):
    """The DB port the handler uses (owner-scoped via the job's connection)."""

    def read(self, conn: Connection, *, conversation_id: str) -> TitleRefreshData | None:
        """Read the current title + transcript, or ``None`` if the conversation is gone."""
        ...

    def write_title(self, conn: Connection, *, conversation_id: str, title: str) -> bool:
        """Persist the refreshed title; ``False`` if the conversation vanished."""
        ...


class PgTitleRepository:
    """Postgres-backed :class:`TitleRepository` (owner-scoped via the job conn)."""

    def read(self, conn: Connection, *, conversation_id: str) -> TitleRefreshData | None:
        row = conn.execute(
            select(conversations.c.title).where(conversations.c.id == conversation_id)
        ).one_or_none()
        if row is None:
            return None
        rows = conn.execute(
            select(messages.c.role, messages.c.content)
            .where(messages.c.conversation_id == conversation_id)
            .order_by(messages.c.created_at, messages.c.id)
        ).all()
        transcript = tuple(
            (r.role, r.content) for r in rows if r.role in _TRANSCRIPT_ROLES and r.content
        )
        return TitleRefreshData(title=row.title or "", transcript=transcript)

    def write_title(self, conn: Connection, *, conversation_id: str, title: str) -> bool:
        result = conn.execute(
            update(conversations).where(conversations.c.id == conversation_id).values(title=title)
        )
        return bool(result.rowcount)


def transcript_excerpt(transcript: Sequence[tuple[str, str]]) -> str:
    """Format the titling excerpt: first 4 + last 12 messages, ≤500 chars each."""
    if len(transcript) <= _EXCERPT_HEAD_MESSAGES + _EXCERPT_TAIL_MESSAGES:
        rows = list(transcript)
    else:
        rows = [
            *transcript[:_EXCERPT_HEAD_MESSAGES],
            ("system", "[… earlier messages omitted …]"),
            *transcript[-_EXCERPT_TAIL_MESSAGES:],
        ]
    lines: list[str] = []
    for role, content in rows:
        text = " ".join(content.split())
        if len(text) > _EXCERPT_MESSAGE_CHARS:
            text = text[:_EXCERPT_MESSAGE_CHARS] + "…"
        lines.append(f"{role}: {text}")
    return "\n".join(lines)


def build_title_refresh_generator(backend: ChatBackend) -> Callable[[str], Awaitable[str]]:
    """Close the composed title-tier backend over a transcript→raw-title call.

    The backend is resolved ONCE at worker composition (``worker_root``), the
    exact synthesis pattern — never per job. The prompt asks for a whole-
    conversation title; the STRICT sanitizer downstream rejects an instruction
    echo, so a weak model degrades to keep-existing, never to a stored prompt.
    """

    async def generate(excerpt: str) -> str:
        from datetime import UTC, datetime  # noqa: PLC0415

        from persona.schema.conversation import ConversationMessage  # noqa: PLC0415

        now = datetime.now(UTC)
        prompt = [
            ConversationMessage(
                role="system",
                content=(
                    "You are shown a conversation transcript. Summarise the WHOLE "
                    "conversation as a title of at most 5 words. Output ONLY the "
                    "title — no quotes, no punctuation, no prose."
                ),
                created_at=now,
            ),
            ConversationMessage(role="user", content=excerpt, created_at=now),
        ]
        response = await backend.chat(prompt, temperature=0.0, max_tokens=24)
        return response.content or ""

    return generate


class TitleRefreshHandler:
    """Re-title one conversation from its transcript; keep-existing on a bad gen."""

    def __init__(
        self,
        *,
        generator: Callable[[str], Awaitable[str]],
        repository: TitleRepository,
        event_channel: UserEventChannel | None = None,
    ) -> None:
        self._generate = generator
        self._repo = repository
        self._event_channel = event_channel

    async def handle(self, payload: TitleRefreshJobPayload, context: JobContext) -> None:
        # R9-020 keep-existing contract: import the STRICT sanitizer half — a
        # rejected generation must NOT fall back to first-words on refresh.
        from persona_api.services.chat_service import sanitize_title_candidate  # noqa: PLC0415

        with context.connection() as conn:
            data = self._repo.read(conn, conversation_id=payload.conversation_id)
        if data is None or not data.transcript:
            return  # conversation deleted (or empty) between enqueue and run — nothing to do.

        # The model call runs OUTSIDE any DB transaction (never hold a conn
        # across an LLM await); the write below re-checks existence.
        raw = await self._generate(transcript_excerpt(data.transcript))
        candidate = sanitize_title_candidate(raw)
        if candidate is None:
            _logger.warning(
                "title refresh kept the existing title — generation unusable "
                "(echo/empty); the sanitizer would only have offered first-words",
                conversation_id=payload.conversation_id,
                threshold=payload.threshold,
            )
            return
        if candidate == data.title:
            return  # already describes the conversation — no write, no ping.

        with context.connection() as conn:
            written = self._repo.write_title(
                conn, conversation_id=payload.conversation_id, title=candidate
            )
        if not written:
            return  # deleted mid-generation — graceful no-op.

        context.meter(
            amount_micros=0,
            kind="model",
            detail={
                "surface": "title_refresh",
                "conversation_id": payload.conversation_id,
                "threshold": str(payload.threshold),
            },
        )
        # Post-commit liveness ping (R9-012 channel): open tabs refetch the
        # sidebar and see the improved title without a reload. Best-effort.
        from persona_api.services.notifications_service import (  # noqa: PLC0415
            publish_sidebar_changed,
        )

        publish_sidebar_changed(
            self._event_channel,
            owner_id=context.owner_id,
            reason="conversation.title_updated",
        )


def register_title_refresh_handler(
    registry: JobRegistry,
    *,
    generator: Callable[[str], Awaitable[str]],
    repository: TitleRepository | None = None,
    event_channel: UserEventChannel | None = None,
) -> None:
    """Register the title-refresh tenant (R9-020; the episodic-registration shape)."""
    registry.register(
        JobTypeSpec(
            type=TITLE_REFRESH_JOB_TYPE,
            payload_model=TitleRefreshJobPayload,
            handler=TitleRefreshHandler(
                generator=generator,
                repository=repository if repository is not None else PgTitleRepository(),
                event_channel=event_channel,
            ),
            idempotency_key=title_refresh_idempotency_key,
            retry=RetryPolicy(max_attempts=2),
            lease=MEDIUM_LEASE,
        )
    )


def enqueue_title_refresh(
    queue: JobQueue,
    *,
    owner_id: str,
    conversation_id: str,
    threshold: int,
) -> JobRecord | None:
    """Enqueue one threshold-keyed refresh; a duplicate is A0's ``ON CONFLICT`` no-op.

    Returns the inserted record, or ``None`` when the key already exists (the
    dedup outcome — observable, so tests prove it rather than assume it).
    """
    payload = TitleRefreshJobPayload(conversation_id=conversation_id, threshold=threshold)
    return queue.enqueue(
        type=TITLE_REFRESH_JOB_TYPE,
        owner_id=owner_id,
        payload=payload.model_dump(),
        idempotency_key=title_refresh_idempotency_key(payload),
    )
