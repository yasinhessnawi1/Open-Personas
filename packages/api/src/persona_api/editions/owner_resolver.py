"""The auth seam: "who owns this request?" → ``owner_id`` (Spec 33, §2.1).

``OwnerResolver`` is the single seam through which the edition decides request
ownership. Everything downstream — the RLS contextvar, the persona-ownership
pre-flight on every route — consumes ``owner_id`` **unchanged** (D-33-X-
ownerresolver-seam): only the *source* of the owner swaps.

- :class:`CloudOwnerResolver` reproduces today's behavior exactly — verify the
  Clerk JWT (via the overridable ``verify`` seam), JIT-provision the ``users``
  row on the superuser engine. No behavior change for cloud.
- :class:`CommunityOwnerResolver` returns a fixed local owner (D-33-3): no JWT,
  no sign-in, single-user. The owner row is pre-seeded at startup
  (D-33-X-owner-seed), so no JIT provisioning is needed.
"""

# CommunityOwnerResolver.resolve intentionally ignores ``request``/``verify``
# (it returns a fixed owner) but must keep the OwnerResolver Protocol signature.
# ruff: noqa: ARG002

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.auth.jwt_verifier import AuthenticatedUser

from persona_api.errors import AuthenticationError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from fastapi import Request

__all__ = [
    "CloudOwnerResolver",
    "CommunityOwnerResolver",
    "OwnerResolver",
]


def _bearer_token(request: Request) -> str:
    """Extract the bearer token from the Authorization header, or 401."""
    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        raise AuthenticationError("missing or malformed Authorization header")
    token = header.removeprefix("Bearer ").strip()
    if not token:
        raise AuthenticationError("empty bearer token")
    return token


@runtime_checkable
class OwnerResolver(Protocol):
    """Resolve the authenticated owner of a request.

    The ``verify`` callable is the overridable JWT-verification seam (tests
    inject a fake verifier); community ignores it.
    """

    async def resolve(
        self,
        request: Request,
        verify: Callable[[str], Awaitable[AuthenticatedUser]],
    ) -> AuthenticatedUser:
        """Return the owner (id + identity) of the request."""
        ...


class CloudOwnerResolver:
    """Cloud: verify the bearer JWT → owner, JIT-provisioning the users row.

    This is the spec-08 behavior, unchanged. The bearer token is extracted and
    verified via the injected ``verify`` seam; the resolved user is upserted on
    the superuser ``admin_engine`` (a freshly authenticated provider user has no
    ``users`` row yet, and everything FKs ``users.id``).
    """

    async def resolve(
        self,
        request: Request,
        verify: Callable[[str], Awaitable[AuthenticatedUser]],
    ) -> AuthenticatedUser:
        token = _bearer_token(request)
        user = await verify(token)
        admin_engine = getattr(request.app.state, "admin_engine", None)
        if admin_engine is not None:
            from persona_api.services.user_service import ensure_user

            ensure_user(
                admin_engine,
                user_id=user.id,
                email=user.email,
                first_name=user.first_name,
                last_name=user.last_name,
            )
        return user


class CommunityOwnerResolver:
    """Community: a fixed single local owner — no JWT, no sign-in (D-33-3).

    The owner row is seeded at startup (D-33-X-owner-seed) so the app-table FKs
    hold; this resolver never touches the DB.
    """

    def __init__(self, *, owner_id: str, email: str) -> None:
        self._user = AuthenticatedUser(id=owner_id, email=email)

    async def resolve(
        self,
        request: Request,
        verify: Callable[[str], Awaitable[AuthenticatedUser]],
    ) -> AuthenticatedUser:
        return self._user
