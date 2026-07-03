"""Credits + usage routes (spec 08, T12, §5.5)."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — used in cast() + Query at runtime
from typing import cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from persona.errors import InvalidTimezoneError, ScheduleNotFoundError
from persona.timezone import validate_timezone

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.schedules.store import ScheduleStore
from persona_api.schemas import (
    CreditsResponse,
    NotificationMarkReadResult,
    NotificationOut,
    UpdateProfileRequest,
    UsageEntry,
    UserProfileResponse,
)
from persona_api.schemas.requests import ScheduleRescheduleRequest
from persona_api.services import (
    calendar_reschedule_service,
    credits_service,
    notifications_service,
    occurrences_service,
    user_service,
)
from persona_api.services.calendar_reschedule_service import ReschedulePreview
from persona_api.services.occurrences_service import OccurrencesResult

router = APIRouter(prefix="/v1/me", tags=["me"])


@router.get("/credits", response_model=CreditsResponse)
async def get_credits(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> CreditsResponse:
    """The caller's current credit balance (stub counter; §5.5).

    ``low_balance`` is surfaced inline so the web app shows the under-limit
    warning without a second round-trip (D-11-12).
    """
    balance = request.app.state.credits_policy.get_balance(
        rls_engine=request.app.state.rls_engine, user_id=user.id
    )
    low = balance < credits_service.LOW_BALANCE_THRESHOLD
    return CreditsResponse(balance=balance, low_balance=low)


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
            created_at=cast("datetime", r["created_at"]),
        )
        for r in rows
    ]


@router.get("/profile", response_model=UserProfileResponse)
async def get_profile(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> UserProfileResponse:
    """The caller's own profile — identity + optional name (Spec K6, K6-D-1).

    Null-safe: ``first_name``/``last_name`` are ``None`` for a nameless account.
    The row is provisioned by ``ensure_user`` in the auth dependency, so a 404 here
    means a genuine invariant break rather than a first-time user.
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
    """Set the caller's optional name + timezone (Spec K6/A8). PATCH — omitted = unchanged.

    Only the fields the client actually sent are written (``exclude_unset``): a
    string sets, an explicit ``null`` clears, an omitted field is left untouched.
    Names are normalised (control-char strip, whitespace-only → unset) in the
    service (K6-D-8). A provided ``timezone`` is validated as an IANA zone here
    (Spec A8, A8-D-9) — an unknown zone is a fail-fast 422, never stored to
    mis-fire in the tick; ``null`` clears it (→ falls back to the config default).
    Scoped to the caller's own row — never another user's.
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
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> OccurrencesResult:
    """The caller's upcoming schedule occurrences + fire history (Spec A8, A8-D-11).

    Computed from the engine's own recurrence path (never a client reimplementation), RLS-scoped
    to the caller. The window is server-capped (horizon + count); the response's ``truncated``
    marker says so honestly when a wide ``from/to`` is clamped. ``from`` must be ``<= to``.
    """
    if from_ > to:
        raise HTTPException(status_code=422, detail="'from' must be <= 'to'")
    return occurrences_service.list_occurrences(
        request.app.state.rls_engine,
        owner_id=user.id,
        from_=from_,
        to=to,
        config=request.app.state.config,
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


def _reschedule_body(body: ScheduleRescheduleRequest) -> None:
    """Validate a calendar reschedule body: exactly one cadence kind + a valid IANA tz (422s)."""
    if (body.pattern is None) == (body.one_time_at is None):
        raise HTTPException(
            status_code=422, detail="send exactly one of 'pattern' or 'one_time_at'"
        )
    try:
        validate_timezone(body.timezone)
    except InvalidTimezoneError as exc:
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


__all__ = ["router"]
