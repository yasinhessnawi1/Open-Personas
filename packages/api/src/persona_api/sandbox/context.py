"""Per-request sandbox context (spec 12 T10).

Threads the request's ``(owner_id, conversation_id)`` from
:func:`chat_service.stream_chat` into the runtime factory's toolbox-build step
WITHOUT changing every loop-builder signature in the codebase. ``contextvars``
is the right shape: async-safe per-request state that propagates through
``await`` boundaries.

**Why contextvars rather than a parameter:** the existing
``app.state.build_conversation_loop`` closure has signature
``(persona_id) -> Loop`` and is replaced by ~10 integration-test fixtures
with scripted loops. Widening the closure signature ripples through every
test; threading via contextvars keeps the boundary unchanged and the test
overrides untouched (their loops don't read the context).

The tool factory wraps the contextvar lookup in
:class:`SandboxRequestContext` so the session_id derivation
(``f"{owner_id}:{conversation_id}"`` per kickoff trip-up #6) is enforced in
one place — never reconstructed ad-hoc downstream.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass

__all__ = [
    "SandboxRequestContext",
    "get_sandbox_request_context",
    "reset_sandbox_request_context",
    "set_sandbox_request_context",
]


@dataclass(frozen=True, slots=True)
class SandboxRequestContext:
    """Per-request sandbox identity.

    Frozen + slots — the receipt is immutable; any state change is a new
    instance. The ``session_id`` property is the one true derivation
    (`{owner_id}:{conversation_id}`) — never recompute it ad-hoc.

    **T12 F-T12-INT-01 hardening (MEDIUM, STRUCTURAL-CLEAR):** the
    workflow audit found that an unescaped ``:`` separator in either field
    produces cross-tenant session_id collisions
    (e.g. ``("alice", "bob:c1")`` and ``("alice:bob", "c1")`` both derive
    to ``"alice:bob:c1"``; the pool's idempotent-on-session_id acquire would
    silently share the substrate IPython kernel between distinct tenants).
    Current Clerk owner_ids (``user_{alnum}``) and server-generated
    conversation_ids (``conv_{uuid.hex}``) never contain ``:``, so the v0.1
    attack path is gated by ingress invariants; this validator is a
    defense-in-depth fail-fast at the boundary so a future ingress change
    can't silently introduce cross-tenant data access.
    """

    owner_id: str
    conversation_id: str
    #: Whether the surface this turn runs on can show the person a generated image.
    #: A connector (Telegram, email, SMS...) sends only the reply text, so an image a
    #: persona makes there never reaches anyone. The chat image charge reads this and
    #: bills nothing on such a turn (R9-308): the owner ruled the house pays for those
    #: until a channel can deliver them. Set explicitly by the registry that runs the
    #: turn, never inferred from the ids.
    delivers_images: bool = True

    def __post_init__(self) -> None:
        # T12 F-T12-INT-01: kickoff trip-up #6 isolation guarantee is
        # load-bearing. Reject the separator character at the boundary so a
        # crafted ID can't collide cross-tenant via `f"{owner}:{conv}"`.
        if ":" in self.owner_id:
            msg = f"owner_id must not contain ':' (got {self.owner_id!r})"
            raise ValueError(msg)
        if ":" in self.conversation_id:
            msg = f"conversation_id must not contain ':' (got {self.conversation_id!r})"
            raise ValueError(msg)

    @property
    def session_id(self) -> str:
        """Tenant-scoped session id (spec 12 kickoff trip-up #6)."""
        return f"{self.owner_id}:{self.conversation_id}"


_CONTEXT: ContextVar[SandboxRequestContext | None] = ContextVar(
    "persona_api_sandbox_request_context", default=None
)


def set_sandbox_request_context(ctx: SandboxRequestContext) -> Token[SandboxRequestContext | None]:
    """Bind ``ctx`` for the current async context; return the reset token."""
    return _CONTEXT.set(ctx)


def get_sandbox_request_context() -> SandboxRequestContext | None:
    """Return the current request's sandbox context, or ``None`` outside a request."""
    return _CONTEXT.get()


def reset_sandbox_request_context(token: Token[SandboxRequestContext | None]) -> None:
    """Restore the prior context bound to ``token`` (use in a ``finally`` block)."""
    _CONTEXT.reset(token)
