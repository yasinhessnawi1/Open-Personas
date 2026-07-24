"""Credits + usage routes (spec 08, T12, §5.5)."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — used in cast() + Query at runtime
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from persona.billing.plans import Plan, get_plan
from persona.errors import (
    InvalidTimezoneError,
    PersonaNotFoundError,
    ScheduleNeverFiresError,
    ScheduleNotFoundError,
)
from persona.timezone import validate_timezone

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.realtime.stream import stream_user_events
from persona_api.schedules.store import ScheduleStore
from persona_api.schedules.tombstones import ScheduleTombstoneStore
from persona_api.schemas import (
    CreditsResponse,
    LedgerEntry,
    NavCountsResponse,
    NotificationMarkReadResult,
    NotificationOut,
    PaygLotOut,
    UpdateProfileRequest,
    UsageEntry,
    UserProfileResponse,
    WalletResponse,
)
from persona_api.schemas.requests import ScheduleCreateRequest, ScheduleRescheduleRequest
from persona_api.services import (
    calendar_reschedule_service,
    nav_counts_service,
    notifications_service,
    occurrences_service,
    schedule_create_service,
    schedule_delete_service,
    subscription_service,
    user_service,
)
from persona_api.services.calendar_reschedule_service import ReschedulePreview
from persona_api.services.occurrences_service import OccurrencesResult
from persona_api.services.schedule_create_service import ScheduleCreateResult
from persona_api.tasks.store import TaskStore

router = APIRouter(prefix="/v1/me", tags=["me"])


def _plan_for_user(request: Request, user_id: str) -> Plan:
    """The caller's plan from their ``subscription`` row — absent/unknown ⇒ Free (Spec M4).

    A pure read used by the credits/wallet surfaces to pick the PER-PLAN low-balance
    warning line (T8). Absent row = free (the T5c precedent); an unknown ``plan_code``
    (forward-compat) also falls back to Free — the restrictive default.
    """
    sub = subscription_service.get_subscription(request.app.state.rls_engine, user_id=user_id)
    code = str(sub.get("plan_code") or "free") if sub is not None else "free"
    try:
        return get_plan(code)
    except (KeyError, ValueError):
        return get_plan("free")


@router.get("/credits", response_model=CreditsResponse)
async def get_credits(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CreditsResponse:
    """The caller's current credit balance (§5.5).

    ``low_balance`` is surfaced inline so the web app shows the under-limit warning
    without a second round-trip (D-11-12). The warning line is PER-PLAN (Spec M4 T8 —
    20% of the plan's included allowance: Free 60, Plus 400, Pro 1200; the flat 10 000
    threshold is retired). Community's unmetered sentinel balance is never "low".
    """
    balance = request.app.state.credits_policy.get_balance(
        rls_engine=request.app.state.rls_engine, user_id=user.id
    )
    plan = _plan_for_user(request, user.id)
    low = balance < plan.low_balance_threshold_credits
    return CreditsResponse(balance=balance, low_balance=low)


@router.get("/wallet", response_model=WalletResponse)
async def get_wallet(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> WalletResponse:
    """The caller's two-bucket dollar-wallet (Spec M4, T8) — the settings billing surface.

    Read-only composition of two RLS-scoped reads (no business logic here): the policy's
    ``wallet_snapshot`` (allowance bucket + period stamp + live PAYG lots in FIFO spend
    order + total — the Metered policy self-heals the free monthly allowance first, T6)
    and the caller's ``subscription`` row (``plan_code`` / ``auto_topup_enabled``; absent
    ⇒ ``free`` / ``False``). ``low_balance`` compares the total against the caller's
    per-plan warning line. Community reports the unmetered sentinel (no lots, never low).
    """
    snapshot = request.app.state.credits_policy.wallet_snapshot(
        rls_engine=request.app.state.rls_engine, user_id=user.id
    )
    sub = subscription_service.get_subscription(request.app.state.rls_engine, user_id=user.id)
    plan = _plan_for_user(request, user.id)
    total = int(cast("int", snapshot["total_balance"]))
    return WalletResponse(
        total_balance=total,
        allowance_balance=int(cast("int", snapshot["allowance_balance"])),
        allowance_period=cast("str | None", snapshot["allowance_period"]),
        payg_lots=[
            PaygLotOut(
                credits_remaining=int(cast("int", lot["credits_remaining"])),
                credits_total=int(cast("int", lot["credits_total"])),
                expires_at=cast("datetime", lot["expires_at"]),
            )
            for lot in cast("list[dict[str, object]]", snapshot["payg_lots"])
        ],
        plan_code=str(sub.get("plan_code") or "free") if sub is not None else "free",
        auto_topup_enabled=bool(sub.get("auto_topup_enabled")) if sub is not None else False,
        low_balance=total < plan.low_balance_threshold_credits,
        low_balance_threshold=plan.low_balance_threshold_credits,
    )


@router.get("/usage", response_model=list[UsageEntry])
async def get_usage(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
    limit: int = 50,
    offset: int = 0,
) -> list[UsageEntry]:
    """The caller's per-turn token usage (§5.5; turn_logs, RLS-scoped)."""
    rows = request.app.state.credits_policy.list_turn_usage(
        rls_engine=request.app.state.rls_engine,
        limit=min(limit, 200),
        offset=offset,
    )
    return [
        UsageEntry(
            persona_id=cast("str | None", r.get("persona_id")),
            tier_used=str(r["tier_used"]),
            model_name=str(r["model_name"]),
            prompt_tokens=int(cast("int", r["prompt_tokens"])),
            completion_tokens=int(cast("int", r["completion_tokens"])),
            cost_cents=float(cast("float", r["cost_cents"])),
            # Spec M2 (D-M2-4): pricing provenance; NULL = legacy pre-M2 row
            # (the web renders it as an estimate).
            cost_basis=cast("str | None", r.get("cost_basis")),
            created_at=cast("datetime", r["created_at"]),
        )
        for r in rows
    ]


@router.get("/usage/ledger", response_model=list[LedgerEntry])
async def get_usage_ledger(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    limit: int = 50,
    offset: int = 0,
) -> list[LedgerEntry]:
    """The caller's ALL-surface credit ledger (Spec M3, T8; credit_transactions, RLS-scoped).

    Where ``/v1/me/usage`` reads only ``turn_logs`` (chat turns), this reads the
    ``credit_transactions`` ledger every billed surface writes to — chat, authoring,
    image (+ true-up), agentic runs, task legs, background LLM (episodic /
    voice-autopick / initiative), voice per-turn + the LiveKit tick, avatar, and
    sandbox — newest-first, each row carrying its ``cost_basis`` provenance + the
    credits moved (``delta``). Owner-scoped both by the RLS engine AND the explicit
    ``user_id`` filter; the community-unmetered edition returns an empty list.
    """
    rows = request.app.state.credits_policy.list_usage(
        rls_engine=request.app.state.rls_engine,
        user_id=user.id,
        limit=min(limit, 200),
        offset=offset,
    )
    return [
        LedgerEntry(
            reason=str(r["reason"]),
            delta=int(cast("int", r["delta"])),
            cost_cents=cast("float | None", r.get("cost_cents")),
            cost_basis=cast("str | None", r.get("cost_basis")),
            created_at=cast("datetime", r["created_at"]),
        )
        for r in rows
    ]


@router.get("/nav-counts", response_model=NavCountsResponse)
async def get_nav_counts(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> NavCountsResponse:
    """The caller's sidebar nav-badge counts in one round-trip (R9-010).

    Six owner-scoped, index-friendly ``COUNT``s (personas / chat conversations /
    calls / non-terminal tasks / schedule rows / canonical graph nodes) — see
    :mod:`persona_api.services.nav_counts_service` for the pinned semantics.
    RLS-scoped like the sibling ``/v1/me`` routes; ``memory_nodes`` is ``0``
    when no graph store is wired (the Memory nav row is hidden then anyway).
    """
    counts = nav_counts_service.get_nav_counts(
        request.app.state.rls_engine,
        owner_id=user.id,
        include_memory=getattr(request.app.state, "graph_store", None) is not None,
    )
    return NavCountsResponse(
        personas=counts["personas"],
        conversations=counts["conversations"],
        calls=counts["calls"],
        memory_nodes=counts["memory_nodes"],
        active_tasks=counts["active_tasks"],
        schedules=counts["schedules"],
    )


@router.get("/profile", response_model=UserProfileResponse)
async def get_profile(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserProfileResponse:
    """The caller's own profile — identity + optional name + preferences (Spec K6, K6-D-1).

    Null-safe: ``first_name``/``last_name`` are ``None`` for a nameless account, and
    ``preferred_model`` (Spec M1, M1-T6 — the sticky last-choice default) is ``None``
    until the caller has picked one. The row is provisioned by ``ensure_user`` in the
    auth dependency, so a 404 here means a genuine invariant break rather than a
    first-time user.
    """
    row = user_service.get_user_profile(request.app.state.rls_engine, user_id=user.id)
    if row is None:  # pragma: no cover - ensure_user guarantees the row exists
        raise HTTPException(status_code=404, detail="user profile not found")
    return UserProfileResponse.model_validate(row)


@router.patch("/profile", response_model=UserProfileResponse)
async def update_profile(
    request: Request,
    body: UpdateProfileRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserProfileResponse:
    """Set the caller's optional name + timezone + model preference (Spec K6/A8/M1).

    PATCH — omitted = unchanged. Only the fields the client actually sent are
    written (``exclude_unset``): a string sets, an explicit ``null`` clears, an
    omitted field is left untouched. Names are normalised (control-char strip,
    whitespace-only → unset) in the service (K6-D-8). A provided ``timezone`` is
    validated as an IANA zone here (Spec A8, A8-D-9) — an unknown zone is a
    fail-fast 422, never stored to mis-fire in the tick; ``null`` clears it (→
    falls back to the config default). ``preferred_model`` (Spec M1, M1-T6 — the
    sticky last-choice default) needs no such call-out: the request schema already
    rejects blank/whitespace-only (422), and its catalog validity is the WEB
    picker's concern, not checked here; ``null`` clears it (→ falls back to the
    tier-resolved default). Scoped to the caller's own row — never another user's.
    """
    provided = body.model_dump(exclude_unset=True)
    tz = provided.get("timezone")
    if tz is not None:
        try:
            validate_timezone(tz)
        except InvalidTimezoneError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    _validate_quiet_hours(provided)
    row = user_service.update_user_profile(
        request.app.state.rls_engine, user_id=user.id, **provided
    )
    if row is None:  # pragma: no cover - ensure_user guarantees the row exists
        raise HTTPException(status_code=404, detail="user profile not found")
    return UserProfileResponse.model_validate(row)


@router.get("/schedule/occurrences", response_model=OccurrencesResult)
async def get_schedule_occurrences(
    request: Request,
    from_: datetime = Query(alias="from"),
    to: datetime = Query(alias="to"),
    persona_id: str | None = Query(default=None),
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> OccurrencesResult:
    """The caller's upcoming schedule occurrences + fire history (Spec A8, A8-D-11).

    Computed from the engine's own recurrence path (never a client reimplementation), RLS-scoped
    to the caller. The window is server-capped (horizon + count); the response's ``truncated``
    marker says so honestly when a wide ``from/to`` is clamped. ``from`` must be ``<= to``.

    ``persona_id`` (R9-024, additive-optional) scopes the result to one persona's schedules — the
    chat right-panel calendar. Server-side resolved (see
    :func:`persona_api.services.occurrences_service._resolved_persona_id`); omitted, the response
    is byte-identical to pre-R9-024 (every owned schedule, as before).
    """
    if from_ > to:
        raise HTTPException(status_code=422, detail="'from' must be <= 'to'")
    return occurrences_service.list_occurrences(
        request.app.state.rls_engine,
        owner_id=user.id,
        from_=from_,
        to=to,
        config=request.app.state.config,
        persona_id=persona_id,
    )


def _validate_quiet_hours(provided: dict[str, object]) -> None:
    """Enforce quiet-hours coherence on a profile PATCH (A8-D-6): both-or-neither, valid window.

    Quiet hours are a PAIR — the two fields must move together (both set to enable, both null to
    disable); an empty window (start == end) is rejected. Sending only one, or a lone non-null,
    is a 422 (never a half-set window).
    """
    from persona.schedules import QuietHours

    has_start = "quiet_hours_start" in provided
    has_end = "quiet_hours_end" in provided
    if not has_start and not has_end:
        return  # untouched
    if has_start != has_end:
        raise HTTPException(
            status_code=422, detail="quiet_hours_start and quiet_hours_end must be sent together"
        )
    start, end = provided["quiet_hours_start"], provided["quiet_hours_end"]
    if (start is None) != (end is None):
        raise HTTPException(status_code=422, detail="quiet hours must be both set or both cleared")
    if start is not None and end is not None:
        try:
            QuietHours(start_minute=cast("int", start), end_minute=cast("int", end))
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc


def _reschedule_body(body: ScheduleRescheduleRequest | ScheduleCreateRequest) -> None:
    """Validate a cadence body: exactly one cadence kind + a valid IANA tz (422s).

    Shared by the reschedule twin (A8) and the create door (A10) — both carry the same
    picker-state cadence envelope.
    """
    if (body.pattern is None) == (body.one_time_at is None):
        raise HTTPException(
            status_code=422, detail="send exactly one of 'pattern' or 'one_time_at'"
        )
    try:
        validate_timezone(body.timezone)
    except InvalidTimezoneError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/schedule/preview", response_model=ReschedulePreview)
async def preview_schedule_create(
    request: Request,
    body: ScheduleRescheduleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReschedulePreview:
    """Preview a schedule CREATE — the engine's next-fire + full clause + quiet-hours warn.

    No write (Spec A10, criterion 2/7): the create dialog shows this as the confirm echo
    before the user confirms — the SAME shape (and the same shared engine preview,
    ``preview_schedule_cadence``) as A8's reschedule preview; the twins never fabricate a
    time the DST gap/fold policy would shift. The body is the bare cadence envelope
    (pattern XOR one_time_at + tz) — a preview needs no executor/subject/key.
    """
    _reschedule_body(body)
    from datetime import UTC, datetime

    return calendar_reschedule_service.preview_schedule_cadence(
        request.app.state.rls_engine,
        owner_id=user.id,
        pattern=body.pattern,
        one_time_at=body.one_time_at,
        timezone=body.timezone,
        now=datetime.now(UTC),
    )


@router.post("/schedule", response_model=ScheduleCreateResult)
async def create_schedule(
    request: Request,
    body: ScheduleCreateRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ScheduleCreateResult:
    """Create a schedule + its backing task — the user's direct door (Spec A10, A10-D-1/2/6).

    Deterministic and model-free: picker-state in (no raw RRULE), A8's ``ScheduleStore``
    CAS door + the A2 task path underneath (one mechanism, A10-D-9). The named persona is
    the executor; the user is the originator. Idempotent on the client-minted
    ``idempotency_key`` (a double-click converges; two deliberate submits stay distinct).
    422 on a never-firing cadence; 404 on an executor persona that isn't the caller's.
    """
    _reschedule_body(body)
    from datetime import UTC, datetime

    from persona_api.tasks.store import TaskStore

    engine = request.app.state.rls_engine
    try:
        result = schedule_create_service.create_user_schedule(
            engine,
            ScheduleStore(engine),
            TaskStore(engine),
            owner_id=user.id,
            pattern=body.pattern,
            one_time_at=body.one_time_at,
            timezone=body.timezone,
            persona_id=body.persona_id,
            subject=body.subject,
            idempotency_key=body.idempotency_key,
            now=datetime.now(UTC),
            notify_on_fire=body.notify_on_fire,
            intent=body.intent,
        )
        # R9-012: post-commit sidebar liveness ping — the Schedule badge on the
        # owner's OTHER tabs/devices catches up (data-only; best-effort).
        notifications_service.publish_sidebar_changed(
            getattr(request.app.state, "event_channel", None),
            owner_id=user.id,
            reason="schedule.created",
        )
        return result
    except PersonaNotFoundError as exc:
        raise HTTPException(status_code=404, detail="executor persona not found") from exc
    except ScheduleNeverFiresError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/schedule/{schedule_id}/reschedule/preview", response_model=ReschedulePreview)
async def preview_schedule_reschedule(
    schedule_id: str,  # noqa: ARG001 — preview is schedule-independent (a pure cadence preview)
    request: Request,
    body: ScheduleRescheduleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReschedulePreview:
    """Preview a calendar reschedule — the engine's next-fire + full clause + quiet-hours warn.

    No write (Spec A8, T9, bars 3/5): the picker shows this as the confirm echo (the SAME full
    clause chat re-echoes) before the user confirms. The next fire is the ENGINE's, so the picker
    never fabricates a time the DST gap/fold policy would shift.
    """
    _reschedule_body(body)
    from datetime import UTC, datetime

    return calendar_reschedule_service.preview_calendar_reschedule(
        request.app.state.rls_engine,
        owner_id=user.id,
        pattern=body.pattern,
        one_time_at=body.one_time_at,
        timezone=body.timezone,
        now=datetime.now(UTC),
    )


@router.post("/schedule/{schedule_id}/reschedule", response_model=ReschedulePreview)
async def apply_schedule_reschedule(
    schedule_id: str,
    request: Request,
    body: ScheduleRescheduleRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ReschedulePreview:
    """Apply a calendar reschedule through the SAME CAS door as chat (Spec A8, T9, bar 4).

    ``actor=user_via_ui``; the client sends picker-state (no raw RRULE — the server maps it). RLS-
    scoped to the caller. Returns the applied clause + the engine's next fire (the twin's confirm).
    """
    _reschedule_body(body)
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    store = ScheduleStore(request.app.state.rls_engine)
    try:
        applied = calendar_reschedule_service.apply_calendar_reschedule(
            store,
            request.app.state.rls_engine,
            owner_id=user.id,
            schedule_id=schedule_id,
            pattern=body.pattern,
            one_time_at=body.one_time_at,
            timezone=body.timezone,
            now=now,
        )
    except ScheduleNotFoundError as exc:
        raise HTTPException(status_code=404, detail="schedule not found") from exc
    return calendar_reschedule_service.preview_calendar_reschedule(
        request.app.state.rls_engine,
        owner_id=user.id,
        pattern=body.pattern,
        one_time_at=body.one_time_at,
        timezone=applied.timezone,
        now=now,
    )


@router.delete("/schedule/{schedule_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_schedule(
    schedule_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Delete a schedule (R9-024 — the calendar's delete affordance, chat + ``/schedule``).

    RLS-scoped through :meth:`~persona_api.schedules.store.ScheduleStore.delete`: a cross-tenant
    id is indistinguishable from a missing one (both 404 — no existence oracle). Audits
    ``schedule.delete``.

    **R9-037.** The delete now writes a durable tombstone (the user's DELETE is
    persona-scoped, durable, intent) every autonomous origination seam consults before
    (re-)creating — the owner-reported bug where a deleted schedule silently came back.
    The backing task (if any — a ``task_scheduled_fire`` schedule), if still live, is
    PAUSED (design point 3: deleting the schedule stops the task's recurrence; the task
    must not silently re-arm, but it is also not silently forgotten — a P6 notification
    tells the user why). This supersedes the pre-R9-037 "backing task untouched" note.
    """
    try:
        schedule_delete_service.delete_schedule_with_intent(
            schedule_store=ScheduleStore(request.app.state.rls_engine),
            task_store=TaskStore(request.app.state.rls_engine),
            tombstones=ScheduleTombstoneStore(request.app.state.rls_engine),
            rls_engine=request.app.state.rls_engine,
            owner_id=user.id,
            schedule_id=schedule_id,
            event_channel=getattr(request.app.state, "event_channel", None),
        )
    except ScheduleNotFoundError as exc:
        raise HTTPException(status_code=404, detail="schedule not found") from exc
    # R9-012: post-commit sidebar liveness ping — the Schedule badge on the owner's
    # other tabs/devices catches up (data-only; best-effort).
    notifications_service.publish_sidebar_changed(
        getattr(request.app.state, "event_channel", None),
        owner_id=user.id,
        reason="schedule.deleted",
    )


@router.get("/notifications", response_model=list[NotificationOut])
async def get_notifications(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
    limit: int = 50,
    offset: int = 0,
) -> list[NotificationOut]:
    """The caller's durable bell feed (Spec P6, RLS-scoped, newest-first, paginated)."""
    rows = notifications_service.list_notifications(
        rls_engine=request.app.state.rls_engine,
        limit=min(limit, 200),
        offset=offset,
    )
    return [
        NotificationOut(
            id=str(r["id"]),
            kind=str(r["kind"]),
            ref_id=cast("str | None", r["ref_id"]),
            level=str(r["level"]),
            message_key=str(r["message_key"]),
            params=cast("dict[str, str]", r["params"]),
            read=bool(r["read"]),
            created_at=cast("datetime", r["created_at"]),
        )
        for r in rows
    ]


@router.post("/notifications/read-all", response_model=NotificationMarkReadResult)
async def mark_all_notifications_read(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> NotificationMarkReadResult:
    """Mark every unread notification read (bell-open, Spec P6)."""
    updated = notifications_service.mark_all_read(rls_engine=request.app.state.rls_engine)
    return NotificationMarkReadResult(updated=updated)


@router.post("/notifications/{notification_id}/read", response_model=NotificationMarkReadResult)
async def mark_notification_read(
    notification_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> NotificationMarkReadResult:
    """Mark one notification read (deep-link click, Spec P6). 0 if not owned/absent."""
    updated = notifications_service.mark_read(
        rls_engine=request.app.state.rls_engine, notification_id=notification_id
    )
    return NotificationMarkReadResult(updated=updated)


@router.get("/events")
async def stream_events(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    """The persistent, RLS-scoped live channel (Spec A11) — the out-of-turn SSE feed
    that makes a background delivery surface live (the bell + the open chat, no
    reload; closes R4-C1-23).

    The me-scope is the **verified token's** user id (``get_current_user``), never a
    param. One connection per open tab, heartbeat-kept, ``Last-Event-ID``-resumable
    (A11-D-3). Fail-soft: an unwired channel returns 503 so the web app degrades to
    P6's poll (never a broken shell); the active-run token stream is untouched (this
    is the out-of-turn channel).
    """
    channel = getattr(request.app.state, "event_channel", None)
    if channel is None:
        raise HTTPException(status_code=503, detail="realtime channel unavailable")
    # The client (fetch + ReadableStream, not EventSource — it must carry the Clerk
    # Bearer header) sends the resume cursor as a header, keeping it out of logs /
    # proxy caches (A11-D-3).
    last_event_id = request.headers.get("last-event-id")
    return StreamingResponse(
        stream_user_events(channel, user.id, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            # Defeat proxy/edge response buffering so frames flush immediately.
            "X-Accel-Buffering": "no",
        },
    )


__all__ = ["router"]
