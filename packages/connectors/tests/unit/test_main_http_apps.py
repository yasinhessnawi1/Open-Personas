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
from persona_connectors.config import ConnectorConfig
from persona_connectors.service import _setup_discord, _setup_slack, _setup_telegram, _supervised
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
    # longpoll IS the message-receiving runner — a zero-arg FACTORY (R9-073c), never
    # called here, so there is no dangling coroutine to close.
    assert runner is not None
    assert callable(runner)
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
    # A zero-arg FACTORY (``gateway.run``, R9-073c) — never called here, so no real
    # gateway websocket ever opens and there is no coroutine to close.
    assert callable(runner)
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
    # socket mode IS the message-receiving runner — a zero-arg FACTORY (``socket.run``,
    # R9-073c), never called here, so no real socket-mode websocket ever opens.
    assert runner is not None
    assert callable(runner)
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
    from persona_connectors import service as service_module

    captured: dict[str, object] = {}
    real_cls = service_module.slack_adapter.SlackLinkingService

    class _SpyLinkingService(real_cls):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: object) -> None:
            super().__init__(**kwargs)  # type: ignore[arg-type]
            captured["instance"] = self

    monkeypatch_target = service_module.slack_adapter
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
        # A zero-arg FACTORY when present (R9-073c) — never called, nothing to close.
        assert runner is None or callable(runner)
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


# --- _supervised (R9-071 contain / R9-073c heal): a crashed runner restarts, with a
# bounded backoff, instead of leaving that platform dead for the process's lifetime ---


async def _no_sleep(_seconds: float) -> None:
    """An injected ``sleep`` that returns instantly — the backoff delay itself is not
    what these tests are proving; the RESTART + ceiling behaviour is."""


@pytest.mark.asyncio
async def test_supervised_contains_a_crashed_runner_instead_of_propagating() -> None:
    """``asyncio.gather(*runners)`` propagates the FIRST raised exception, which would
    end every OTHER runner (+ the HTTP server) along with the crashed one. ``_supervised``
    must swallow (and log) a crashed runner's exception so ``gather`` keeps waiting on the
    rest — proven directly here, and end-to-end via ``asyncio.gather`` below. The runner
    always fails, so this also exercises the ceiling (else the call never returns).
    """

    async def crashing_runner() -> None:
        raise RuntimeError("discord gateway blew up")

    # Must not raise — a crashed platform runner is contained, not propagated.
    await _supervised("discord", crashing_runner, sleep=_no_sleep)


@pytest.mark.asyncio
async def test_supervised_restarts_a_crashed_runner_then_it_recovers() -> None:
    """The FIRST crash is a RESTART, not a permanent death (R9-073c) — ``make_runner`` is
    called again; once it stops raising, ``_supervised`` returns cleanly (no ceiling hit).
    """
    calls = 0

    async def flaky_runner() -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("Model @cf/zai-org/glm-5.2 is not available")
        # the second attempt "recovers" (a real runner would now serve forever; a test
        # double just returns, which `_supervised` treats as a clean completion).

    await _supervised("telegram", flaky_runner, sleep=_no_sleep)
    assert calls == 2  # crashed once, restarted once, then stopped (no more restarts)


@pytest.mark.asyncio
async def test_supervised_backs_off_exponentially_and_stops_at_the_ceiling() -> None:
    """A PERSISTENTLY crashing runner is retried with exponential backoff (1s doubling to
    a 60s cap) up to the ceiling, then ``_supervised`` gives up (returns) rather than
    retrying forever — proven via the exact backoff schedule handed to the injected sleep.
    """
    calls = 0
    delays: list[float] = []

    async def crashing_runner() -> None:
        nonlocal calls
        calls += 1
        raise RuntimeError("permanently broken")

    async def _record_sleep(seconds: float) -> None:
        delays.append(seconds)

    await _supervised("discord", crashing_runner, sleep=_record_sleep)

    assert calls == 11  # the initial attempt + 10 restarts (the ceiling)
    assert delays == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0, 60.0, 60.0, 60.0, 60.0]


@pytest.mark.asyncio
async def test_supervised_resets_the_ceiling_after_a_healthy_stint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A runner that stayed up a good while before crashing again must NOT count toward
    the SAME ceiling as a hot-spinning one — the failure count resets after
    ``_RESTART_HEALTHY_UPTIME_SECONDS`` of uptime (an injected ``monotonic`` proves it
    without a real wait).

    The ceiling is monkeypatched down to 1 so the reset is OBSERVABLE purely from the
    call count: crash #1 is unhealthy (0s uptime) so the failure count is 1 afterwards;
    crash #2 is healthy (150s uptime) so, if the reset fires, the count drops back to 0
    (then 1) and a THIRD attempt happens — without the reset it would be 2, over the
    ceiling of 1, and ``_supervised`` would give up after only 2 calls.
    """
    from persona_connectors import service as service_module

    monkeypatch.setattr(service_module, "_RESTART_MAX_CONSECUTIVE_FAILURES", 1)

    calls = 0
    clock = iter([0.0, 1.0, 10.0, 160.0, 999.0])

    def fake_monotonic() -> float:
        return next(clock)

    async def flaky_runner() -> None:
        nonlocal calls
        calls += 1
        if calls <= 2:
            raise RuntimeError(f"crash #{calls}")
        # the third attempt "recovers" — a clean return, no more restarts.

    await _supervised("discord", flaky_runner, sleep=_no_sleep, monotonic=fake_monotonic)
    assert calls == 3  # the healthy-uptime reset let it try a third time, past ceiling=1


@pytest.mark.asyncio
async def test_gather_over_supervised_runners_lets_the_others_keep_serving() -> None:
    """One platform's runner crashing (and eventually being given up on) must not stop
    the others from running to completion under the SAME ``asyncio.gather`` call
    ``_amain`` uses.
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
        _supervised("discord", crashing_runner, sleep=_no_sleep),
        _supervised("telegram", healthy_runner),
    )
    assert other_ran is True


@pytest.mark.asyncio
async def test_supervised_reraises_cancelled_error_for_clean_shutdown() -> None:
    """Normal shutdown (``asyncio.CancelledError``) must propagate untouched — the
    containment/restart layer only ever swallows genuine crashes, never a deliberate
    cancellation, and it must not be retried like an ordinary crash.
    """

    async def cancelled_runner() -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await _supervised("discord", cancelled_runner)
