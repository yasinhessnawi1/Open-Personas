"""The account-linking lifecycle — pure owned-surface logic (Spec C1 T3, C1-D-5).

The security spine. A one-time token binds a platform identity to a Persona user;
thereafter every inbound resolves through a live *active* binding (and Spec 08
RLS scopes the rest). This module is the **port + pure logic** half:

- :class:`LinkToken` / :class:`LinkRecord` — frozen value types (the row shapes,
  minus any plaintext secret);
- :class:`LinkStore` — the persistence **Protocol** (the port); the concrete
  SQLAlchemy adapter lives in the api-coupled ``persona_connectors.infra`` (the
  C0 recorder pattern — Protocol-in-owned-surface, adapter-in-infra);
- :class:`LinkingService` — the trigger-agnostic orchestration (issue → redeem →
  bind → resolve → unlink), with the validation rules + the sha256-at-rest
  hashing. ``now``/``ttl`` are injected so the logic is pure + deterministic.

Owned surface — **api-free** (the decoupling guard enforces it). Uses only the
stdlib + persona-core's error base; never persona-api.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime  # noqa: TC003 — Pydantic needs runtime access
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, field_validator

from persona_connectors.errors import IdentityNotLinkedError, LinkTokenInvalidError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import timedelta

__all__ = [
    "LinkRecord",
    "LinkStore",
    "LinkToken",
    "LinkingService",
    "hash_token",
]

# A generous token entropy: token_urlsafe(32) → ~43 base64url chars, well under
# Telegram's 64-char deep-link payload limit, ~256 bits of entropy.
_TOKEN_BYTES = 32

TokenStatus = Literal["pending", "consumed", "expired"]
IdentityStatus = Literal["active", "revoked"]


def hash_token(plaintext: str) -> str:
    """Return the sha256 hex of a link token — the only form stored at rest.

    The plaintext is the bearer capability handed to the user; storing only its
    one-way hash means a DB leak yields no usable tokens (the BYO-Fernet posture).
    Redemption hashes the presented token and looks up by hash.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class LinkToken(BaseModel):
    """A one-time account-linking token row (the hash, never the plaintext)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    token_hash: str
    owner_id: str
    platform: str
    status: TokenStatus
    expires_at: datetime
    created_at: datetime
    consumed_at: datetime | None = None

    @field_validator("expires_at", "created_at", mode="after")
    @classmethod
    def _must_be_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "naive datetime not allowed on LinkToken"
            raise ValueError(msg)
        return value


class LinkRecord(BaseModel):
    """A platform-identity ↔ Persona-user binding row (the security spine)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    platform: str
    platform_identity: str
    owner_id: str
    status: IdentityStatus
    linked_at: datetime
    revoked_at: datetime | None = None

    @field_validator("linked_at", mode="after")
    @classmethod
    def _linked_at_must_be_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "naive datetime not allowed on LinkRecord.linked_at"
            raise ValueError(msg)
        return value


@runtime_checkable
class LinkStore(Protocol):
    """The persistence port for the linking lifecycle (the C0 recorder pattern).

    The concrete adapter (``persona_connectors.infra``) implements this over the
    ``connector_link_tokens`` / ``connector_identities`` tables: the owner-scoped
    writes/list run on the RLS engine; the pre-auth cross-tenant reads
    (:meth:`get_token_by_hash` / :meth:`get_active_identity`) run on the dispatch
    (BYPASSRLS) engine, keyed by the unguessable hash / the UNIQUE-active spine
    (the A0-worker pattern). ``@runtime_checkable`` so a composition root can
    assert the injected store satisfies the port.
    """

    def create_token(self, token: LinkToken) -> None:
        """Persist an issued (pending) token (owner-scoped write)."""
        ...

    def get_token_by_hash(self, token_hash: str) -> LinkToken | None:
        """Look a token up by its sha256 hash (pre-auth cross-tenant read)."""
        ...

    def consume_token(self, token_hash: str, *, now: datetime) -> None:
        """Mark a token consumed (single-use transition)."""
        ...

    def bind_identity(
        self, *, platform: str, platform_identity: str, owner_id: str, now: datetime
    ) -> None:
        """Create the active platform-identity ↔ owner binding."""
        ...

    def get_active_identity(self, *, platform: str, platform_identity: str) -> LinkRecord | None:
        """Resolve the live active binding for an inbound identity (pre-auth read)."""
        ...

    def revoke_identity(
        self, *, owner_id: str, platform: str, platform_identity: str, now: datetime
    ) -> None:
        """Sever a binding (status→revoked; the row remains for audit)."""
        ...


class LinkingService:
    """The trigger-agnostic account-linking orchestration (issue → redeem → bind →
    resolve → unlink).

    Holds no state beyond its injected :class:`LinkStore` (DI; no globals). All
    time is injected (``now``/``ttl``) so the logic is pure + deterministic.
    """

    def __init__(self, store: LinkStore) -> None:
        self._store = store

    def issue(
        self,
        *,
        owner_id: str,
        platform: str,
        now: datetime,
        ttl: timedelta,
        generate_code: Callable[[], str] | None = None,
    ) -> str:
        """Issue a one-time link token for ``owner_id`` on ``platform``.

        Generates an opaque bearer token, stores only its hash (pending, TTL'd),
        and returns the **plaintext** for the caller to hand to the user. The
        plaintext is never persisted (CQS: the write returns only the capability
        the caller must deliver, not stored data).

        ``generate_code`` lets a per-platform carrier inject the code **format**
        (C4 D-C4-X-c1-issue-codegen — the OTP carrier injects a short textable
        base32 code; the URL/OAuth-state carriers pass nothing). It is the *only*
        per-carrier variance: the hash-at-rest, single-use, TTL, and redeem
        lifecycle stay framework-owned. **Default (``None``) is byte-unchanged**:
        the prior high-entropy ``secrets.token_urlsafe(32)`` — Telegram/Discord/
        Slack are untouched.
        """
        plaintext = (
            secrets.token_urlsafe(_TOKEN_BYTES) if generate_code is None else generate_code()
        )
        self._store.create_token(
            LinkToken(
                token_hash=hash_token(plaintext),
                owner_id=owner_id,
                platform=platform,
                status="pending",
                expires_at=now + ttl,
                created_at=now,
            )
        )
        return plaintext

    def redeem_and_bind(
        self, *, plaintext_token: str, platform: str, platform_identity: str, now: datetime
    ) -> str:
        """Redeem a token and bind ``platform_identity`` to its owner.

        Validates the token (known, pending, unexpired, platform-matched), marks
        it consumed (single-use), creates the active binding, and returns the
        owner id. Raises :class:`~persona_connectors.errors.LinkTokenInvalidError`
        on any violation — a forged / stale / replayed / mismatched token never
        binds.
        """
        token = self._store.get_token_by_hash(hash_token(plaintext_token))
        if token is None:
            raise LinkTokenInvalidError("unknown link token", context={"platform": platform})
        if token.status != "pending":
            raise LinkTokenInvalidError(
                "link token already used", context={"platform": platform, "status": token.status}
            )
        if token.expires_at <= now:
            raise LinkTokenInvalidError("link token expired", context={"platform": platform})
        if token.platform != platform:
            raise LinkTokenInvalidError(
                "link token platform mismatch",
                context={"token_platform": token.platform, "presented": platform},
            )
        self._store.consume_token(token.token_hash, now=now)
        self._store.bind_identity(
            platform=platform,
            platform_identity=platform_identity,
            owner_id=token.owner_id,
            now=now,
        )
        return token.owner_id

    def resolve_owner(self, *, platform: str, platform_identity: str) -> str:
        """Resolve an inbound platform identity to its linked Persona-user owner.

        Raises :class:`~persona_connectors.errors.IdentityNotLinkedError` when no
        live active binding exists — the inbound gets a link-instruction and zero
        access, NEVER another user's personas (the security spine).
        """
        record = self._store.get_active_identity(
            platform=platform, platform_identity=platform_identity
        )
        if record is None:
            raise IdentityNotLinkedError(
                "no linked Persona user for this platform identity",
                context={"platform": platform},
            )
        return record.owner_id

    def unlink(
        self, *, owner_id: str, platform: str, platform_identity: str, now: datetime
    ) -> None:
        """Sever a binding (CQS: a write; returns no data). Re-link is allowed after."""
        self._store.revoke_identity(
            owner_id=owner_id, platform=platform, platform_identity=platform_identity, now=now
        )
