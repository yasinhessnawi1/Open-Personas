"""Credits + usage routes (spec 08, T12, §5.5)."""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — used in cast() at runtime
from typing import cast

from fastapi import APIRouter, Depends, Request

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.schemas import (
    CreditsResponse,
    NotificationMarkReadResult,
    NotificationOut,
    UsageEntry,
)
from persona_api.services import credits_service, notifications_service

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
