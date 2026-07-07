"""The autonomy area endpoints (Spec A6) — the morning review (B5); controls follow (B4).

``GET /v1/autonomy/review`` serves the one shared morning digest, RLS-scoped. Opening the review
CONSUMES the deferred chatter atomically (build-from-RETURNING, A6-D-10) so it is delivered exactly
once across the review and C0's morning message; the digest's main sections never depend on it.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.digest import DeferredDigestStore, MorningDigest, build_morning_digest

router = APIRouter(prefix="/v1/autonomy", tags=["autonomy"])


@router.get("/review", response_model=MorningDigest)
async def get_review(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MorningDigest:
    """The caller's morning review — waiting → stuck → done → initiatives + upcoming (A6-D-2)."""
    engine = request.app.state.rls_engine
    now = datetime.now(UTC)
    deferred = DeferredDigestStore(engine).consume_undelivered(user.id, now=now)
    return build_morning_digest(
        engine,
        owner_id=user.id,
        config=request.app.state.config,
        now=now,
        deferred=deferred,
    )


__all__ = ["router"]
