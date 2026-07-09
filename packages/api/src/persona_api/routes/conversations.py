"""Conversation lifecycle + SSE chat routes (spec 08, T08, §5.2, KEYSTONE 1).

The message endpoint streams ``ConversationLoop.turn`` over SSE. Every route is
RLS-scoped via ``get_current_user``. The per-request loop builder comes from
``app.state.build_conversation_loop`` (the runtime factory, T10).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime  # noqa: TC003 — used in cast() at runtime
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import StreamingResponse
from persona.approvals import is_decision_cue

from persona_api.approvals import ApprovalStore
from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.errors import TurnNotActiveError
from persona_api.middleware.rate_limit import rate_limit
from persona_api.routes._runtime_guard import require_runtime_wired
from persona_api.schemas import (
    ActiveTurnResponse,
    ConversationDetail,
    ConversationSummary,
    CreateConversationRequest,
    MessageView,
    PostMessageRequest,
)
from persona_api.services import (
    audit_service,
    chat_service,
    document_service,
    notifications_service,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.approvals import ProposalStatus

    from persona_api.approvals.resolver import ResolutionOutcome

router = APIRouter(prefix="/v1", tags=["conversations"])


@router.post(
    "/personas/{persona_id}/conversations",
    status_code=status.HTTP_201_CREATED,
    response_model=ConversationSummary,
)
async def create_conversation(
    persona_id: str,
    body: CreateConversationRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ConversationSummary:
    """Start a new conversation against a persona."""
    conv_id = chat_service.create_conversation(
        rls_engine=request.app.state.rls_engine,
        owner_id=user.id,
        persona_id=persona_id,
        title=body.title,
        origin=body.origin,
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="conversation.create",
        target=conv_id,
    )
    # R9-012: post-commit sidebar liveness ping — the owner's OTHER tabs/devices
    # soft-refresh their sidebar (data-only; best-effort, never fails the create).
    notifications_service.publish_sidebar_changed(
        getattr(request.app.state, "event_channel", None),
        owner_id=user.id,
        reason="conversation.created",
    )
    row = chat_service.get_conversation(
        rls_engine=request.app.state.rls_engine, conversation_id=conv_id
    )
    return _summary(row)


@router.get("/conversations", response_model=list[ConversationSummary])
async def list_conversations(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
    limit: int = 50,
    offset: int = 0,
) -> list[ConversationSummary]:
    """List the caller's conversations (paginated; RLS-scoped)."""
    rows = chat_service.list_conversations(
        rls_engine=request.app.state.rls_engine, limit=min(limit, 200), offset=offset
    )
    return [_summary(r) for r in rows]


@router.get("/conversations/{conversation_id}", response_model=ConversationDetail)
async def get_conversation(
    conversation_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> ConversationDetail:
    """Get a conversation's full message history (404 if not the caller's)."""
    row = chat_service.get_conversation(
        rls_engine=request.app.state.rls_engine, conversation_id=conversation_id
    )
    messages = cast("list[dict[str, Any]]", row["messages"])
    return ConversationDetail(
        id=str(row["id"]),
        persona_id=str(row["persona_id"]),
        title=str(row["title"]),
        origin=cast("Any", row["origin"]),
        messages=[
            MessageView(
                id=str(m["id"]),
                role=str(m["role"]),
                content=str(m["content"]),
                created_at=m["created_at"],
                channel=m.get("channel"),
                # Spec 35 D-35-2: persisted routing tier for the per-message chip.
                tier_used=cast("str | None", m.get("tier_used")),
                # Spec P3 (P3-D-2): project the persisted ordered rich event log
                # (P1's ``stream_events`` column) so the frontend reconstructs the
                # interleaved view. Reuses the existing reattach-surface coercion;
                # an empty/NULL column collapses to ``None`` → text-only render
                # (byte-exact back-compat, criterion 5).
                events=_coerce_stream_events(m.get("stream_events")) or None,
            )
            for m in messages
        ],
        created_at=cast("datetime", row["created_at"]),
        updated_at=cast("datetime", row["updated_at"]),
    )


@router.delete("/conversations/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_conversation(
    conversation_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Delete a conversation + all its messages + workspace artefacts.

    Cascade reach (T19 + Spec 13 T12 co-landing per D-14-X-cascade-coordination):

    1. DB rows (existing): the ``conversations`` row + its ``messages`` /
       ``turn_logs`` via FK cascade.
    2. **Document workspace + DocumentStore chunks** (T19): every doc
       attached to the conversation, via
       :func:`document_service.remove_all_for_conversation` — the
       cascade-helper T13 introduced specifically for this reuse.
    3. **Image workspace files** (Spec 13 T12): each image referenced by
       the conversation's messages. Spec 13 owns this branch; the two
       cascade extensions coexist additively in this same handler per
       the D-14-X-cascade-coordination locking decision.

    404 if not the caller's conversation (RLS-scoped).
    """
    # Pre-fetch persona_id BEFORE the DB delete — chat_service.delete_conversation
    # tears down the row, so we'd lose this lookup if we deferred it.
    # get_conversation also does the RLS ownership check (404 if cross-tenant).
    conv = chat_service.get_conversation(
        rls_engine=request.app.state.rls_engine, conversation_id=conversation_id
    )
    persona_id = str(conv["persona_id"])

    # DB delete (existing cascade to messages + turn_logs).
    chat_service.delete_conversation(
        rls_engine=request.app.state.rls_engine, conversation_id=conversation_id
    )

    # T19 — document workspace + DocumentStore chunks cascade (R5-D-4: via the
    # storage backend so uploads on S3 cascade-delete too).
    file_storage = getattr(request.app.state, "file_storage", None)
    build_document_store = getattr(request.app.state, "build_document_store", None)
    if file_storage is not None and build_document_store is not None:
        document_service.remove_all_for_conversation(
            file_storage=file_storage,
            owner_id=user.id,
            persona_id=persona_id,
            conversation_id=conversation_id,
            document_store=build_document_store(),
        )

    # (Spec 13 T12 inserts its image-workspace-cascade here when it lands.)

    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="conversation.delete",
        target=conversation_id,
    )
    # R9-012: post-commit sidebar liveness ping (see create_conversation).
    notifications_service.publish_sidebar_changed(
        getattr(request.app.state, "event_channel", None),
        owner_id=user.id,
        reason="conversation.deleted",
    )


async def _maybe_resolve_pending_approval(
    request: Request, *, owner_id: str, conversation_id: str, reply: str
) -> StreamingResponse | None:
    """A6 chat-twin: resolve a decision reply against a pending approval on this conversation.

    Returns a minimal resolution SSE when ALL hold: the reply is a decision cue (deterministic —
    no model), the resolution service is wired, and a pending approval exists on the conversation's
    ``waiting(on_user)`` task. Otherwise returns ``None`` → the caller runs a normal chat turn and
    the proposal stays PENDING. The reply goes through the SAME ``resolve(channel="chat")`` as the
    chat twin (identical floor → CAS → execute); the durable status is read back for the honest
    reflection (the persona's clarify/re-confirm + any resumed-leg output arrive via the C0 message
    + the A11 ``task.updated`` refetch, per A6-D-3/D-9).
    """
    if not is_decision_cue(reply):
        return None
    build = getattr(request.app.state, "build_approval_resolver", None)
    if build is None:  # keyless / community-without-C0 boot — fall through to a normal turn
        return None
    engine = request.app.state.rls_engine
    store = ApprovalStore(engine)
    proposal = store.get_pending_for_conversation(owner_id, conversation_id)
    if proposal is None:  # a cued reply with nothing pending → a normal turn
        return None
    # Persist the user's reply so the transcript shows it (no assistant row — the persona's voice
    # is the C0 clarify/reconfirm message + the resumed leg).
    request.app.state.chat_turn_sink.append_user_message(
        conversation_id=conversation_id, content=reply
    )
    outcome = await build().resolve(
        owner_id, proposal.proposal_id, reply, channel="chat", now=datetime.now(UTC)
    )
    status_after = store.get_proposal(owner_id, proposal.proposal_id).status
    return _approval_resolution_response(outcome, status_after)


def _approval_resolution_response(
    outcome: ResolutionOutcome, status_after: ProposalStatus
) -> StreamingResponse:
    """A minimal, immediately-ending SSE reflecting the durable resolution (A6-D-3 calm reflect)."""
    payload = {
        "kind": "approval_resolved",
        "outcome": outcome.outcome.value if outcome.outcome is not None else None,
        "executed": outcome.executed,
        "note": outcome.note,
        "status": status_after.value,  # the DURABLE post-state — the reflection anchor
    }

    async def _gen() -> AsyncIterator[bytes]:
        yield f"event: done\ndata: {json.dumps(payload)}\n\n".encode()

    return StreamingResponse(_gen(), media_type="text/event-stream")


@router.post(
    "/conversations/{conversation_id}/messages",
    dependencies=[Depends(rate_limit("messages"))],
)
async def post_message(
    conversation_id: str,
    body: PostMessageRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    """Send a message; stream the response as SSE (§5.2, KEYSTONE 1).

    Multimodal (spec 13 T20): when ``body.images`` is non-empty the route
    constructs a multimodal :class:`ConversationMessage` content list
    (``[TextContent, ImageContent, ...]``) and passes ``turn_has_image=True``
    through to the runtime so :meth:`Router.choose` restricts to vision-capable
    tiers. The text-only path (``body.images is None``) is unchanged
    byte-for-byte — T03/T13 regression invariants hold.
    """
    # Pre-flight the conversation existence/ownership BEFORE streaming begins, so
    # a missing/cross-tenant conversation returns a clean 404 (RLS-scoped) rather
    # than raising mid-stream after SSE headers are sent ("response already
    # started"). This is the per-endpoint RLS guard for the chat path (#4).
    chat_service.get_conversation(
        rls_engine=request.app.state.rls_engine, conversation_id=conversation_id
    )
    # Spec A6 chat-twin: a DECISION reply to a pending approval is NOT a model turn — route it to
    # the shared resolver (the SAME service the inbox uses), BEFORE the credits/runtime/long-op
    # guards (it is not a model turn), and reflect the durable outcome as a minimal SSE. Only a
    # decision-like message (the deterministic cue) that matches a pending approval on THIS
    # conversation is diverted; anything else falls through to the normal turn, leaving the proposal
    # PENDING (never auto-denied — the cue-gate's strictly-safer failure mode).
    approval_response = await _maybe_resolve_pending_approval(
        request, owner_id=user.id, conversation_id=conversation_id, reply=body.content
    )
    if approval_response is not None:
        return approval_response
    # Pre-flight credit guard: 402 BEFORE streaming starts (D-11-12 / spec 11 §5).
    request.app.state.credits_policy.require_credits(
        rls_engine=request.app.state.rls_engine, user_id=user.id
    )
    # Pre-flight runtime guard: a keyless/unwired boot (no model configured) →
    # clean 503 BEFORE streaming, not an AttributeError 500 (R1-D-2).
    require_runtime_wired(request, "build_conversation_loop")

    # The text-only path's ``user_message`` is BYTE-FOR-BYTE unchanged (T03/T13
    # regression invariant); image-bearing turns thread ``images`` + the
    # ``turn_has_image`` flag separately so the runtime loop's signature stays
    # ``user_message: str`` (the loop's prompt + retrieval pipeline operate on
    # text; images travel as references on the side and are persisted on the
    # ``messages.images`` JSONB column per D-13-X-now option (c)).
    turn_has_image = bool(body.images)

    # F3 follow-up — build the DocumentContext from the conversation's
    # attached documents BEFORE streaming starts (so a sidecar-read failure
    # surfaces as a clean 500 before SSE headers are sent). Looks up the
    # persona_id from the conversation (the workspace path is
    # persona-scoped); falls back to an empty context when nothing's
    # attached — text-only behaviour is byte-for-byte unchanged.
    document_store_builder = getattr(request.app.state, "build_document_store", None)
    document_context = None
    try:
        conversation_row = chat_service.get_conversation(
            rls_engine=request.app.state.rls_engine,
            conversation_id=conversation_id,
        )
        persona_id_for_docs = str(conversation_row["persona_id"])
        document_context = document_service.build_document_context(
            file_storage=request.app.state.file_storage,
            owner_id=user.id,
            persona_id=persona_id_for_docs,
            conversation_id=conversation_id,
            user_message=body.content,
            document_store=(document_store_builder() if document_store_builder else None),
        )
    except Exception:  # noqa: BLE001 — document path is best-effort
        # If document-context construction fails, log + continue with
        # text-only behaviour. The persona just doesn't see the docs
        # this turn; the chat flow stays usable rather than hard-erroring.
        document_context = None

    # Spec P1 (D-P1-detached-execution): start the turn as a DETACHED background
    # task + stream its live tail. ``start_chat_turn`` persists the user message +
    # an in-progress assistant row, launches the worker (which checkpoints,
    # finalizes, and bills on clean completion — D-P1-billing-contract), and
    # returns the live handle. A client disconnect mid-stream no longer cancels
    # the turn; it keeps running and is re-tailable on return (T4). One-active-turn
    # is enforced here: a 409 (TurnAlreadyActiveError) raises cleanly BEFORE the
    # SSE response starts, never mid-stream.
    handle = await chat_service.start_chat_turn(
        rls_engine=request.app.state.rls_engine,
        sink=request.app.state.chat_turn_sink,
        registry=request.app.state.chat_turn_registry,
        loop_builder=request.app.state.build_conversation_loop,
        owner_id=user.id,
        conversation_id=conversation_id,
        user_message=body.content,
        channel=body.channel,
        title_builder=getattr(request.app.state, "title_builder", None),
        images=list(body.images) if body.images else None,
        turn_has_image=turn_has_image,
        document_context=document_context,
        # Image-workspace cascade: thread the workspace root so the turn can
        # resolve the uploaded image bytes for the model + sandbox.
        workspace_root=getattr(request.app.state, "workspace_root", None),
        # Spec R7 (R7-D-4): the per-user long-op concurrency cap (chat SSE). 0 =
        # uncapped (community / unset), so existing behaviour is unchanged.
        max_concurrent_long_ops=getattr(request.app.state, "max_concurrent_long_ops", 0),
        # R5-D-4: the file-storage backend the turn resolves inbound image bytes
        # through (local/S3). workspace_root stays for the deferred file-tool paths.
        file_storage=getattr(request.app.state, "file_storage", None),
    )
    # Spec K2 (T8d): off-critical-path synthesis is enqueued at the turn boundary
    # by the detached worker on clean completion (relocated from the old inline
    # path into ChatTurnRegistry, which holds app.state.job_queue — D-K2-2 +
    # D-P1-detached-execution). No call-site wiring needed here.
    # The rate-limit dependency's headers don't auto-merge into a route-built
    # StreamingResponse (FastAPI limitation) — copy them from the stashed
    # decision so X-RateLimit-* appears on the SSE response too.
    headers: dict[str, str] = {}
    decision = getattr(request.state, "rate_limit_decision", None)
    if decision is not None:
        headers = decision.headers()
    return StreamingResponse(
        chat_service.stream_turn(handle), media_type="text/event-stream", headers=headers
    )


def _summary(row: dict[str, object]) -> ConversationSummary:
    # The LIST query (chat_service.list_conversations) attaches the derived
    # last-message fields (already truncated server-side); the CREATE path
    # returns a plain conversations row without them, so both are read with
    # ``.get`` and default to None — a just-created conversation has no
    # messages anyway, so None is the correct value there too.
    return ConversationSummary(
        id=str(row["id"]),
        persona_id=str(row["persona_id"]),
        title=str(row["title"]),
        # V9-D-3 birth-origin; default 'chat' guards a row read before migration 019.
        origin=cast("Any", row.get("origin", "chat")),
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
        last_message_preview=cast("str | None", row.get("last_message_preview")),
        last_message_role=cast("Any", row.get("last_message_role")),
    )


# -- Spec P1 reattach surface (mirrors the /runs/{id}/events + /cancel shape) ---


def _coerce_stream_events(value: object) -> list[dict[str, object]]:
    """Coerce the ``stream_events`` JSON column to a list of dicts (sqlite ↦ str)."""
    if value is None:
        return []
    if isinstance(value, str):
        loaded = json.loads(value)
        return list(loaded) if isinstance(loaded, list) else []
    return list(value) if isinstance(value, list) else []


@router.get("/conversations/{conversation_id}/active-turn", response_model=ActiveTurnResponse)
async def read_active_turn(
    conversation_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> ActiveTurnResponse:
    """The in-progress assistant turn for a conversation, for reattach-on-return (P1, T4).

    The web client calls this on return to detect a live turn and seed the partial
    (content + the tool/text interleave in ``stream_events``) before resubscribing
    to the live tail. 404 (``TurnNotActiveError``) when no turn is in flight — the
    client then reconciles via the conversation history. RLS-scoped → 404 if the
    conversation isn't the caller's.
    """
    chat_service.get_conversation(
        rls_engine=request.app.state.rls_engine, conversation_id=conversation_id
    )
    row = chat_service.get_active_turn(
        rls_engine=request.app.state.rls_engine, conversation_id=conversation_id
    )
    if row is None:
        raise TurnNotActiveError("no active turn", context={"conversation_id": conversation_id})
    return ActiveTurnResponse(
        message_id=str(row["id"]),
        streaming_status=str(row["streaming_status"]),
        content=str(row["content"]),
        stream_events=_coerce_stream_events(row.get("stream_events")),
    )


@router.get("/conversations/{conversation_id}/active-turn/events")
async def stream_active_turn_events(
    conversation_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> StreamingResponse:
    """Resubscribe to a live turn's SSE tail (reattach) — the SAME ``stream_turn``
    generator the originating POST streams (P1, T4; don't fork the transport).

    RLS-scoped ownership pre-check → 404 if the conversation isn't the caller's.
    404 (``TurnNotActiveError``) if no live turn is registered in-process (it
    finished / was interrupted / never started) — checked BEFORE the SSE response
    starts, so the client gets a clean 404 (then reconciles) rather than a
    half-open stream.
    """
    chat_service.get_conversation(
        rls_engine=request.app.state.rls_engine, conversation_id=conversation_id
    )
    registry = getattr(request.app.state, "chat_turn_registry", None)
    handle = registry.get(conversation_id) if registry is not None else None
    if handle is None:
        raise TurnNotActiveError("no active turn", context={"conversation_id": conversation_id})
    return StreamingResponse(chat_service.stream_turn(handle), media_type="text/event-stream")


@router.post(
    "/conversations/{conversation_id}/active-turn/cancel",
    status_code=status.HTTP_202_ACCEPTED,
)
async def cancel_active_turn(
    conversation_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, str]:
    """Explicitly cancel a live chat turn (mirrors ``/runs/{id}/cancel``; P1, T4).

    Flips the turn's task cancel (``ChatTurnRegistry.request_cancel``): the worker
    finalizes the partial as ``cancelled`` and does NOT bill (D-P1-billing-contract).
    RLS-scoped → 404 if the conversation isn't the caller's; 404 if no live turn.
    """
    chat_service.get_conversation(
        rls_engine=request.app.state.rls_engine, conversation_id=conversation_id
    )
    registry = getattr(request.app.state, "chat_turn_registry", None)
    cancelled = registry.request_cancel(conversation_id) if registry is not None else False
    if not cancelled:
        raise TurnNotActiveError("no active turn", context={"conversation_id": conversation_id})
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="conversation.turn.cancel",
        target=conversation_id,
    )
    return {"status": "cancelling"}
