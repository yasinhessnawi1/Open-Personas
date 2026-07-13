"""Calls history route (Spec V9, V9-D-5).

``GET /v1/calls`` — the caller's voice-call history, newest-first + paginated,
RLS-scoped. Read-only (CQS): the call-records are authored by the API-free voice
``CallRecorder`` (V9-D-5); this surface only reads them. Each item carries
``conversation_id`` so the web can open the saved transcript
(``GET /v1/conversations/{conversation_id}`` — the spoken turns now persist as
messages, V9-D-1/D-2).
"""

from __future__ import annotations

from typing import Any, cast

from fastapi import APIRouter, Depends, Request

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.schemas import CallSummary
from persona_api.services import calls_service

router = APIRouter(prefix="/v1", tags=["calls"])


@router.get("/calls", response_model=list[CallSummary])
async def list_calls(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
    limit: int = 50,
    offset: int = 0,
) -> list[CallSummary]:
    """List the caller's voice calls (newest-first, paginated; RLS-scoped)."""
    rows = calls_service.list_calls(
        rls_engine=request.app.state.rls_engine, limit=min(limit, 200), offset=offset
    )
    return [_call_summary(r) for r in rows]


def _call_summary(row: dict[str, object]) -> CallSummary:
    return CallSummary(
        call_id=str(row["call_id"]),
        conversation_id=str(row["conversation_id"]),
        persona_id=str(row["persona_id"]),
        title=str(row.get("title") or ""),
        started_at=cast("Any", row["started_at"]),
        ended_at=cast("Any", row.get("ended_at")),
        duration_s=cast("int | None", row.get("duration_s")),
        end_reason=cast("Any", row.get("end_reason")),
    )
