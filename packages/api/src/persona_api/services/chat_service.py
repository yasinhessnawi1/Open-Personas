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

from persona_api.db.engine import aware_utc
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import messages as messages_t
from persona_api.db.models import personas as personas_t
from persona_api.errors import ConversationNotFoundError
from persona_api.services import document_service, image_service

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
    "get_conversation",
    "list_conversations",
    "get_active_turn",
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
    ranked = select(
        messages_t.c.conversation_id.label("conversation_id"),
        messages_t.c.content.label("content"),
        messages_t.c.role.label("role"),
        over(
            func.row_number(),
            partition_by=messages_t.c.conversation_id,
            order_by=(messages_t.c.created_at.desc(), messages_t.c.id.desc()),
        ).label("rn"),
    ).subquery("ranked_messages")
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
    """Return a conversation + its full message history (RLS-scoped → 404)."""
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
                .where(messages_t.c.conversation_id == conversation_id)
                .order_by(messages_t.c.created_at.asc())
            )
            .mappings()
            .all()
        )
    out = dict(conv)
    out["messages"] = [dict(m) for m in msgs]
    return out


def _load_conversation(conn: Connection, conversation_id: str) -> Conversation:
    """Materialise a runtime Conversation from the DB rows (RLS-scoped)."""
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
            .where(messages_t.c.conversation_id == conversation_id)
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
    return ConversationMessage(role=role, content=str(row["content"]), created_at=created_at)


def _sse(event: str, data: dict[str, object]) -> bytes:
    """Format one SSE event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


async def start_chat_turn(
    *,
    rls_engine: Engine,
    sink: MessagesTurnSink,
    registry: ChatTurnRegistry,
    loop_builder: LoopBuilder,
    owner_id: str,
    conversation_id: str,
    user_message: str,
    channel: ChannelContext | None,
    title_builder: Callable[[str], Awaitable[str]] | None = None,
    images: list[ImageRefSchema] | None = None,
    turn_has_image: bool = False,
    document_context: DocumentContext | None = None,
    workspace_root: Path | None = None,
    max_concurrent_long_ops: int = 0,
    file_storage: FileStorage | None = None,
) -> ChatTurnHandle:
    """Persist the turn at START + launch it detached; return the live handle (P1, T2b).

    The chat turn is now a **persistent, resumable session** (D-P1-detached-execution):

    1. Reject early if a turn is already streaming for this conversation (→ 409,
       block-don't-queue, D-P1-one-active-turn) — BEFORE any DB write, so the
       partial-unique index never has to fire.
    2. Persist the user message + an in-progress assistant row (``open_turn``),
       so a reload mid-turn refetches both (acceptance #2).
    3. Resolve the turn's images / documents (request scope — needs
       ``workspace_root``) and build the loop, exactly as the inline path did.
    4. Launch the detached task via the registry; a client disconnect no longer
       cancels it. The worker drives the loop, checkpoints, finalizes, and bills
       on clean completion (D-P1-billing-contract); the request streams the live
       tail via :func:`stream_turn`.

    Raises :class:`~persona_api.errors.TurnAlreadyActiveError` (→ 409),
    :class:`~persona_api.errors.ConversationNotFoundError` (→ 404), etc. cleanly
    BEFORE the SSE response starts — never mid-stream.
    """
    if registry.get(conversation_id) is not None:
        from persona_api.errors import TurnAlreadyActiveError  # noqa: PLC0415

        raise TurnAlreadyActiveError(
            "a turn is already running for this conversation",
            context={"conversation_id": conversation_id},
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
        assistant_message_id = sink.open_turn(
            conversation_id=conversation_id,
            user_message=user_message,
            channel=channel,
            images=images,
        )

        # Auto-title the first turn from its first user message (best-effort, small
        # tier) on the detached completion path — never delays / breaks the turn.
        on_complete: Callable[[], Awaitable[None]] | None = None
        if is_first_turn and title_builder is not None:
            _title_builder = title_builder

            async def on_complete() -> None:
                await _maybe_set_title(rls_engine, conversation_id, user_message, _title_builder)

        handle = registry.start(
            conversation_id=conversation_id,
            owner_id=owner_id,
            assistant_message_id=assistant_message_id,
            loop=loop,
            conversation=conversation,
            user_message=user_message,
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
) -> None:
    """Generate + persist a short title from the first message. Best-effort: any
    failure (model error, timeout) is logged and swallowed — the conversation
    keeps its default title rather than breaking the turn."""
    try:
        title = (await title_builder(first_message)).strip()
        if title:
            set_title(
                rls_engine=rls_engine, conversation_id=conversation_id, title=title[:_MAX_TITLE_LEN]
            )
    except Exception as exc:  # noqa: BLE001 — auto-title must never break a chat turn
        _log.warning("auto-title failed for {cid}: {err}", cid=conversation_id, err=str(exc))
