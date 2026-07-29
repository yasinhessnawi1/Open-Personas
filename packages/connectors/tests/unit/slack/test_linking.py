"""Slack OAuth v2 linking (Spec C3 ⛔) — binding-symmetry across ALL THREE + state-integrity.

The third carrier, tested against the **real** C1 ``LinkingService`` + ``InboundIdentityResolver``
(only persistence faked). Two non-negotiables:

1. **Binding-shape symmetry across all three** — Telegram (``/start``), Discord (OAuth), and
   Slack (OAuth) bindings all go through the same C1 ``redeem_and_bind`` → ``bind_identity``,
   so each resolves through the shared ``resolver.resolve`` identically.
2. **OAuth-state integrity** — tampered / replayed / expired / wrong-platform ``state`` →
   refusal, never a bind.

C1 is unchanged again → the third clean validation of the C1-D-5 linking abstraction (C3-R-3).
"""

from __future__ import annotations

# ruff: noqa: ARG001, ARG002 — fakes/handlers mirror protocol signatures; some args unused.
import urllib.parse
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from persona_connectors.domain.linking import LinkingService, LinkRecord, LinkToken
from persona_connectors.domain.normalise import NormalisedInbound
from persona_connectors.domain.resolution import (
    InboundIdentityResolver,
    ResolvedIdentity,
    UnlinkedIdentity,
)
from persona_connectors.errors import LinkTokenInvalidError, SlackApiError
from persona_connectors.slack.linking import (
    SlackLinkingService,
    SlackOAuthClient,
    build_authorize_url,
)
from pydantic import SecretStr

_NOW = datetime(2026, 6, 27, 12, 0, 0, tzinfo=UTC)
_TTL = timedelta(minutes=15)
_OWNER = "owner-A"
_SLACK_ID = "U777"
_SECRET = "slack-oauth-secret.zzz"  # noqa: S105 — test literal


class _FakeLinkStore:
    """An in-memory ``LinkStore`` — the real lifecycle/validation runs over it."""

    def __init__(self) -> None:
        self.tokens: dict[str, LinkToken] = {}
        self.identities: dict[tuple[str, str], LinkRecord] = {}

    def create_token(self, token: LinkToken) -> None:
        self.tokens[token.token_hash] = token

    def get_token_by_hash(self, token_hash: str) -> LinkToken | None:
        return self.tokens.get(token_hash)

    def consume_token(self, token_hash: str, *, now: datetime) -> None:
        token = self.tokens[token_hash]
        self.tokens[token_hash] = token.model_copy(
            update={"status": "consumed", "consumed_at": now}
        )

    def bind_identity(
        self, *, platform: str, platform_identity: str, owner_id: str, now: datetime
    ) -> None:
        self.identities[(platform, platform_identity)] = LinkRecord(
            platform=platform,
            platform_identity=platform_identity,
            owner_id=owner_id,
            status="active",
            linked_at=now,
        )

    def get_active_identity(self, *, platform: str, platform_identity: str) -> LinkRecord | None:
        record = self.identities.get((platform, platform_identity))
        return record if record is not None and record.status == "active" else None

    def revoke_identity(
        self, *, owner_id: str, platform: str, platform_identity: str, now: datetime
    ) -> None:
        key = (platform, platform_identity)
        record = self.identities.get(key)
        if record is not None:
            self.identities[key] = record.model_copy(
                update={"status": "revoked", "revoked_at": now}
            )


class _FakeOAuth:
    def __init__(self, user_id: str = _SLACK_ID) -> None:
        self.user_id = user_id

    async def resolve_user_id(self, code: str) -> str:
        return self.user_id


def _service(store: _FakeLinkStore) -> tuple[SlackLinkingService, LinkingService]:
    linking = LinkingService(store)
    slack = SlackLinkingService(
        linking=linking,
        oauth=_FakeOAuth(),
        client_id="client-1",
        redirect_uri="https://app.test/slack/oauth/callback",
        scope="im:history,chat:write",
    )
    return slack, linking


def _state_of(url: str) -> str:
    return urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]


def _inbound(platform: str, sender_id: str) -> NormalisedInbound:
    return NormalisedInbound(
        platform=platform,
        sender_id=sender_id,
        conversation_key="c",
        message_id="m",
        text="hi",
        received_at=_NOW,
    )


# --- the authorize URL carrier ---


def test_authorize_url_carries_state_scope_and_redirect() -> None:
    url = build_authorize_url(
        client_id="client-1",
        redirect_uri="https://app.test/cb",
        state="STATE123",
        scope="im:history,chat:write",
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert url.startswith("https://slack.com/oauth/v2/authorize?")
    assert query["client_id"] == ["client-1"]
    assert query["state"] == ["STATE123"]
    assert query["scope"] == ["im:history,chat:write"]
    assert query["redirect_uri"] == ["https://app.test/cb"]


def test_authorize_url_carries_user_scope_when_provided() -> None:
    """R9-067: ``user_scope`` rides the authorize URL exactly like ``scope`` does."""
    url = build_authorize_url(
        client_id="client-1",
        redirect_uri="https://app.test/cb",
        state="STATE123",
        scope="im:history,chat:write,im:write",
        user_scope="identity.basic",
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["scope"] == ["im:history,chat:write,im:write"]
    assert query["user_scope"] == ["identity.basic"]


def test_authorize_url_omits_scope_and_user_scope_when_empty() -> None:
    """R9-067's bug, pinned: the (pre-fix) no-argument call must still omit both —
    this is the exact call shape that reproduced Slack's "No scopes requested" reject.
    """
    url = build_authorize_url(
        client_id="client-1", redirect_uri="https://app.test/cb", state="STATE123"
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert "scope" not in query
    assert "user_scope" not in query


def test_issue_authorize_url_carries_the_service_configured_scopes() -> None:
    """The service (as ``__main__._setup_slack`` builds it) forwards its OWN configured
    ``scope``/``user_scope`` into every issued authorize URL — not empty defaults-by-accident.
    """
    store = _FakeLinkStore()
    linking = LinkingService(store)
    slack = SlackLinkingService(
        linking=linking,
        oauth=_FakeOAuth(),
        client_id="client-1",
        redirect_uri="https://app.test/slack/oauth/callback",
        scope="im:history,chat:write,im:write",
        user_scope="identity.basic",
    )
    url = slack.issue_authorize_url(owner_id=_OWNER, now=_NOW, ttl=_TTL)
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["scope"] == ["im:history,chat:write,im:write"]
    assert query["user_scope"] == ["identity.basic"]


# --- binding-shape symmetry across ALL THREE carriers ---


@pytest.mark.asyncio
async def test_all_three_carriers_resolve_identically() -> None:
    """Telegram /start ≡ Discord OAuth ≡ Slack OAuth — all bind via the same C1 path."""
    store = _FakeLinkStore()
    slack, linking = _service(store)
    resolver = InboundIdentityResolver(linking)  # the REAL shared resolution gate

    # Slack: the out-of-band OAuth callback writes the binding.
    url = slack.issue_authorize_url(owner_id=_OWNER, now=_NOW, ttl=_TTL)
    assert await slack.complete_oauth(code="code-1", state=_state_of(url), now=_NOW) == _OWNER

    # Discord + Telegram: bind via the SAME C1 redeem_and_bind (simulating their carriers).
    for platform, token_platform, identity in (
        ("discord", "discord", "discord-1"),
        ("telegram", "telegram", "tg-1"),
    ):
        token = linking.issue(owner_id=_OWNER, platform=token_platform, now=_NOW, ttl=_TTL)
        linking.redeem_and_bind(
            plaintext_token=token, platform=platform, platform_identity=identity, now=_NOW
        )

    # The SHARED resolver resolves all three to the same owner, all ResolvedIdentity.
    resolutions = [
        resolver.resolve(_inbound("slack", _SLACK_ID)),
        resolver.resolve(_inbound("discord", "discord-1")),
        resolver.resolve(_inbound("telegram", "tg-1")),
    ]
    assert all(isinstance(r, ResolvedIdentity) and r.owner_id == _OWNER for r in resolutions)
    # Identical record shape across all three (only platform/identity differ).
    assert {rec.status for rec in store.identities.values()} == {"active"}
    assert {rec.owner_id for rec in store.identities.values()} == {_OWNER}


# --- OAuth-state integrity (every attack → refusal) ---


@pytest.mark.asyncio
async def test_tampered_state_refuses_to_bind() -> None:
    store = _FakeLinkStore()
    slack, linking = _service(store)
    with pytest.raises(LinkTokenInvalidError):
        await slack.complete_oauth(code="code-1", state="forged-xyz", now=_NOW)
    assert store.identities == {}
    resolution = InboundIdentityResolver(linking).resolve(_inbound("slack", _SLACK_ID))
    assert isinstance(resolution, UnlinkedIdentity)


@pytest.mark.asyncio
async def test_replayed_state_refuses_the_second_bind() -> None:
    store = _FakeLinkStore()
    slack, _ = _service(store)
    state = _state_of(slack.issue_authorize_url(owner_id=_OWNER, now=_NOW, ttl=_TTL))
    await slack.complete_oauth(code="code-1", state=state, now=_NOW)
    with pytest.raises(LinkTokenInvalidError):
        await slack.complete_oauth(code="code-2", state=state, now=_NOW)
    assert len(store.identities) == 1


@pytest.mark.asyncio
async def test_expired_state_refuses_to_bind() -> None:
    store = _FakeLinkStore()
    slack, _ = _service(store)
    state = _state_of(slack.issue_authorize_url(owner_id=_OWNER, now=_NOW, ttl=_TTL))
    with pytest.raises(LinkTokenInvalidError):
        await slack.complete_oauth(code="c", state=state, now=_NOW + _TTL + timedelta(seconds=1))
    assert store.identities == {}


@pytest.mark.asyncio
async def test_wrong_platform_state_refuses_to_bind() -> None:
    """A Discord-issued token presented on the Slack callback is platform-mismatched → refused."""
    store = _FakeLinkStore()
    slack, linking = _service(store)
    discord_state = linking.issue(owner_id=_OWNER, platform="discord", now=_NOW, ttl=_TTL)
    with pytest.raises(LinkTokenInvalidError):
        await slack.complete_oauth(code="c", state=discord_state, now=_NOW)
    assert store.identities == {}


# --- the OAuth I/O client (over httpx MockTransport) ---


def _oauth_client(handler: object) -> SlackOAuthClient:
    return SlackOAuthClient(
        client_id="client-1",
        client_secret=SecretStr(_SECRET),
        redirect_uri="https://app.test/cb",
        http=httpx.AsyncClient(transport=httpx.MockTransport(handler)),  # type: ignore[arg-type]
        api_base_url="https://slack.test/api",
    )


@pytest.mark.asyncio
async def test_resolve_user_id_reads_authed_user_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/oauth.v2.access"
        return httpx.Response(
            200, json={"ok": True, "authed_user": {"id": "U777"}, "team": {"id": "T1"}}
        )

    assert await _oauth_client(handler).resolve_user_id("the-code") == "U777"


@pytest.mark.asyncio
async def test_ok_false_exchange_is_a_domain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "error": "invalid_code"})

    with pytest.raises(SlackApiError):
        await _oauth_client(handler).resolve_user_id("bad")


@pytest.mark.asyncio
async def test_missing_authed_user_is_a_domain_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "team": {"id": "T1"}})  # no authed_user

    with pytest.raises(SlackApiError):
        await _oauth_client(handler).resolve_user_id("code")


@pytest.mark.asyncio
async def test_client_secret_never_leaks_on_a_transport_fault() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("boom")

    with pytest.raises(SlackApiError) as exc:
        await _oauth_client(handler).resolve_user_id("code")
    assert _SECRET not in str(exc.value)
    assert all(_SECRET not in v for v in exc.value.context.values())
    assert exc.value.__cause__ is None
