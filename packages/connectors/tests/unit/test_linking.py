"""The account-linking lifecycle — pure domain logic (Spec C1 T3, C1-D-5).

The security spine, exercised against a FAKE in-memory LinkStore (no DB): the
one-time token is issued for an owner, redeemed by a platform identity (binding
it), and thereafter that identity resolves to exactly that owner. An unlinked
identity resolves to NOTHING (``IdentityNotLinkedError`` → link-instruction, zero
access). The plaintext token is never stored — only its sha256 hash. Link →
unlink → re-link works (the partial-active-unique allowance).

Owned surface — api-free; the LinkStore is a Protocol (port), here faked.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from persona_connectors.domain.linking import (
    LinkingService,
    LinkRecord,
    LinkStore,
    LinkToken,
    hash_token,
)
from persona_connectors.errors import IdentityNotLinkedError, LinkTokenInvalidError

_NOW = datetime(2026, 6, 23, 12, 0, tzinfo=UTC)
_TTL = timedelta(minutes=15)


class _FakeLinkStore:
    """In-memory LinkStore (the persistence port faked for the pure-logic tests)."""

    def __init__(self) -> None:
        self.tokens: dict[str, LinkToken] = {}
        self.identities: list[LinkRecord] = []

    def create_token(self, token: LinkToken) -> None:
        self.tokens[token.token_hash] = token

    def get_token_by_hash(self, token_hash: str) -> LinkToken | None:
        return self.tokens.get(token_hash)

    def consume_token(self, token_hash: str, *, now: datetime) -> None:
        tok = self.tokens[token_hash]
        self.tokens[token_hash] = tok.model_copy(update={"status": "consumed", "consumed_at": now})

    def bind_identity(
        self, *, platform: str, platform_identity: str, owner_id: str, now: datetime
    ) -> None:
        self.identities.append(
            LinkRecord(
                platform=platform,
                platform_identity=platform_identity,
                owner_id=owner_id,
                status="active",
                linked_at=now,
            )
        )

    def get_active_identity(self, *, platform: str, platform_identity: str) -> LinkRecord | None:
        for rec in self.identities:
            if (
                rec.platform == platform
                and rec.platform_identity == platform_identity
                and rec.status == "active"
            ):
                return rec
        return None

    def revoke_identity(
        self, *, owner_id: str, platform: str, platform_identity: str, now: datetime
    ) -> None:
        for i, rec in enumerate(self.identities):
            if (
                rec.owner_id == owner_id
                and rec.platform == platform
                and rec.platform_identity == platform_identity
                and rec.status == "active"
            ):
                self.identities[i] = rec.model_copy(update={"status": "revoked", "revoked_at": now})


def _service() -> tuple[LinkingService, _FakeLinkStore]:
    store = _FakeLinkStore()
    return LinkingService(store), store


def test_fake_store_satisfies_the_link_store_protocol() -> None:
    """@runtime_checkable port: the infra adapter (and this fake) satisfy LinkStore."""
    assert isinstance(_FakeLinkStore(), LinkStore)


def test_token_is_stored_hashed_never_plaintext() -> None:
    """The bearer token is returned to the caller; only its sha256 hex is stored."""
    svc, store = _service()
    plaintext = svc.issue(owner_id="u1", platform="telegram", now=_NOW, ttl=_TTL)
    assert plaintext  # a real opaque token handed back
    assert plaintext not in store.tokens  # never keyed by plaintext
    assert hash_token(plaintext) in store.tokens  # only the hash is at rest
    assert store.tokens[hash_token(plaintext)].owner_id == "u1"


def test_issue_uses_injected_code_generator() -> None:
    """C4 D-C4-X-c1-issue-codegen: a carrier may inject the code FORMAT (a short
    textable OTP for SMS/WhatsApp); the lifecycle (hash-at-rest, single-use, the
    redeem path) is unchanged — only the generated string differs."""
    svc, store = _service()
    plaintext = svc.issue(
        owner_id="u1", platform="whatsapp", now=_NOW, ttl=_TTL, generate_code=lambda: "ABCD2345"
    )
    assert plaintext == "ABCD2345"  # the injected code is handed back verbatim
    assert "ABCD2345" not in store.tokens  # still keyed by the hash, never plaintext
    assert hash_token("ABCD2345") in store.tokens
    # and it redeems through the unchanged mechanism-agnostic path
    owner = svc.redeem_and_bind(
        plaintext_token="ABCD2345",
        platform="whatsapp",
        platform_identity="+15551230000",
        now=_NOW,
    )
    assert owner == "u1"


def test_issue_default_generator_is_the_unchanged_urlsafe_token() -> None:
    """The additive proof: with NO generate_code, issue() is byte-for-byte the prior
    behaviour — a high-entropy token_urlsafe(32) (~43 url-safe chars)."""
    svc, store = _service()
    plaintext = svc.issue(owner_id="u1", platform="telegram", now=_NOW, ttl=_TTL)
    assert len(plaintext) >= 40  # token_urlsafe(32) → ~43 chars; never a short code by default
    assert hash_token(plaintext) in store.tokens


def test_redeem_binds_identity_and_returns_owner() -> None:
    """A platform identity redeems the token → bound to the issuing owner."""
    svc, _ = _service()
    plaintext = svc.issue(owner_id="u1", platform="telegram", now=_NOW, ttl=_TTL)
    owner = svc.redeem_and_bind(
        plaintext_token=plaintext, platform="telegram", platform_identity="tg-42", now=_NOW
    )
    assert owner == "u1"
    assert svc.resolve_owner(platform="telegram", platform_identity="tg-42") == "u1"


def test_resolve_unlinked_identity_is_zero_access() -> None:
    """The load-bearing invariant: no live binding → IdentityNotLinkedError (the
    inbound gets a link-instruction, NEVER another user's personas)."""
    svc, _ = _service()
    with pytest.raises(IdentityNotLinkedError):
        svc.resolve_owner(platform="telegram", platform_identity="stranger")


def test_token_is_single_use() -> None:
    """A consumed token cannot be redeemed again (replay defence)."""
    svc, _ = _service()
    plaintext = svc.issue(owner_id="u1", platform="telegram", now=_NOW, ttl=_TTL)
    svc.redeem_and_bind(
        plaintext_token=plaintext, platform="telegram", platform_identity="tg-1", now=_NOW
    )
    with pytest.raises(LinkTokenInvalidError):
        svc.redeem_and_bind(
            plaintext_token=plaintext, platform="telegram", platform_identity="tg-2", now=_NOW
        )


def test_expired_token_is_rejected() -> None:
    """A token past its TTL cannot be redeemed."""
    svc, _ = _service()
    plaintext = svc.issue(owner_id="u1", platform="telegram", now=_NOW, ttl=_TTL)
    later = _NOW + timedelta(hours=1)
    with pytest.raises(LinkTokenInvalidError):
        svc.redeem_and_bind(
            plaintext_token=plaintext, platform="telegram", platform_identity="tg-1", now=later
        )


def test_unknown_token_is_rejected() -> None:
    """A token that was never issued cannot bind anything."""
    svc, _ = _service()
    with pytest.raises(LinkTokenInvalidError):
        svc.redeem_and_bind(
            plaintext_token="forged", platform="telegram", platform_identity="tg-1", now=_NOW
        )


def test_token_platform_mismatch_is_rejected() -> None:
    """A token issued for one platform cannot bind an identity on another."""
    svc, _ = _service()
    plaintext = svc.issue(owner_id="u1", platform="telegram", now=_NOW, ttl=_TTL)
    with pytest.raises(LinkTokenInvalidError):
        svc.redeem_and_bind(
            plaintext_token=plaintext, platform="discord", platform_identity="dc-1", now=_NOW
        )


def test_link_unlink_relink_cycle() -> None:
    """Unlink severs access; a fresh token re-links (the partial-active allowance)."""
    svc, _ = _service()
    p1 = svc.issue(owner_id="u1", platform="telegram", now=_NOW, ttl=_TTL)
    svc.redeem_and_bind(plaintext_token=p1, platform="telegram", platform_identity="tg-1", now=_NOW)
    svc.unlink(owner_id="u1", platform="telegram", platform_identity="tg-1", now=_NOW)
    with pytest.raises(IdentityNotLinkedError):
        svc.resolve_owner(platform="telegram", platform_identity="tg-1")
    # Re-link works (revoked row stays for audit; a new active binding is allowed).
    p2 = svc.issue(owner_id="u1", platform="telegram", now=_NOW, ttl=_TTL)
    svc.redeem_and_bind(plaintext_token=p2, platform="telegram", platform_identity="tg-1", now=_NOW)
    assert svc.resolve_owner(platform="telegram", platform_identity="tg-1") == "u1"


def test_hash_token_is_sha256_hex_and_deterministic() -> None:
    """The at-rest hash is sha256 hex (stable, one-way)."""
    import hashlib

    assert hash_token("abc") == hashlib.sha256(b"abc").hexdigest()
    assert hash_token("abc") == hash_token("abc")
    assert hash_token("abc") != hash_token("abd")
