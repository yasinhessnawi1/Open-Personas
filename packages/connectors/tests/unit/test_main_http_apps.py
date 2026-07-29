"""The connector service's HTTP-app assembly (R9-061).

The API proxies ``POST /v1/connectors/{platform}/link`` to the connector service.
Before this fix, Telegram (longpoll — the default transport), Discord (gateway — its
ONLY transport), and Slack (socket mode — the default transport) never built their
link/OAuth-callback ASGI app at all: ``_setup_discord``/``_setup_slack`` never called
``build_discord_app``/``build_slack_app`` anywhere, and ``_setup_telegram`` only built
its app in ``webhook`` mode. So the route didn't exist (404) whenever a platform's
OWN message-receiving transport wasn't HTTP.

This drives ``_setup_telegram`` / ``_setup_discord`` / ``_setup_slack`` — the exact
composition functions the service entry point (``_amain``) calls — directly, with
injected fakes (an ``httpx.MockTransport`` for the platform API + fakes for the
core/domain seams), and asserts the returned FastAPI app serves the link route (a
POST without auth → 401/400, NEVER 404) and, for Discord/Slack, the OAuth callback
route — even when the platform is configured on its non-HTTP outbound transport.
Also proves Slack's socket mode does NOT serve ``/slack/events`` (that stays
HTTP-transport-only, D-C3-2) while ``http`` transport mode does.
"""
# ruff: noqa: ARG001 — the fakes mirror real callable/handler signatures; unused params
# are intentional (the same posture as telegram/test_flow.py's fakes).

from __future__ import annotations

import asyncio
import contextlib
import urllib.parse
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from persona_connectors.__main__ import _setup_discord, _setup_slack, _setup_telegram, _supervised
from persona_connectors.config import ConnectorConfig
from pydantic import SecretStr

if TYPE_CHECKING:
    from collections.abc import Iterator

_TOKEN = SecretStr("test-bot-token")  # noqa: S105 — test literal
_NOW = datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)
_TTL = timedelta(minutes=15)


def _config(**overrides: object) -> ConnectorConfig:
    """A cloud-edition config with JWT verification + OAuth credentials wired.

    ``jwt_secret`` is required so ``make_jwt_verifier`` doesn't fail-fast at
    construction (D-08-4); the discord/slack OAuth creds are required by this
    fix's fail-fast validation (Discord/Slack account linking needs them).
    """
    defaults: dict[str, object] = {
        "edition": "cloud",
        "jwt_secret": "test-jwt-secret",  # noqa: S106 — test literal
        "discord_oauth_client_id": "discord-client-id",
        "discord_oauth_client_secret": "discord-client-secret",  # noqa: S106 — test literal
        "discord_oauth_redirect_uri": "https://example.test/discord/oauth/callback",
        "slack_oauth_client_id": "slack-client-id",
        "slack_oauth_client_secret": "slack-client-secret",  # noqa: S106 — test literal
        "slack_oauth_redirect_uri": "https://example.test/slack/oauth/callback",
    }
    defaults.update(overrides)
    return ConnectorConfig(**defaults)  # type: ignore[arg-type]


def _mock_client(handler: object) -> httpx.AsyncClient:
    transport = httpx.MockTransport(handler)  # type: ignore[arg-type]
    return httpx.AsyncClient(transport=transport)


async def _run_turn(request: object) -> str:
    return "ok"


def _list_persona_names(owner_id: str) -> dict[str, list[str]]:
    return {}


@contextlib.contextmanager
def _owner_scope(owner_id: str) -> Iterator[None]:
    yield


def _common_kwargs() -> dict[str, object]:
    """Fakes for the seams none of these route-existence assertions exercise.

    ``_setup_*`` only STORES ``linking_service`` / ``resolver`` / ``conversation_store``
    into connector/flow objects at setup time (never calls a method on them) — so plain
    ``MagicMock``s are sufficient; no inbound message ever flows through these tests.
    """
    return {
        "linking_service": MagicMock(),
        "resolver": MagicMock(),
        "conversation_store": MagicMock(),
        "list_persona_names": _list_persona_names,
        "run_turn": _run_turn,
        "owner_scope": _owner_scope,
    }


def _telegram_handler(request: httpx.Request) -> httpx.Response:
    # Answers both getMe (bot username resolution) and deleteWebhook/setWebhook.
    return httpx.Response(200, json={"ok": True, "result": {"username": "opbot", "id": "1"}})


def _discord_handler(request: httpx.Request) -> httpx.Response:
    # Answers GET /users/@me (bot id resolution).
    return httpx.Response(200, json={"id": "999", "username": "opbot"})


def _slack_handler(request: httpx.Request) -> httpx.Response:
    # Answers auth.test (bot user id resolution).
    return httpx.Response(200, json={"ok": True, "user_id": "U1", "team_id": "T1"})


# --- Telegram: longpoll (the DEFAULT transport) must still serve the link route ---


@pytest.mark.asyncio
async def test_telegram_longpoll_serves_the_link_route_not_404() -> None:
    http = _mock_client(_telegram_handler)
    config = _config(telegram_transport="longpoll")
    connector, app, runner = await _setup_telegram(
        config=config,
        token=_TOKEN,
        http=http,
        **_common_kwargs(),  # type: ignore[arg-type]
    )
    assert connector is not None
    assert runner is not None  # longpoll IS the message-receiving runner
    runner.close()  # never actually run the poll loop
    client = TestClient(app)
    resp = client.post("/v1/connectors/telegram/link")
    assert resp.status_code == 401  # missing bearer — the route EXISTS (was 404 pre-fix)
    await http.aclose()


@pytest.mark.asyncio
async def test_telegram_webhook_has_no_separate_runner_but_serves_the_link_route() -> None:
    http = _mock_client(_telegram_handler)
    config = _config(telegram_transport="webhook", telegram_webhook_url="https://example.test/hook")
    connector, app, runner = await _setup_telegram(
        config=config,
        token=_TOKEN,
        http=http,
        **_common_kwargs(),  # type: ignore[arg-type]
    )
    assert connector is not None
    # webhook mode: inbound delivery IS the HTTP app being served via http_apps — no
    # second, independently-served runner.
    assert runner is None
    client = TestClient(app)
    resp = client.post("/v1/connectors/telegram/link")
    assert resp.status_code == 401
    await http.aclose()


# --- Discord: gateway is its ONLY message transport (no HTTP alternative) ---


@pytest.mark.asyncio
async def test_discord_gateway_serves_the_link_and_oauth_callback_routes_not_404() -> None:
    http = _mock_client(_discord_handler)
    config = _config()
    connector, app, runner = await _setup_discord(
        config=config,
        token=_TOKEN,
        http=http,
        **_common_kwargs(),  # type: ignore[arg-type]
    )
    assert connector is not None
    runner.close()  # never open a real gateway websocket
    client = TestClient(app)
    link_resp = client.post("/v1/connectors/discord/link")
    assert link_resp.status_code == 401  # was 404 pre-fix (the app was never built)
    callback_resp = client.get("/discord/oauth/callback")
    assert callback_resp.status_code == 400  # missing code/state — route exists, not 404
    await http.aclose()


@pytest.mark.asyncio
async def test_discord_without_oauth_credentials_fails_fast() -> None:
    """No silent 404s: missing OAuth config raises at startup, not a broken route."""
    from persona_connectors.errors import ConnectorError

    http = _mock_client(_discord_handler)
    config = _config(discord_oauth_client_secret=None)
    with pytest.raises(ConnectorError):
        await _setup_discord(
            config=config,
            token=_TOKEN,
            http=http,
            **_common_kwargs(),  # type: ignore[arg-type]
        )
    await http.aclose()


# --- Slack: socket mode is the DEFAULT event transport ---


@pytest.mark.asyncio
async def test_slack_socket_mode_serves_link_and_callback_but_not_events() -> None:
    http = _mock_client(_slack_handler)
    config = _config(slack_transport="socket", slack_app_token="xapp-test")  # noqa: S106
    connector, app, runner = await _setup_slack(
        config=config,
        token=_TOKEN,
        http=http,
        **_common_kwargs(),  # type: ignore[arg-type]
    )
    assert connector is not None
    assert runner is not None  # socket mode IS the message-receiving runner
    runner.close()  # never open a real socket-mode websocket
    client = TestClient(app)
    link_resp = client.post("/v1/connectors/slack/link")
    assert link_resp.status_code == 401  # was 404 pre-fix (the app was never built)
    callback_resp = client.get("/slack/oauth/callback")
    assert callback_resp.status_code == 400  # missing code/state — route exists, not 404
    # D-C3-2: socket mode must NOT serve the HTTP-signed events route.
    events_resp = client.post("/slack/events", json={"type": "url_verification"})
    assert events_resp.status_code == 404
    await http.aclose()


@pytest.mark.asyncio
async def test_slack_setup_forwards_the_configured_scopes_into_the_authorize_url() -> None:
    """R9-067: ``_setup_slack`` must forward ``ConnectorConfig.slack_scope`` /
    ``slack_user_scope`` into the ``SlackLinkingService`` it builds — the CONFIGURED
    values, not the class defaults and not empty-by-accident (the original bug).

    A spy subclass captures the constructed instance so the actual authorize URL can be
    built from it and inspected, rather than trusting that the kwargs were merely passed.
    """
    from persona_connectors import __main__ as main_module

    captured: dict[str, object] = {}
    real_cls = main_module.slack_adapter.SlackLinkingService

    class _SpyLinkingService(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            captured["instance"] = self

    monkeypatch_target = main_module.slack_adapter
    original = monkeypatch_target.SlackLinkingService
    monkeypatch_target.SlackLinkingService = _SpyLinkingService  # type: ignore[misc]
    try:
        http = _mock_client(_slack_handler)
        config = _config(
            slack_transport="socket",
            slack_app_token="xapp-test",  # noqa: S106
            slack_scope="custom:scope,another:scope",
            slack_user_scope="custom.user.scope",
        )
        connector, app, runner = await _setup_slack(
            config=config,
            token=_TOKEN,
            http=http,
            **_common_kwargs(),  # type: ignore[arg-type]
        )
        assert connector is not None
        if runner is not None:
            runner.close()
        await http.aclose()
    finally:
        monkeypatch_target.SlackLinkingService = original  # type: ignore[misc]

    instance = captured["instance"]
    url = instance.issue_authorize_url(  # type: ignore[attr-defined]
        owner_id="owner-1", now=_NOW, ttl=_TTL
    )
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["scope"] == ["custom:scope,another:scope"]
    assert query["user_scope"] == ["custom.user.scope"]


@pytest.mark.asyncio
async def test_slack_with_bot_token_but_empty_scope_fails_fast() -> None:
    """R9-067: an empty ``PERSONA_CONNECTORS_SLACK_SCOPE`` can ONLY ever reproduce
    Slack's "No scopes requested" install rejection — refuse at startup, never let a
    broken install link reach a user.
    """
    from persona_connectors.errors import ConnectorError

    http = _mock_client(_slack_handler)
    config = _config(slack_transport="socket", slack_app_token="xapp-test", slack_scope="")  # noqa: S106
    with pytest.raises(ConnectorError):
        await _setup_slack(
            config=config,
            token=_TOKEN,
            http=http,
            **_common_kwargs(),  # type: ignore[arg-type]
        )
    await http.aclose()


@pytest.mark.asyncio
async def test_slack_http_transport_mounts_events_on_the_same_app_no_extra_runner() -> None:
    http = _mock_client(_slack_handler)
    config = _config(slack_transport="http", slack_signing_secret="signing-secret")  # noqa: S106
    connector, app, runner = await _setup_slack(
        config=config,
        token=_TOKEN,
        http=http,
        **_common_kwargs(),  # type: ignore[arg-type]
    )
    assert connector is not None
    # http mode: inbound events delivery IS the HTTP app being served via http_apps —
    # no second, independently-served runner.
    assert runner is None
    client = TestClient(app)
    link_resp = client.post("/v1/connectors/slack/link")
    assert link_resp.status_code == 401
    # A bad/missing signature → not 404 proves /slack/events IS mounted on this app.
    events_resp = client.post("/slack/events", json={"type": "url_verification"})
    assert events_resp.status_code != 404
    await http.aclose()


# --- _supervised (R9-071): one crashed platform runner must never kill the others ---


@pytest.mark.asyncio
async def test_supervised_contains_a_crashed_runner_instead_of_propagating() -> None:
    """``asyncio.gather(*runners)`` propagates the FIRST raised exception, which would
    end every OTHER runner (+ the HTTP server) along with the crashed one. ``_supervised``
    must swallow (and log) a crashed runner's exception so ``gather`` keeps waiting on the
    rest — proven directly here, and end-to-end via ``asyncio.gather`` below.
    """

    async def crashing_runner() -> None:
        raise RuntimeError("discord gateway blew up")

    await _supervised("discord", crashing_runner())  # must not raise


@pytest.mark.asyncio
async def test_gather_over_supervised_runners_lets_the_others_keep_serving() -> None:
    """One platform's runner crashing must not stop the others from running to
    completion under the SAME ``asyncio.gather`` call ``_amain`` uses.
    """
    other_ran = False

    async def crashing_runner() -> None:
        raise RuntimeError("discord gateway blew up")

    async def healthy_runner() -> None:
        nonlocal other_ran
        await asyncio.sleep(0)  # yield once, so both are genuinely concurrent
        other_ran = True

    # Must not raise — a crashed platform runner is contained, not propagated.
    await asyncio.gather(
        _supervised("discord", crashing_runner()),
        _supervised("telegram", healthy_runner()),
    )
    assert other_ran is True


@pytest.mark.asyncio
async def test_supervised_reraises_cancelled_error_for_clean_shutdown() -> None:
    """Normal shutdown (``asyncio.CancelledError``) must propagate untouched — the
    containment layer only swallows genuine crashes, never a deliberate cancellation.
    """

    async def cancelled_runner() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _supervised("discord", cancelled_runner())
