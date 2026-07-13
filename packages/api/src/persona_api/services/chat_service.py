"""Conversation lifecycle + detached chat turns (spec 08 T08 + spec P1 T2b, KEYSTONE 1).

CRUD for conversations + the chat-turn entry points. Spec P1 reshaped the turn
from inline-streaming (persist-after-final) into a **detached, resumable
session** (D-P1-detached-execution):

- :func:`start_chat_turn` — persist the user message + an in-progress assistant
  row at turn START (``MessagesTurnSink.open_turn``), resolve images/documents,
  build the loop, and launch the turn as a detached background task via the
  ``ChatTurnRegistry``. A client disconnect no longer cancels the turn.
- :func:`stream_turn` — stream the live tail (events + chunks + the terminal
  ``done`` / ``error`` frame) from the turn's in-process queue. The SAME
  generator serves the originating POST and every reattach (T4).

The worker (``background.chat_turn_worker``) owns the during-turn checkpointing,
the terminal finalize, and the credits deduct on clean completion (the D-08-6
revision — bill regardless of client presence, D-P1-billing-contract). The old
persist-in-the-generator hazard is gone: persistence + billing live in the
detached task, so a mid-stream disconnect never loses the turn or skips the bill.

The ``ConversationLoop`` is built per-request by the runtime factory (T10),
injected as ``loop_builder`` so the flow stays testable with a scripted backend.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from typing import TYPE_CHECKING, Literal, cast

from persona.errors import PersonaError, PersonaNotFoundError
from persona.logging import get_logger
from persona.schema.conversation import Conversation, ConversationMessage
from sqlalchemy import delete, func, insert, over, select, update
from sqlalchemy.exc import IntegrityError

from persona_api.db.engine import aware_utc
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import messages as messages_t
from persona_api.db.models import personas as personas_t
from persona_api.errors import ConversationNotFoundError
from persona_api.services import document_service, image_service
from persona_api.services.message_metadata import metadata_from_channel

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable
    from pathlib import Path

    from persona.backends import StreamChunk
    from persona.sandbox.result import SandboxFile
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.images import TurnImage
    from persona_runtime.loop import ConversationLoop
    from persona_runtime.prompt import DocumentContext
    from sqlalchemy import Connection, Engine

    from persona_api.background.chat_turn_worker import ChatTurnHandle, ChatTurnRegistry
    from persona_api.realtime.channel import UserEventChannel
    from persona_api.schemas import ChannelContext
    from persona_api.schemas import ImageRef as ImageRefSchema
    from persona_api.services.chat_turn_sink import MessagesTurnSink
    from persona_api.storage import FileStorage

    # The runtime factory (T10) builds a ConversationLoop for a persona under the
    # current request's RLS scope, given the persona_id.
    LoopBuilder = Callable[[str], Awaitable[ConversationLoop]]


__all__ = [
    "create_conversation",
    "delete_conversation",
    "edit_and_rerun_turn",
    "get_conversation",
    "list_conversations",
    "get_active_turn",
    "regenerate_turn",
    "set_title",
    "start_chat_turn",
    "stream_turn",
]

_log = get_logger("api.chat")

# The message role, mirroring ConversationMessage.role + the messages_role_check
# DB CHECK constraint (the source of truth that makes the cast in _to_message
# sound).
Role = Literal["user", "assistant", "system", "tool"]

# A title builder turns the first user message into a short conversation title.
TitleBuilder = "Callable[[str], Awaitable[str]]"
_MAX_TITLE_LEN = 120
# A short title is a few words; the prompt asks for ≤5, we allow a little slack
# and hard-cap so a reasoning leak can never become a paragraph-long "title".
_MAX_TITLE_WORDS = 8
_FALLBACK_TITLE_WORDS = 6
_DEFAULT_TITLE = "New conversation"

# R4 T3: substrings that betray the titling INSTRUCTION being echoed back (or a
# small/background-tier model leaking chain-of-thought about it) instead of an
# actual title — e.g. "We need to output a title of at most 5 words, no quotes,
# no punctuation, no prose…". Matched case-insensitively; a hit means "not a
# title" → fall back. Kept deliberately instruction-specific so a genuine short
# title is not caught.
_TITLE_ECHO_MARKERS: tuple[str, ...] = (
    "at most",
    "no quotes",
    "no punctuation",
    "no prose",
    "conversation title",
    "output only",
    "a title of",
    "5 words",
    "five words",
    "we need to",
    "the user's message",
    "summarise the user",
    "summarize the user",
    "as a title",
)


def _fallback_title(first_message: str) -> str:
    """A sensible title when the model's output is unusable: the first few words
    of the user's own message, else a neutral default."""
    words = first_message.split()
    if not words:
        return _DEFAULT_TITLE
    return " ".join(words[:_FALLBACK_TITLE_WORDS])


def sanitize_title_candidate(raw: str) -> str | None:
    """The strict half of title sanitisation: a cleaned title, or ``None`` when
    the generation is unusable (R9-020).

    Rejects an instruction echo (the R4 bug class), strips wrapping quotes +
    trailing punctuation, and caps the word count + length. Returns ``None``
    instead of falling back so REFRESH callers (the ``title_refresh`` job) can
    keep an existing good title rather than regress it to first-words — the
    first-turn path composes its fallback via :func:`sanitize_conversation_title`.
    """
    if not raw:
        return None
    # First non-empty line only — a model may wrap the title in prose.
    line = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    cleaned = line.strip("\"'“”‘’`").strip()
    if any(marker in cleaned.lower() for marker in _TITLE_ECHO_MARKERS):
        return None
    cleaned = cleaned.rstrip(".!?,;:").strip()
    if not cleaned:
        return None
    words = cleaned.split()
    if len(words) > _MAX_TITLE_WORDS:
        cleaned = " ".join(words[:_MAX_TITLE_WORDS])
    return cleaned[:_MAX_TITLE_LEN] or None


def sanitize_conversation_title(raw: str, *, first_message: str) -> str:
    """Turn a title-model's raw output into a safe conversation title (R4 T3).

    Root cause of the reported bug: the titling PROMPT is not stored — a
    small/background-tier model echoes its own instruction (or leaks reasoning
    about it) into ``message.content``, and that echo was stored verbatim as the
    title. This rejects such an echo, strips wrapping quotes + trailing
    punctuation the prompt asked to avoid, caps the word count, and falls back to
    the first words of the user's message (or a default) on a bad generation.
    Always returns a non-empty, human-readable title.
    """
    candidate = sanitize_title_candidate(raw)
    return candidate if candidate is not None else _fallback_title(first_message)


# Server-side cap on the last-message preview returned by the LIST endpoint, so
# the sidebar never has to ship/trim a full message body. Longer messages are
# truncated and get a trailing ellipsis (see _truncate_preview).
LAST_MESSAGE_PREVIEW_MAX_LEN = 120

# Defensive per-file cap on a document staged into the sandbox input mount
# (document-workspace cascade). Documents are already size-validated at upload,
# but this bounds the bytes we copy into a single turn's sandbox input set so a
# pathological doc can't blow up the input payload. Mirrors the image
# MAX_UPLOAD_BYTES (20 MiB).
MAX_STAGED_DOCUMENT_BYTES = 20 * 1024 * 1024


def create_conversation(
    *, rls_engine: Engine, owner_id: str, persona_id: str, title: str, origin: str = "chat"
) -> str:
    """Create a conversation against a persona (RLS-scoped). Returns its id.

    ``origin`` is the immutable birth-marker (Spec V9, V9-D-3): ``'chat'`` (the
    default — text-born) or ``'call'`` (the web sets this when creating a
    conversation to host a voice call). Set ONCE here, never mutated.
    """
    conv_id = f"conv_{uuid.uuid4().hex}"
    with rls_engine.begin() as conn:
        # Verify the persona is the caller's (RLS would hide it otherwise).
        exists = conn.execute(select(personas_t.c.id).where(personas_t.c.id == persona_id)).first()
        if exists is None:
            raise PersonaNotFoundError("persona not found", context={"id": persona_id})
        conn.execute(
            insert(conversations_t).values(
                id=conv_id, owner_id=owner_id, persona_id=persona_id, title=title, origin=origin
            )
        )
    return conv_id


def delete_conversation(*, rls_engine: Engine, conversation_id: str) -> None:
    """Delete a conversation (cascades to its messages + turn_logs via FK).

    RLS-scoped → a conversation that isn't the caller's is invisible and the
    delete matches no row → 404.
    """
    with rls_engine.begin() as conn:
        result = conn.execute(
            delete(conversations_t)
            .where(conversations_t.c.id == conversation_id)
            .returning(conversations_t.c.id)
        )
        if result.first() is None:
            raise ConversationNotFoundError(
                "conversation not found", context={"id": conversation_id}
            )


def set_title(*, rls_engine: Engine, conversation_id: str, title: str) -> None:
    """Set a conversation's title (RLS-scoped; used by the auto-title path)."""
    with rls_engine.begin() as conn:
        conn.execute(
            update(conversations_t)
            .where(conversations_t.c.id == conversation_id)
            .values(title=title)
        )


def list_conversations(*, rls_engine: Engine, limit: int, offset: int) -> list[dict[str, object]]:
    """List the caller's CHAT conversations (RLS-scoped), paginated.

    Each returned row carries the conversation columns PLUS two derived
    last-message fields — ``last_message_preview`` (the most recent message's
    text, already trimmed + truncated server-side) and ``last_message_role`` —
    so the web sidebar can render a real preview. Both are ``None`` for a
    conversation with no messages.

    **Call-born conversations are excluded** (Spec V9, V9-D-3, acceptance #1):
    the filter is ``origin != 'call'`` — read STRICTLY from the ``conversations``
    marker column, with NO join to the call-record. That is the only-seam
    discipline: the chat list knows only the birth-marker, never voice/call
    state. A call-only session (``origin='call'``, empty title) therefore never
    pollutes the chat list as an empty "Untitled conversation"; a chat that was
    later *called* keeps its immutable ``origin='chat'`` and so STAYS in the chat
    list (it surfaces in the Calls surface independently, via the call-record).

    The latest message per conversation is resolved WITHOUT an N+1 fan-out: a
    single ``ROW_NUMBER() OVER (PARTITION BY conversation_id ORDER BY
    created_at DESC)`` window picks the newest ``messages`` row per
    conversation, and that subquery LEFT-JOINs onto the paginated
    conversations (so a conversation with zero messages still appears, with
    NULL preview fields). The whole statement runs in the one RLS-scoped
    transaction, so the ``messages`` rows it reads are constrained to the
    caller's tenant exactly like the conversations are — no cross-tenant leak.
    """
    # The "latest message per conversation" subquery: rank messages newest-first
    # within each conversation and keep only rank 1. ``created_at`` ties are
    # broken by ``id`` so the pick is deterministic.
    #
    # R9-025 leg C: excludes SUPERSEDED rows up front (``superseded_at IS NULL``)
    # — belt-and-braces defensive correctness. A regenerate/edit's replacement row
    # always carries a LATER ``created_at`` than the row it superseded, so rank-1
    # naturally lands on the replacement even without this filter in the steady
    # state; the filter only matters for the narrow transient window between the
    # supersede UPDATE committing and the replacement INSERT committing (they are
    # two separate statements, not one transaction, in ``regenerate_turn`` /
    # ``edit_and_rerun_turn``) — without it, a preview fetched in that exact
    # instant would show the now-hidden old reply instead of falling back to the
    # still-visible preceding message.
    ranked = (
        select(
            messages_t.c.conversation_id.label("conversation_id"),
            messages_t.c.content.label("content"),
            messages_t.c.role.label("role"),
            over(
                func.row_number(),
                partition_by=messages_t.c.conversation_id,
                order_by=(messages_t.c.created_at.desc(), messages_t.c.id.desc()),
            ).label("rn"),
        )
        .where(messages_t.c.superseded_at.is_(None))
        .subquery("ranked_messages")
    )
    latest = select(ranked).where(ranked.c.rn == 1).subquery("latest_message")

    with rls_engine.begin() as conn:
        rows = (
            conn.execute(
                select(
                    conversations_t,
                    latest.c.content.label("last_message_content"),
                    latest.c.role.label("last_message_role"),
                )
                .select_from(
                    conversations_t.join(
                        latest,
                        conversations_t.c.id == latest.c.conversation_id,
                        isouter=True,
                    )
                )
                # V9-D-3: exclude call-born conversations — read ONLY the marker,
                # no join to the call-record (the only-seam line). The chat list
                # never inspects voice/call state.
                .where(conversations_t.c.origin != "call")
                .order_by(conversations_t.c.updated_at.desc())
                .limit(limit)
                .offset(offset)
            )
            .mappings()
            .all()
        )
    out: list[dict[str, object]] = []
    for r in rows:
        row = dict(r)
        # Collapse the raw body to the truncated preview at the service boundary
        # so the route/response never sees a full message body.
        row["last_message_preview"] = _truncate_preview(
            cast("str | None", row.pop("last_message_content"))
        )
        out.append(row)
    return out


def _truncate_preview(content: str | None) -> str | None:
    """Trim + truncate a message body to the sidebar preview cap.

    Returns ``None`` for a missing body (a conversation with no messages).
    Collapses surrounding whitespace, then truncates to
    :data:`LAST_MESSAGE_PREVIEW_MAX_LEN` characters with a trailing ellipsis
    when the trimmed text overflows.
    """
    if content is None:
        return None
    trimmed = content.strip()
    if len(trimmed) <= LAST_MESSAGE_PREVIEW_MAX_LEN:
        return trimmed
    return trimmed[: LAST_MESSAGE_PREVIEW_MAX_LEN - 1].rstrip() + "…"


def get_active_turn(*, rls_engine: Engine, conversation_id: str) -> dict[str, object] | None:
    """Return the in-progress (streaming) assistant message for a conversation, or None.

    The reattach seed (Spec P1, D-P1-reattach-frontend): the live turn's assistant
    row — ``content`` (accumulated partial) + ``stream_events`` (the tool/text
    interleave checkpoint) + ``streaming_status``. RLS-scoped, so a conversation
    that isn't the caller's yields ``None`` (the route pre-checks ownership for a
    clean conversation-404). ``None`` when no turn is running (all messages
    terminal). The partial-unique index guarantees at most one ``running`` row.
    """
    with rls_engine.begin() as conn:
        row = (
            conn.execute(
                select(messages_t).where(
                    messages_t.c.conversation_id == conversation_id,
                    messages_t.c.streaming_status == "running",
                )
            )
            .mappings()
            .first()
        )
    return dict(row) if row is not None else None


def get_conversation(*, rls_engine: Engine, conversation_id: str) -> dict[str, object]:
    """Return a conversation + its full message history (RLS-scoped → 404).

    R9-025 leg C: excludes SUPERSEDED rows (``superseded_at IS NOT NULL``) — the
    web listing half of the supersede contract (:func:`_load_conversation` is the
    model-context half). A regenerated-away reply or an edited-away user message
    is never returned here, so the client renders the replacement in the SAME
    position rather than showing both (VISUALLY replaced, not duplicated).
    """
    with rls_engine.begin() as conn:
        conv = (
            conn.execute(select(conversations_t).where(conversations_t.c.id == conversation_id))
            .mappings()
            .first()
        )
        if conv is None:
            raise ConversationNotFoundError(
                "conversation not found", context={"id": conversation_id}
            )
        msgs = (
            conn.execute(
                select(messages_t)
                .where(
                    messages_t.c.conversation_id == conversation_id,
                    messages_t.c.superseded_at.is_(None),
                )
                .order_by(messages_t.c.created_at.asc())
            )
            .mappings()
            .all()
        )
    out = dict(conv)
    out["messages"] = [dict(m) for m in msgs]
    return out


def _load_conversation(conn: Connection, conversation_id: str) -> Conversation:
    """Materialise a runtime Conversation from the DB rows (RLS-scoped).

    R9-025 leg C: excludes SUPERSEDED rows (``superseded_at IS NOT NULL``) — a
    regenerated-away assistant reply or an edited-away user message must never
    re-enter a future model prompt. This is the context-rebuild half of the
    supersede contract; :func:`get_conversation` is the matching web-listing half.
    """
    conv = (
        conn.execute(select(conversations_t).where(conversations_t.c.id == conversation_id))
        .mappings()
        .first()
    )
    if conv is None:
        raise ConversationNotFoundError("conversation not found", context={"id": conversation_id})
    msgs = (
        conn.execute(
            select(messages_t)
            .where(
                messages_t.c.conversation_id == conversation_id,
                messages_t.c.superseded_at.is_(None),
            )
            .order_by(messages_t.c.created_at.asc())
        )
        .mappings()
        .all()
    )
    return Conversation(
        conversation_id=str(conv["id"]),
        persona_id=str(conv["persona_id"]),
        messages=[_to_message(dict(m)) for m in msgs],
        compacted_summary=str(conv["compacted_summary"]),
        compacted_up_to=int(conv["compacted_up_to"]),
    )


def _to_message(row: dict[str, object]) -> ConversationMessage:
    """Build a ConversationMessage from a DB row. ``role`` is constrained to the
    valid set by the ``messages_role_check`` DB CHECK, so the cast is sound."""
    role = cast("Role", str(row["role"]))
    # aware_utc: community SQLite returns naive instants (R4-C1-8) — the frozen
    # model rejects them, which 422'd every send in any conversation with history.
    created_at = cast("datetime", aware_utc(cast("datetime", row["created_at"])))
    # Rehydrate the runtime metadata the finalize persisted into channel["runtime_metadata"]
    # (the R4 rail-escape fix): without this, every pending rail (contract_proposal /
    # cancel_proposal / reschedule_proposal / proactive_question) died at the turn boundary.
    # Legacy / connector / A9-delegation channel shapes degrade to {} (fail-soft).
    metadata = metadata_from_channel(row.get("channel"))
    return ConversationMessage(
        role=role, content=str(row["content"]), created_at=created_at, metadata=metadata
    )


def _sse(event: str, data: dict[str, object]) -> bytes:
    """Format one SSE event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


# R9-022: the partial unique index that backstops D-P1-one-active-turn
# (``db/models.py``'s ``uq_messages_one_streaming_per_conversation``). A true
# concurrent race — two requests both pass the registry check (+ the R9-022
# heal) before either persists — surfaces here as an ``IntegrityError`` from
# the ``open_turn`` INSERT; :func:`_is_one_active_turn_race` recognises it so
# it can be remapped to the SAME 409 the registry's early check produces,
# never a raw 500.
#
# Matched by substring in the driver's error text rather than
# ``exc.orig.diag.constraint_name`` (the ``ApprovalStore`` precedent) because
# that attribute is psycopg-only: Postgres's message quotes the index name
# verbatim, but SQLite (community) reports the conflicting column instead —
# this is the ONLY unique index on ``messages`` over ``conversation_id``, so
# the column-shaped fallback is an unambiguous match on that dialect too.
_ONE_ACTIVE_TURN_INDEX = "uq_messages_one_streaming_per_conversation"
_ONE_ACTIVE_TURN_SQLITE_TEXT = "UNIQUE constraint failed: messages.conversation_id"


def _is_one_active_turn_race(exc: IntegrityError) -> bool:
    """True iff ``exc`` is the one-active-turn partial-unique violation (either dialect)."""
    text = str(exc)
    return _ONE_ACTIVE_TURN_INDEX in text or _ONE_ACTIVE_TURN_SQLITE_TEXT in text


async def start_chat_turn(
    *,
    rls_engine: Engine,
    sink: MessagesTurnSink,
    registry: ChatTurnRegistry,
    loop_builder: LoopBuilder,
    owner_id: str,
    conversation_id: str,
    user_message: str | None = None,
    channel: ChannelContext | None = None,
    title_builder: Callable[[str], Awaitable[str]] | None = None,
    images: list[ImageRefSchema] | None = None,
    turn_has_image: bool = False,
    document_context: DocumentContext | None = None,
    workspace_root: Path | None = None,
    max_concurrent_long_ops: int = 0,
    file_storage: FileStorage | None = None,
    event_channel: UserEventChannel | None = None,
    persist_user_message: bool = True,
    edited_from: str | None = None,
) -> ChatTurnHandle:
    """Persist the turn at START + launch it detached; return the live handle (P1, T2b).

    The chat turn is now a **persistent, resumable session** (D-P1-detached-execution):

    1. Reject early if a turn is already streaming for this conversation (→ 409,
       block-don't-queue, D-P1-one-active-turn) — BEFORE any DB write, so the
       partial-unique index never has to fire.
    2. R9-022 lazy self-heal: step 1 only proves the in-process registry has no
       active turn for this conversation — a ``running`` row can still be a
       crash orphan (its owning process died mid-turn; D-P1-restart-sweep's
       startup sweep is the between-PROCESS backstop, this is the
       between-restarts one, applied the moment the conversation is next
       used). Heal it to ``interrupted`` here, before the next persist, so it
       never blocks a legitimate new turn.
    3. Persist the user message + an in-progress assistant row (``open_turn``),
       so a reload mid-turn refetches both (acceptance #2). A residual race
       here (another request truly won the turn between steps 1-2 and this
       INSERT) surfaces as the same partial-unique violation the DB
       backstops — remapped to the SAME 409 as step 1, never a raw 500
       (R9-022).
    4. Resolve the turn's images / documents (request scope — needs
       ``workspace_root``) and build the loop, exactly as the inline path did.
    5. Launch the detached task via the registry; a client disconnect no longer
       cancels it. The worker drives the loop, checkpoints, finalizes, and bills
       on clean completion (D-P1-billing-contract); the request streams the live
       tail via :func:`stream_turn`.

    Raises :class:`~persona_api.errors.TurnAlreadyActiveError` (→ 409 — the
    already-active check in step 1 OR the residual-race remap in step 3),
    :class:`~persona_api.errors.ConversationNotFoundError` (→ 404), etc. cleanly
    BEFORE the SSE response starts — never mid-stream.

    R9-025 leg C — ``persist_user_message`` / ``edited_from`` (both default to the
    byte-identical normal-send shape; every existing caller is unaffected):

    ``persist_user_message=False`` is :func:`regenerate_turn`'s mode: the caller has
    already superseded the target assistant reply, so the freshly-loaded history's
    LAST message is the preceding user turn being re-answered, not new content to
    persist a second time. This pops that trailing user message off the loaded
    history (so it is not double-counted as both "history" and "the new turn") and
    feeds its content as ``user_message`` — mirroring exactly what a normal send's
    load-before-persist ordering already produces, so the loop/billing/telemetry/
    title-refresh math downstream is untouched. ``user_message`` MUST be ``None`` in
    this mode (it is derived, never passed) — a defensive ``ValueError`` otherwise.

    ``edited_from`` is :func:`edit_and_rerun_turn`'s marker: when set, the NEW user
    row's ``channel`` carries ``{"edited_from": <old_message_id>}`` instead of
    ``channel``'s value (always ``None`` on the edit path — v1 edit is text-only).
    Threaded straight through to :meth:`MessagesTurnSink.open_turn`.
    """
    if registry.get(conversation_id) is not None:
        from persona_api.errors import TurnAlreadyActiveError  # noqa: PLC0415

        raise TurnAlreadyActiveError(
            "a turn is already running for this conversation",
            context={"conversation_id": conversation_id},
        )

    # R9-022 lazy self-heal: the registry (the in-process liveness authority) has
    # just proven there is no active turn for this conversation, but an unclean
    # end (a process crash/restart mid-turn, or an out-of-band cancellation that
    # skips ``finalize`` — see ``chat_turn_worker``'s shutdown-cancel path) can
    # still leave a ``running`` assistant row behind. The startup sweep
    # (``restart_sweep.py``, D-P1-restart-sweep) heals the same class of row on
    # the NEXT process boot, but a long-lived process can sit on the orphan far
    # longer than that — heal it lazily, right here, so the very next turn on
    # this conversation is never blocked by a row the registry itself proves is
    # dead. Ordering is load-bearing: registry-check (above) → heal (here) →
    # the ``open_turn`` INSERT (below) — never the other way round.
    healed_id = sink.heal_orphaned_running(conversation_id=conversation_id)
    if healed_id is not None:
        _log.warning(
            "chat turn self-heal: conversation {cid} had an orphaned 'running' "
            "assistant message {mid} with no live in-process turn — healed to "
            "'interrupted' so the new turn can proceed (R9-022)",
            cid=conversation_id,
            mid=healed_id,
        )

    # Spec R7 (R7-D-4): reserve the durable per-user long-op slot BEFORE any persist,
    # so an over-cap user gets a clean 429 before a message is written (a post-persist
    # refusal would orphan a ``running`` row and wedge the one-active-turn guard). The
    # slot is released in the worker's terminal ``finally`` (all paths); if any setup
    # step below raises BEFORE the turn is handed off to the worker, we release here so
    # the slot never leaks. ``max_concurrent_long_ops <= 0`` (community/uncapped) is a
    # no-op that always admits (no DB write). This is the parallel-spend race the
    # per-conversation one-active guard can't close — a user with N conversations could
    # otherwise start N concurrent turns.
    from persona.concurrency import admit_long_op, release_long_op  # noqa: PLC0415

    op_token = admit_long_op(
        rls_engine=rls_engine,
        user_id=owner_id,
        op_class="chat",
        max_concurrent=max_concurrent_long_ops,
    )
    if op_token is None:
        from persona_api.errors import ConcurrencyCappedError  # noqa: PLC0415

        raise ConcurrencyCappedError(
            "too many concurrent chat turns in flight for this user",
            context={"user_id": owner_id, "retry_after_s": "5"},
        )
    try:
        with rls_engine.begin() as conn:
            conversation = _load_conversation(conn, conversation_id)
            prior_msg_count = len(conversation.messages)
        persona_id = conversation.persona_id
        # R9-025 leg C (regenerate): the "current" turn's user content already
        # exists as a persisted row — the caller (regenerate_turn) has already
        # superseded its old reply, so it is the LAST message
        # ``_load_conversation`` just loaded. Pop it off so it is fed as THIS
        # turn's ``user_message`` (context ends exactly at it) instead of being
        # double-counted as both history AND the new turn.
        if not persist_user_message:
            if not conversation.messages or conversation.messages[-1].role != "user":
                # Defensive only — regenerate_turn's own tail-eligibility check
                # (_tail_target) already guarantees this before it ever calls in.
                raise PersonaError(
                    "start_chat_turn: persist_user_message=False requires a "
                    "preceding user message in the loaded history",
                    context={"conversation_id": conversation_id},
                )
            tail = conversation.messages.pop()
            tail_content = tail.content
            if not isinstance(tail_content, str):
                # Defensive only — ``_load_conversation``'s ``_to_message`` always
                # builds ``ConversationMessage.content`` as a plain ``str`` from the
                # DB's TEXT column (``str(row["content"])``), never the
                # list-of-typed-parts multimodal shape a FRESH send can carry
                # in-memory. This can only trip if that contract ever changes.
                raise PersonaError(
                    "start_chat_turn: persist_user_message=False requires plain-text "
                    "preceding user content",
                    context={"conversation_id": conversation_id},
                )
            user_message = tail_content
            prior_msg_count -= 1
        elif user_message is None:
            raise ValueError(
                "start_chat_turn: user_message is required when persist_user_message=True"
            )
        # A plain local (not just an `assert`) so the narrowing survives into the
        # `on_complete` closure below — mypy does not carry `assert`-narrowing of an
        # outer-scope variable into a nested function's body.
        assert user_message is not None  # narrowed by the branches above
        resolved_message: str = user_message
        is_first_turn = prior_msg_count == 0

        # Resolve images/documents + stage docs for the host file tools (request
        # scope — needs workspace_root). The detached worker binds the sandbox
        # contextvar for code_execution; these resolve bytes by path, no contextvar.
        turn_images = _resolve_turn_images(
            file_storage=file_storage, owner_id=owner_id, persona_id=persona_id, images=images
        )
        turn_documents = _resolve_turn_documents(
            file_storage=file_storage,
            owner_id=owner_id,
            persona_id=persona_id,
            conversation_id=conversation_id,
        )
        _stage_documents_for_file_read(
            workspace_root=workspace_root,
            owner_id=owner_id,
            persona_id=persona_id,
            documents=turn_documents,
        )

        # Spec P4-D-3 — bind the request's sandbox context for the DURATION of the loop
        # build only (tight set→try→reset). The builtin ``filesystem`` MCP subprocess is
        # scoped at SPAWN, which happens inside this build; the supervisor resolves its
        # scoped root from this contextvar (same source as the in-process file tools).
        # The detached worker re-binds the SAME (owner, conversation) context for turn
        # execution (chat_turn_worker), so spawn-time and turn-time scope agree.
        # Contextvars don't propagate into the worker's task, so this bind cannot leak
        # past the build; unbound at build ⇒ the child fails closed (serve-and-deny).
        from persona_api.sandbox import (  # noqa: PLC0415
            SandboxRequestContext,
            reset_sandbox_request_context,
            set_sandbox_request_context,
        )

        _scope_token = set_sandbox_request_context(
            SandboxRequestContext(owner_id=owner_id, conversation_id=conversation_id)
        )
        try:
            loop = await loop_builder(persona_id)
        finally:
            reset_sandbox_request_context(_scope_token)
        try:
            assistant_message_id = sink.open_turn(
                conversation_id=conversation_id,
                user_message=resolved_message,
                channel=channel,
                images=images,
                persist_user_message=persist_user_message,
                edited_from=edited_from,
            )
        except IntegrityError as exc:
            # R9-022: a true concurrent race — another request's turn opened for
            # this SAME conversation in the gap between the registry-check/heal
            # above and this INSERT (the partial unique index,
            # D-P1-one-active-turn, is the final backstop). Map it to the SAME
            # 409 shape the registry's early check produces — never a raw 500.
            # Any OTHER integrity error (unrelated to this constraint) is a real
            # bug and propagates unchanged.
            if not _is_one_active_turn_race(exc):
                raise
            from persona_api.errors import TurnAlreadyActiveError  # noqa: PLC0415

            raise TurnAlreadyActiveError(
                "a turn is already running for this conversation",
                context={"conversation_id": conversation_id},
            ) from exc

        # Auto-title the first turn from its first user message (best-effort, title
        # tier — R9-020) on the detached completion path — never delays / breaks the
        # turn. A successful write publishes sidebar.changed so open tabs update live.
        on_complete: Callable[[], Awaitable[None]] | None = None
        if is_first_turn and title_builder is not None:
            _title_builder = title_builder

            async def on_complete() -> None:
                await _maybe_set_title(
                    rls_engine,
                    conversation_id,
                    resolved_message,
                    _title_builder,
                    owner_id=owner_id,
                    event_channel=event_channel,
                )

        handle = registry.start(
            conversation_id=conversation_id,
            owner_id=owner_id,
            assistant_message_id=assistant_message_id,
            loop=loop,
            conversation=conversation,
            user_message=resolved_message,
            on_complete=on_complete,
            op_token=op_token,
            turn_has_image=turn_has_image,
            images=turn_images or None,
            documents=turn_documents or None,
            document_context=document_context,
        )
    except BaseException:
        # Any failure BEFORE the worker took ownership (load / resolve / build /
        # open_turn / a one-active re-check in ``registry.start``) → release the slot
        # here so a failed setup never leaks it (the worker's ``finally`` only runs
        # once the turn is handed off). On success the worker owns the release.
        release_long_op(rls_engine=rls_engine, user_id=owner_id, op_id=op_token)
        raise
    return handle


# -- R9-025 leg C: regenerate + edit-and-rerun (real retry, not an echo-resend) ----
#
# **V1 scope (deliberate):** regenerate and edit apply to the CONVERSATION TAIL
# only — the last assistant message (regenerate) and the last user message (edit).
# No branching of older history; that is a future feature with tree semantics.
# ``_tail_target`` is the ONE place the eligibility rule lives, shared by both.


def _tail_target(
    messages: list[dict[str, object]], *, message_id: str, role: str
) -> tuple[dict[str, object] | None, dict[str, object] | None, str | None]:
    """Resolve a regenerate/edit target against the v1 conversation-TAIL rule.

    ``messages`` is the conversation's FULL history (oldest-first, every role,
    every ``superseded_at`` state — unlike :func:`_load_conversation` /
    :func:`get_conversation`, which already filter superseded rows out; this
    needs to see everything to tell "doesn't exist" apart from "already
    superseded"). Returns ``(target, trailing_reply, reason)``:

    - On success, ``target`` is the row to supersede. ``trailing_reply`` is its
      immediate assistant reply (also to be superseded) when ``role == "user"``
      and the LAST visible message is that reply — the normal completed-turn
      shape; ``None`` otherwise (a lone tail user message with no reply yet —
      e.g. an A6 chat-twin decision reply — or the ``role == "assistant"`` case,
      which never has "a reply to its reply"). ``reason`` is ``None``.
    - On failure, ``target`` is ``None`` and ``reason`` names why:
      ``"not_found"`` (no message with this id in the conversation at all),
      ``"already_superseded"`` (a duplicate click or a race with a concurrent
      regenerate/edit), or ``"not_last_assistant_message"`` /
      ``"not_last_user_message"`` (exists, right role, but an OLDER turn —
      outside v1's tail-only scope). The caller (``regenerate_turn`` /
      ``edit_and_rerun_turn``) maps EVERY failure reason to the SAME 422
      (``TurnTargetInvalidError``) — a blanket "not currently a valid
      regenerate/edit target," never a separate 404 for the not-found case
      (deliberately simpler than ``turn_into_file``'s split; nothing here reads
      ``context.reason`` to branch UI behaviour differently for "doesn't exist"
      vs "exists but stale").
    """
    exists = any(m["id"] == message_id for m in messages)
    if not exists:
        return None, None, "not_found"
    visible = [m for m in messages if m.get("superseded_at") is None]
    still_visible = next((m for m in visible if m["id"] == message_id), None)
    if role == "assistant":
        if visible and visible[-1]["id"] == message_id and visible[-1]["role"] == "assistant":
            return visible[-1], None, None
        reason = "already_superseded" if still_visible is None else "not_last_assistant_message"
        return None, None, reason
    if role == "user":
        if visible and visible[-1]["id"] == message_id and visible[-1]["role"] == "user":
            return visible[-1], None, None
        if (
            len(visible) >= 2
            and visible[-1]["role"] == "assistant"
            and visible[-2]["id"] == message_id
            and visible[-2]["role"] == "user"
        ):
            return visible[-2], visible[-1], None
        reason = "already_superseded" if still_visible is None else "not_last_user_message"
        return None, None, reason
    return None, None, "not_found"


def _all_messages_including_superseded(
    *, rls_engine: Engine, conversation_id: str
) -> list[dict[str, object]]:
    """Full message history (oldest-first, EVERY ``superseded_at`` state) — the
    tail-eligibility read for :func:`regenerate_turn` / :func:`edit_and_rerun_turn`
    (see :func:`_tail_target`'s docstring for why superseded rows must stay visible
    to this one read)."""
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(
                select(messages_t)
                .where(messages_t.c.conversation_id == conversation_id)
                .order_by(messages_t.c.created_at.asc(), messages_t.c.id.asc())
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


async def regenerate_turn(
    *,
    rls_engine: Engine,
    sink: MessagesTurnSink,
    registry: ChatTurnRegistry,
    loop_builder: LoopBuilder,
    owner_id: str,
    conversation_id: str,
    assistant_message_id: str,
    title_builder: Callable[[str], Awaitable[str]] | None = None,
    workspace_root: Path | None = None,
    max_concurrent_long_ops: int = 0,
    file_storage: FileStorage | None = None,
    event_channel: UserEventChannel | None = None,
) -> ChatTurnHandle:
    """Regenerate the LAST assistant reply — R9-025 leg C ("a REAL retry", not an
    echo-resend of the preceding user message as a brand-new turn).

    The shipped wave-2a "retry" client-side re-sent the preceding user message as a
    NEW turn — the model then saw its OWN just-superseded reply still sitting in
    context (nothing was ever excluded) and produced a confused answer (the owner's
    operator-pass verdict). This fixes it at the root:

    1. **409** (:class:`~persona_api.errors.TurnAlreadyActiveError`) if a turn is
       already streaming for this conversation — the SAME registry check
       :func:`start_chat_turn` opens with, checked here FIRST, before any write, so
       a blocked regenerate never supersedes a reply with nothing queued to
       replace it.
    2. **422** (:class:`~persona_api.errors.TurnTargetInvalidError`) unless
       ``assistant_message_id`` is the conversation's CURRENT last assistant
       message (see :func:`_tail_target` — v1 tail-only scope).
    3. **Supersede** the target (``MessagesTurnSink.supersede_message`` —
       migration 047's ``superseded_at``): excluded from every future model
       prompt (:func:`_load_conversation`'s filter) AND the web message listing
       (:func:`get_conversation`'s filter) from this instant on. The row is
       NEVER deleted or content-mutated (the additive invariant) — a future
       tree/branch-history feature can still read it.
    4. **Start a REAL turn** via :func:`start_chat_turn` with
       ``persist_user_message=False`` — the preceding user message is NOT
       re-inserted (it already exists); its content is popped off the
       freshly-(re)loaded history and fed as the turn's ``user_message``, so
       the model's context ends EXACTLY at that user turn — the superseded
       reply is provably absent from the prompt, not just visually hidden.
       Billing / telemetry / title-refresh ride the exact same worker path a
       normal send does (M2 bills the new turn — normal; a regenerate never
       grows the VISIBLE message count, so title-refresh's threshold-crossing
       math is unaffected, matching a normal send at the same history depth
       — see the inline note in :func:`start_chat_turn`).

    Composes with the R9-022 self-heal for free: if the current tail happens to be
    an orphaned ``running`` row (a crashed process's leftover — the registry has NO
    entry for it, so step 1 passes), step 4's :func:`start_chat_turn` heals it to
    ``interrupted`` (its existing, proven lazy-heal step) before launching the new
    turn — regenerate never needs its own special case for that.

    V1 scope (deliberate — see the module-level note above): the target must be
    the CURRENT tail. No regenerating an older turn (a future tree/branch feature).

    Documents / images are NOT re-attached this turn (``document_context=None``,
    ``images=None``) — a deliberate v1 scope trim (matches the pre-existing
    system-wide behaviour that historical image attachments are never re-fed to
    later turns either — ``_to_message`` never carries ``images`` into the runtime
    ``Conversation``). A follow-up can wire document-context reconstruction if a
    regenerated reply on a document-attached conversation is found to need it.
    """
    from persona_api.errors import TurnAlreadyActiveError, TurnTargetInvalidError  # noqa: PLC0415

    if registry.get(conversation_id) is not None:
        raise TurnAlreadyActiveError(
            "a turn is already running for this conversation",
            context={"conversation_id": conversation_id},
        )

    messages = _all_messages_including_superseded(
        rls_engine=rls_engine, conversation_id=conversation_id
    )
    target, _trailing, reason = _tail_target(
        messages, message_id=assistant_message_id, role="assistant"
    )
    if target is None:
        raise TurnTargetInvalidError(
            "message is not the conversation's current last assistant reply",
            context={
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "reason": reason or "not_found",
            },
        )

    if not sink.supersede_message(message_id=assistant_message_id):
        # A concurrent request superseded it in the narrow gap between the read
        # above and here — the same residual-race class start_chat_turn's own
        # docstring documents for the one-active-turn index. Treat identically:
        # "no longer a valid target," never a raw failure.
        raise TurnTargetInvalidError(
            "message was already superseded by a concurrent request",
            context={
                "conversation_id": conversation_id,
                "message_id": assistant_message_id,
                "reason": "already_superseded",
            },
        )

    return await start_chat_turn(
        rls_engine=rls_engine,
        sink=sink,
        registry=registry,
        loop_builder=loop_builder,
        owner_id=owner_id,
        conversation_id=conversation_id,
        persist_user_message=False,
        title_builder=title_builder,
        workspace_root=workspace_root,
        max_concurrent_long_ops=max_concurrent_long_ops,
        file_storage=file_storage,
        event_channel=event_channel,
    )


async def edit_and_rerun_turn(
    *,
    rls_engine: Engine,
    sink: MessagesTurnSink,
    registry: ChatTurnRegistry,
    loop_builder: LoopBuilder,
    owner_id: str,
    conversation_id: str,
    user_message_id: str,
    new_content: str,
    title_builder: Callable[[str], Awaitable[str]] | None = None,
    workspace_root: Path | None = None,
    max_concurrent_long_ops: int = 0,
    file_storage: FileStorage | None = None,
    event_channel: UserEventChannel | None = None,
) -> ChatTurnHandle:
    """Edit the LAST user message's content and immediately re-run — R9-025 leg C.

    **Representation decision: supersede + a brand-new row — NOT an in-place
    content UPDATE.** Argued here (the kickoff left this open):

    - Symmetric with :func:`regenerate_turn`, whose new reply is ALWAYS a new row
      — one mental model for "the tail changed" across both actions, and one
      shared :func:`_tail_target` eligibility rule.
    - Honours the additive invariant this whole feature is built on: existing
      rows are untouched. An in-place UPDATE would DESTROY the original wording
      the instant the edit lands — with supersede+new-row it merely stops being
      *read*, so a future tree/branch-history feature (explicitly out of v1
      scope) could still recover it. "Keeps history honest."
    - Keeps the UI simple the SAME way the assistant side already does: the web
      listing (:func:`get_conversation`) just stops returning the old pair and
      starts returning the new one — no separate "this row was mutated,
      re-render its content" case to handle, no diff/history UI to build for v1.
    - The new row's ``channel`` carries ``{"edited_from": <old_message_id>}`` — a
      durable, queryable marker distinguishing an edited turn from an ordinary
      send (see :meth:`MessagesTurnSink.open_turn`'s ``edited_from`` param).
      Harmless to every existing ``channel`` reader (they only look for their OWN
      namespaced key and ignore the rest — see
      ``message_metadata.metadata_from_channel``).

    Mechanics, mirroring :func:`regenerate_turn`:

    1. **409** / **422** exactly as :func:`regenerate_turn` (see its docstring) —
       except the v1 tail rule here is "the LAST user message," which
       :func:`_tail_target` resolves as either the true last message, or the
       second-to-last when the last is that user message's OWN assistant reply
       (the normal completed-turn shape — that reply is superseded too).
    2. **Supersede** the old user row, and its trailing reply when one exists.
    3. **Re-run via :func:`start_chat_turn` completely UNMODIFIED**
       (``persist_user_message`` stays at its default ``True``) — once the old
       pair is superseded, the freshly-loaded history naturally ends right
       before them, so sending ``new_content`` is BYTE-FOR-BYTE the same code
       path a normal :func:`start_chat_turn` call takes: a new user row + a new
       assistant row, billing/telemetry/title-refresh unchanged. Editing the
       conversation's FIRST user message correctly re-triggers the turn-1
       auto-title hook (``is_first_turn`` is keyed off the post-supersede
       history depth) — the old title was generated from the now-superseded
       wording, so refreshing it is exactly right, not a side effect to guard
       against.

    V1 scope: text-only (no ``images``/``document_context`` re-attachment — see
    :func:`regenerate_turn`'s matching note; the same trim, for the same reason).
    """
    from persona_api.errors import TurnAlreadyActiveError, TurnTargetInvalidError  # noqa: PLC0415

    if registry.get(conversation_id) is not None:
        raise TurnAlreadyActiveError(
            "a turn is already running for this conversation",
            context={"conversation_id": conversation_id},
        )

    messages = _all_messages_including_superseded(
        rls_engine=rls_engine, conversation_id=conversation_id
    )
    target, trailing_reply, reason = _tail_target(messages, message_id=user_message_id, role="user")
    if target is None:
        raise TurnTargetInvalidError(
            "message is not the conversation's current last user message",
            context={
                "conversation_id": conversation_id,
                "message_id": user_message_id,
                "reason": reason or "not_found",
            },
        )

    if not sink.supersede_message(message_id=user_message_id):
        raise TurnTargetInvalidError(
            "message was already superseded by a concurrent request",
            context={
                "conversation_id": conversation_id,
                "message_id": user_message_id,
                "reason": "already_superseded",
            },
        )
    if trailing_reply is not None:
        trailing_id = str(trailing_reply["id"])
        if not sink.supersede_message(message_id=trailing_id):
            # The reply was superseded by a concurrent regenerate/edit in the same
            # narrow gap — abort rather than leave a dangling old reply that would
            # otherwise sit BEFORE the freshly-inserted edited user row in
            # created_at order (a broken turn shape). The user row supersede above
            # already committed; the caller can simply retry the edit (its content
            # was never lost — it lives in the client's draft, not the DB).
            raise TurnTargetInvalidError(
                "the reply to this message was already superseded by a concurrent request",
                context={
                    "conversation_id": conversation_id,
                    "message_id": trailing_id,
                    "reason": "already_superseded",
                },
            )

    return await start_chat_turn(
        rls_engine=rls_engine,
        sink=sink,
        registry=registry,
        loop_builder=loop_builder,
        owner_id=owner_id,
        conversation_id=conversation_id,
        user_message=new_content,
        channel=None,
        edited_from=user_message_id,
        title_builder=title_builder,
        workspace_root=workspace_root,
        max_concurrent_long_ops=max_concurrent_long_ops,
        file_storage=file_storage,
        event_channel=event_channel,
    )


async def stream_turn(handle: ChatTurnHandle) -> AsyncIterator[bytes]:
    """Stream a detached turn's live tail as SSE frames (P1, T2b).

    Drains the handle's event queue — granular events + text chunks + the
    terminal ``done`` (clean completion) or ``error`` frame — translating each to
    an SSE frame in true emission order, exactly like the old inline stream. The
    SAME generator serves the originating POST and every reattach
    (``GET …/active-turn/events``, T4): a client disconnect just stops draining;
    the detached turn keeps running and is re-tailable on return.

    Note: the turn's persistence + billing happen in the worker, NOT here — so a
    disconnect mid-drain never loses the turn or skips the bill (the D-08-6
    revision; the inline path's persist-in-the-generator hazard is gone).
    """
    while True:
        item = await handle.events.get()
        if item is None:  # end-of-stream sentinel
            break
        kind, payload = item
        if kind == "event":
            ev = cast("RunEvent", payload)
            yield _sse(ev.type, ev.data)
        elif kind == "chunk":
            chunk = cast("StreamChunk", payload)
            if chunk.delta:
                yield _sse("chunk", {"delta": chunk.delta, "is_final": chunk.is_final})
        elif kind in ("done", "error"):
            yield _sse(kind, cast("dict[str, object]", payload))


def _resolve_turn_images(
    *,
    file_storage: FileStorage | None,
    owner_id: str,
    persona_id: str,
    images: list[ImageRefSchema] | None,
) -> list[TurnImage]:
    """Resolve inbound image refs to runtime :class:`TurnImage` carriers.

    Reads each uploaded image's bytes from the persona workspace via the
    existing :func:`persona_api.services.image_service.fetch` resolver (the same
    path that backs ``GET /uploads/{ref}``), so the loop can route them to both
    the model and the sandbox. The bytes live exactly once under
    ``workspace_root/owner_id/persona_id`` (Spec 13 D-13-X-now option c); this
    is read-only resolution, never a second persisted copy.

    Returns an empty list when there are no images or no ``workspace_root`` is
    configured (CLI / test paths) — the text-only path is unaffected. A
    ref that cannot be resolved (deleted/cross-tenant) is skipped with a
    WARNING rather than failing the whole turn: the persisted ``images`` JSONB
    is still written by :func:`_persist_turn`, and a partial-vision turn beats a
    hard 500 mid-stream.

    Args:
        workspace_root: The per-deployment workspace root (``app.state``).
        owner_id: Authenticated tenant id (RLS scope).
        persona_id: Persona owning the conversation + the uploads.
        images: Inbound image refs from the chat body (may be ``None``).

    Returns:
        Resolved :class:`TurnImage` carriers in caller order (possibly empty).
    """
    if not images or file_storage is None:
        return []
    # Local import: keeps the api-runtime import graph free of the runtime
    # package at module load + mirrors the lazy-import discipline elsewhere.
    from persona_runtime.images import TurnImage

    resolved: list[TurnImage] = []
    for ref in images:
        try:
            file_bytes, _media = image_service.fetch(
                file_storage=file_storage,
                owner_id=owner_id,
                persona_id=persona_id,
                ref=ref.workspace_path,
            )
        except PersonaError as exc:
            _log.warning(
                "uploaded image could not be resolved for the turn; skipping",
                workspace_path=ref.workspace_path,
                reason=str(exc),
            )
            continue
        resolved.append(
            TurnImage(
                workspace_path=ref.workspace_path,
                media_type=ref.media_type,
                content_bytes=file_bytes,
            )
        )
    return resolved


def _resolve_turn_documents(
    *,
    file_storage: FileStorage | None,
    owner_id: str,
    persona_id: str,
    conversation_id: str,
) -> list[SandboxFile]:
    """Resolve the conversation's attached documents to sandbox input files.

    Document-workspace cascade: uploaded NON-image documents reached the model
    only as a ``document_context`` synopsis; the sandbox ``file_read`` /
    ``code_execution`` tools never saw the actual file (so ``file_read`` could
    surface a stale, unrelated file). This stages each attached document's
    ORIGINAL bytes as a :class:`SandboxFile` under the sandbox input mount at
    ``uploads/<filename>`` so the runtime loop appends it to
    ``deferred_input_files`` and the tools can read THIS file.

    Bytes are read via the existing document-store resolver
    (:func:`document_service.read_document_bytes`) — no new storage. A document
    that cannot be read (missing/oversize) is skipped with a WARNING rather than
    failing the turn; the model still has the synopsis via ``document_context``.

    Args:
        workspace_root: The per-deployment workspace root (``app.state``).
        persona_id: Persona owning the conversation + the documents.
        conversation_id: Conversation scope (documents are conversation-scoped).

    Returns:
        Resolved :class:`SandboxFile` carriers in workspace order (possibly
        empty — no documents, or no ``workspace_root`` on the CLI/test path).
    """
    if file_storage is None:
        return []
    # Local import: keep the api module-load import graph free of the runtime/
    # core sandbox types (mirrors the lazy-import discipline above).
    from persona.sandbox.result import SandboxFile, guess_media_type  # noqa: PLC0415

    refs = document_service.list_for_conversation(
        file_storage=file_storage,
        owner_id=owner_id,
        persona_id=persona_id,
        conversation_id=conversation_id,
    )
    if not refs:
        return []

    resolved: list[SandboxFile] = []
    for ref in refs:
        file_bytes = document_service.read_document_bytes(
            file_storage=file_storage,
            owner_id=owner_id,
            persona_id=persona_id,
            conversation_id=conversation_id,
            doc_ref=ref.doc_ref,
        )
        if file_bytes is None:
            _log.warning(
                "uploaded document could not be resolved for the turn; skipping",
                doc_ref=ref.doc_ref,
                filename=ref.filename,
            )
            continue
        if len(file_bytes) > MAX_STAGED_DOCUMENT_BYTES:
            _log.warning(
                "uploaded document exceeds the sandbox staging cap; skipping",
                doc_ref=ref.doc_ref,
                size_bytes=len(file_bytes),
                max_bytes=MAX_STAGED_DOCUMENT_BYTES,
            )
            continue
        # Stage at a predictable, model-readable ``uploads/<filename>`` path so a
        # sandbox ``file_read("uploads/<filename>")`` finds THIS document.
        resolved.append(
            SandboxFile(
                path=f"uploads/{ref.filename}",
                content_bytes=file_bytes,
                size_bytes=len(file_bytes),
                media_type=guess_media_type(ref.filename),
            )
        )
    return resolved


def _stage_documents_for_file_read(
    *,
    workspace_root: Path | None,
    owner_id: str,
    persona_id: str,
    documents: list[SandboxFile],
) -> None:
    """Mirror staged documents into the HOST-side ``file_read`` scoped root.

    The ``code_execution`` tool reads the documents staged onto
    ``deferred_input_files`` because the runtime ships those bytes into the
    REMOTE sandbox's working directory (``/home/user/uploads/<name>`` on the
    hosted E2B substrate — relative ``uploads/<name>`` from CWD). The built-in
    ``file_read`` tool, by contrast, reads the LOCAL filesystem under its
    per-request scoped root ``<workspace_root>/<owner_id>/<persona_id>``
    (:func:`persona_api.services.runtime_factory.RuntimeFactory._build_file_sandbox_root_provider`)
    — a *different* subtree from the conversation-scoped document store. Without
    this mirror, ``file_read("uploads/<name>")`` resolves to a path that does
    not exist and returns ``FileNotFoundError``.

    This stages each document's bytes to
    ``<workspace_root>/<owner_id>/<persona_id>/uploads/<filename>`` so a
    ``file_read("uploads/<filename>")`` finds THIS conversation's uploaded
    document at the SAME relative path ``code_execution`` uses. The result is a
    single coherent path model — ``uploads/<filename>`` — across both tools.

    Isolation invariant (do NOT regress the just-landed security scoping): the
    target is resolved THROUGH :func:`resolve_sandbox_path` against the same
    per-(owner, persona) root file_read reads, so a pathological filename cannot
    escape the persona's subtree, and persona A never gains a path into persona
    B's (or another owner's) files. Only the CURRENT request's owner/persona
    root is ever written, and only the CURRENT conversation's attached
    documents (the ``documents`` already resolved for this turn).

    Best-effort: a write failure is logged and skipped (the model still has the
    ``document_context`` synopsis + the ``code_execution`` copy); a partial
    staging beats a hard turn failure.

    Args:
        workspace_root: The per-deployment workspace root (``app.state``). When
            ``None`` (CLI / test path) the file tools have no scoped provider
            anyway, so staging is a no-op.
        owner_id: Authenticated tenant id — the first scope segment.
        persona_id: Persona owning the conversation — the second scope segment.
        documents: The :class:`SandboxFile` carriers already resolved for this
            turn by :func:`_resolve_turn_documents` (path ``uploads/<filename>``,
            bytes in ``content_bytes``).
    """
    if workspace_root is None or not documents:
        return
    # Local import: keep the api module-load import graph free of the core
    # sandbox resolver (mirrors the lazy-import discipline elsewhere here).
    from persona.errors import SandboxViolationError  # noqa: PLC0415
    from persona.tools._sandbox import (  # noqa: PLC0415
        resolve_sandbox_path,
        write_nofollow_bytes,
    )

    # The EXACT root the file_read provider resolves at dispatch time
    # (runtime_factory._build_file_sandbox_root_provider). Keeping the two in
    # lockstep is the contract that makes ``file_read("uploads/<name>")`` work.
    file_read_root = workspace_root / owner_id / persona_id
    for sf in documents:
        if sf.content_bytes is None:
            continue
        try:
            target = resolve_sandbox_path(file_read_root, sf.path)
        except SandboxViolationError:
            _log.warning(
                "document path escapes the file_read scoped root; not mirrored",
                path=sf.path,
            )
            continue
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            # R2 F-03: write via the O_NOFOLLOW opener so a symlink swapped into the
            # final component cannot redirect the mirror write outside the sandbox
            # (a swapped link raises OSError → skip, like any other write failure).
            write_nofollow_bytes(target, sf.content_bytes)
        except OSError as exc:
            _log.warning(
                "could not mirror document into the file_read root; skipping",
                path=sf.path,
                exc_type=type(exc).__name__,
            )


async def _maybe_set_title(
    rls_engine: Engine,
    conversation_id: str,
    first_message: str,
    title_builder: Callable[[str], Awaitable[str]],
    *,
    owner_id: str | None = None,
    event_channel: UserEventChannel | None = None,
) -> None:
    """Generate + persist a short title from the first message. Best-effort: any
    failure (model error, timeout) is logged and swallowed — the conversation
    keeps its default title rather than breaking the turn.

    R9-020: a successful title write publishes ``sidebar.changed``
    (reason=``conversation.title_updated``) post-commit, so the owner's open
    tabs pick the new title up live (the R9-012 channel; best-effort, no
    channel / no tab → the sidebar catches up on its next refetch).
    """
    try:
        raw = await title_builder(first_message)
        # R4 T3: sanitise the model output (reject an instruction echo, strip
        # quotes/punctuation, cap words, fall back to the user's words) so the
        # stored title is a real title, never the titling prompt.
        title = sanitize_conversation_title(raw, first_message=first_message)
        if title:
            set_title(
                rls_engine=rls_engine, conversation_id=conversation_id, title=title[:_MAX_TITLE_LEN]
            )
            if owner_id is not None:
                from persona_api.services.notifications_service import (  # noqa: PLC0415
                    publish_sidebar_changed,
                )

                publish_sidebar_changed(
                    event_channel, owner_id=owner_id, reason="conversation.title_updated"
                )
    except Exception as exc:  # noqa: BLE001 — auto-title must never break a chat turn
        _log.warning("auto-title failed for {cid}: {err}", cid=conversation_id, err=str(exc))
