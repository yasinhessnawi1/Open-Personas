"""Per-user, per-endpoint, per-minute rate limiting (spec 08, T09, §6).

Rate limiting exists from the first endpoint (architecture §9.9 — not a week-15
task). A fixed-window counter keyed by ``(user_id, endpoint, minute)``:

- ``InMemoryRateLimitStore`` — dict-based, for dev/tests.
- ``PostgresRateLimitStore`` — the ``rate_limit_buckets`` table (spec 07). NOT
  under RLS (spec-07 rls.py: it's the platform limiter's table, not tenant
  data), so it uses a plain engine connection, not the RLS scope.

Per-endpoint limits (§6): messages 20, runs 5, author 3, else the default 60.
Every response carries ``X-RateLimit-Limit/Remaining/Reset``; over-limit raises
:class:`RateLimitExceededError` (429) with those headers + ``Retry-After``.
"""

from __future__ import annotations

import threading
from collections.abc import Awaitable, Callable  # runtime: used in a FastAPI dep signature
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from fastapi import Request, Response
from sqlalchemy import text

from persona_api.errors import RateLimitExceededError

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "InMemoryRateLimitStore",
    "PostgresRateLimitStore",
    "RateLimitDecision",
    "RateLimitStore",
    "RateLimiter",
    "rate_limit",
]

_WINDOW_SECONDS = 60


@runtime_checkable
class RateLimitStore(Protocol):
    """Port for the fixed-window counter (CQS-bending: increments AND returns)."""

    def incr(self, user_id: str, endpoint: str, window_start: datetime) -> int:
        """Increment the bucket and return the new count for the window."""
        ...


class InMemoryRateLimitStore:
    """Dict-based store for dev/tests. Thread-safe; unbounded (fine for a worker)."""

    def __init__(self) -> None:
        self._counts: dict[tuple[str, str, datetime], int] = {}
        self._lock = threading.Lock()

    def incr(self, user_id: str, endpoint: str, window_start: datetime) -> int:
        key = (user_id, endpoint, window_start)
        with self._lock:
            new = self._counts.get(key, 0) + 1
            self._counts[key] = new
            return new


class PostgresRateLimitStore:
    """Backed by the ``rate_limit_buckets`` table (spec 07). Not RLS-scoped."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def incr(self, user_id: str, endpoint: str, window_start: datetime) -> int:
        # Atomic upsert-and-increment; the composite PK makes the ON CONFLICT
        # target the (user, endpoint, window) row. RETURNING gives the new count.
        stmt = text(
            "INSERT INTO rate_limit_buckets (user_id, endpoint, window_start, request_count) "
            "VALUES (:u, :e, :w, 1) "
            "ON CONFLICT (user_id, endpoint, window_start) "
            "DO UPDATE SET request_count = rate_limit_buckets.request_count + 1 "
            "RETURNING request_count"
        )
        with self._engine.begin() as conn:
            count = conn.execute(
                stmt, {"u": user_id, "e": endpoint, "w": window_start}
            ).scalar_one()
        return int(count)


class RateLimitDecision:
    """The headers + verdict for one limit check."""

    def __init__(self, *, limit: int, remaining: int, reset_epoch: int) -> None:
        self.limit = limit
        self.remaining = remaining
        self.reset_epoch = reset_epoch

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset_epoch),
        }


class RateLimiter:
    """Per-user, per-endpoint, per-minute fixed-window limiter (§6)."""

    def __init__(
        self,
        store: RateLimitStore,
        *,
        default_limit: int = 60,
        per_endpoint: dict[str, int] | None = None,
    ) -> None:
        self._store = store
        self._default = default_limit
        self._limits = per_endpoint or {}

    def limit_for(self, endpoint: str) -> int:
        return self._limits.get(endpoint, self._default)

    def check(
        self, user_id: str, endpoint: str, *, now: datetime | None = None
    ) -> RateLimitDecision:
        """Count this request; raise :class:`RateLimitExceededError` if over limit.

        Returns the :class:`RateLimitDecision` (headers) when allowed.
        """
        moment = now or datetime.now(UTC)
        window_start = moment.replace(second=0, microsecond=0)
        reset_epoch = int(window_start.timestamp()) + _WINDOW_SECONDS
        limit = self.limit_for(endpoint)
        count = self._store.incr(user_id, endpoint, window_start)
        remaining = max(0, limit - count)
        if count > limit:
            raise RateLimitExceededError(
                "rate limit exceeded",
                context={
                    "limit": str(limit),
                    "remaining": "0",
                    "reset": str(reset_epoch),
                    "retry_after": str(max(0, reset_epoch - int(moment.timestamp()))),
                },
            )
        return RateLimitDecision(limit=limit, remaining=remaining, reset_epoch=reset_epoch)


def rate_limit(endpoint_key: str) -> Callable[..., Awaitable[None]]:
    """Build a FastAPI dependency that enforces the limit for ``endpoint_key``.

    Depends on ``get_current_user`` so authentication (which sets the RLS
    contextvar) is guaranteed to run first. On allow, sets ``X-RateLimit-*``
    headers on the response; on exceed, raises :class:`RateLimitExceededError`
    (429 + headers, via the handler). The limiter is ``app.state.rate_limiter``.
    """
    # Imported here to avoid a circular import (auth.deps → errors → ...).
    from fastapi import Depends

    from persona_api.auth import AuthenticatedUser, get_current_user

    async def _dep(
        request: Request,
        response: Response,
        user: AuthenticatedUser = Depends(get_current_user),
    ) -> None:
        limiter: RateLimiter = request.app.state.rate_limiter
        decision: RateLimitDecision | None = getattr(request.state, "rate_limit_decision", None)
        if decision is None or getattr(request.state, "rate_limit_counted", None) != (
            user.id,
            endpoint_key,
        ):
            # Not counted yet. A route that must refuse before reading its body (the
            # upload route) counts the request earlier and leaves the decision here.
            decision = limiter.check(user.id, endpoint_key)
        # For normal (JSON) responses, headers on the injected Response merge into
        # the final response. For a route that returns its OWN StreamingResponse,
        # they DON'T (FastAPI doesn't merge into a route-constructed Response) —
        # so also stash the decision; the streaming route copies the headers on.
        for key, value in decision.headers().items():
            response.headers[key] = value
        request.state.rate_limit_decision = decision

    return _dep
