"""The Spec I1 T1 extraction: ``build_connectors`` composes what ``_amain`` used to.

The composition body moved from ``__main__`` to :mod:`persona_connectors.service` so the
api can import it and host the same runners in-process (D-I1-9). A refactor that merely
compiles is not a refactor that preserved behaviour, so these tests pin the three things
that could silently drift:

1. the SAME set of runner names is wired for a given config (the extraction is behaviour
   preserving, not "close enough");
2. the bundle carries the transport runners only, never the HTTP server, because deciding
   how to serve HTTP is the HOST's business (D-I1-15) and the embedded api mounts instead;
3. ``__main__`` stays a thin launcher that imports the builder rather than owning it.

The heavy collaborators (engines, ``RuntimeFactory``, credits policy, job queue) arrive by
injection, which is what makes this testable at all: no DB, no torch, no live socket.
"""
# ruff: noqa: ARG001, ARG002 — the fakes mirror real callable/handler signatures.

from __future__ import annotations

import pathlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from persona_connectors import service
from persona_connectors.config import ConnectorConfig

if TYPE_CHECKING:
    from persona_connectors.service import ConnectorsBundle

_PLATFORM_API_RESPONSES = {
    "getMe": {"ok": True, "result": {"username": "opbot", "id": "1"}},
    "users/@me": {"id": "999", "username": "opbot"},
    "auth.test": {"ok": True, "user_id": "U1", "team_id": "T1"},
}


def _handler(request: httpx.Request) -> httpx.Response:
    """One transport answering every platform's identity probe at setup."""
    url = str(request.url)
    for marker, payload in _PLATFORM_API_RESPONSES.items():
        if marker in url:
            return httpx.Response(200, json=payload)
    return httpx.Response(200, json={"ok": True, "result": {"username": "opbot", "id": "1"}})


def _config(**overrides: object) -> ConnectorConfig:
    """A cloud config with the credentials each platform's setup fails fast without."""
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


async def _build(config: ConnectorConfig, http: httpx.AsyncClient) -> ConnectorsBundle:
    """Drive the REAL ``build_connectors`` with every heavy collaborator injected."""
    return await service.build_connectors(
        connector_config=config,
        api_config=MagicMock(),
        rls_engine=MagicMock(),
        dispatch_engine=MagicMock(),
        runtime_factory=MagicMock(),
        credits_policy=MagicMock(),
        job_queue=MagicMock(),
        stripe_gateway=None,
        http=http,
    )


@pytest.mark.asyncio
async def test_build_connectors_wires_the_same_runner_set_the_entry_point_gathered() -> None:
    """The extraction is behaviour preserving: same platforms in, same runners out.

    Pre-extraction ``_amain`` appended ``_supervised("telegram"|"discord"|"slack", …)`` for
    exactly these transports (Telegram only in longpoll, Slack only in socket mode, Discord
    always) and NEVER a runner for the phone/email channels, whose inbound arrives over the
    shared HTTP app. This asserts the whole set at once, so dropping or gaining one fails.
    """
    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    config = _config(
        telegram_bot_token="tg-token",  # noqa: S106 — test literal
        discord_bot_token="dc-token",  # noqa: S106 — test literal
        slack_bot_token="sl-token",  # noqa: S106 — test literal
        slack_app_token="xapp-test",  # noqa: S106 — test literal
        twilio_account_sid="AC123",
        twilio_auth_token="tw-token",  # noqa: S106 — test literal
        twilio_whatsapp_from="whatsapp:+15550001111",
        twilio_sms_from="+15550002222",
    )
    bundle = await _build(config, http)

    assert set(bundle.runners) == {"telegram", "discord", "slack"}
    # Every configured platform still contributes a deliverer, including the two that
    # contribute no runner (R9-061: their inbound rides the shared HTTP app).
    assert set(bundle.deliverers) == {"telegram", "discord", "slack", "whatsapp", "sms"}
    assert all(callable(factory) for factory in bundle.runners.values())
    await http.aclose()


@pytest.mark.asyncio
async def test_the_bundle_carries_no_http_runner_because_serving_is_the_hosts_choice() -> None:
    """D-I1-15: the bundle exposes the merged app, never a runner that serves it.

    ``_amain`` appended ``_supervised("http", _serve_app(parent, port=8080))``. That moved
    to ``run_standalone``, because the embedded api MOUNTS these routes into its own app on
    its own port and must start no second uvicorn. If the bundle ever carried an "http"
    runner again, the embedded host would silently open a second public ingress, which is
    Option B and was not chosen.
    """
    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    bundle = await _build(_config(telegram_bot_token="tg-token"), http)  # noqa: S106

    assert "http" not in bundle.runners
    assert bundle.http_app is not None
    # The merged app is real and serves the link route the web front-door calls.
    client = TestClient(bundle.http_app)
    assert client.post("/v1/connectors/telegram/link").status_code == 401
    await http.aclose()


@pytest.mark.asyncio
async def test_no_configured_platform_yields_an_empty_bundle_rather_than_raising() -> None:
    """D-I1-6: the builder reports absence; the HOST decides whether absence is fatal.

    Standalone still raises (the next test), but the embedded api must be able to boot,
    warn and keep serving the web app, so the raise cannot live in the shared builder.
    """
    http = httpx.AsyncClient(transport=httpx.MockTransport(_handler))
    bundle = await _build(_config(), http)

    assert bundle.deliverers == {}
    assert bundle.runners == {}
    assert bundle.http_app is None
    assert bundle.idle_sweep is None
    await http.aclose()


def test_standalone_still_refuses_a_boot_with_no_connector_configured() -> None:
    """The standalone entry keeps its pre-extraction fail-fast, in its own module now.

    Read from source rather than executed: running it would build real engines and the
    real torch-backed ``RuntimeFactory``. The assertion is that the raise is on the
    standalone path specifically, which is what makes D-I1-6's split real.
    """
    source = (pathlib.Path(service.__file__)).read_text(encoding="utf-8")
    standalone = source.split("async def run_standalone(", 1)[1]
    assert "no connector configured" in standalone
    assert "raise ConnectorError" in standalone


def test_dunder_main_is_a_launcher_that_owns_no_composition() -> None:
    """D-I1-9: library code must never import a ``__main__``, so nothing may live there.

    A grep guard in the spirit of A10-D-9. If composition drifts back into ``__main__``,
    the api's import path would be the thing that breaks, and it would break in the
    lifespan of a deployed process rather than here.
    """
    entry = pathlib.Path(service.__file__).parent / "__main__.py"
    source = entry.read_text(encoding="utf-8")

    assert "from persona_connectors.service import run_standalone" in source
    for owned_by_service in ("build_connectors", "_setup_telegram", "ConnectorConfig("):
        assert owned_by_service not in source, (
            f"{owned_by_service!r} is back in __main__; composition belongs in service.py"
        )


# --- R9-123 / D-I1-18: the eager RLS role verify actually runs at startup ---


def test_run_standalone_verifies_the_rls_role_before_it_composes_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``make_engine`` only ATTACHES the guard; something must force the first connect.

    ``guard_rls_engine_role`` is a ``first_connect`` listener, so without an eager
    verification the role check fires on whatever connects first, which in production is
    the first inbound message: a misconfigured secret would be discovered by a user being
    served another tenant's roster (R9-123), not by the deploy.

    This drives the REAL ``run_standalone`` rather than grepping for the call. The heavy
    collaborators are replaced so no engine, torch runtime or socket is built, and the
    function is allowed to run on to its genuine "no connector configured" refusal. Two
    things are asserted: that the verify ran at all, and that it ran on the RLS engine
    BEFORE composition got as far as that refusal.
    """
    import asyncio

    from persona_connectors.composition import ConnectorComposition
    from persona_connectors.errors import ConnectorError

    order: list[str] = []
    rls_engine = MagicMock(name="rls_engine")
    dispatch_engine = MagicMock(name="dispatch_engine")

    monkeypatch.setattr(ConnectorComposition, "make_engine", lambda _self: rls_engine)
    monkeypatch.setattr(ConnectorComposition, "make_dispatch_engine", lambda _self: dispatch_engine)
    monkeypatch.setattr(service, "build_credits_policy", lambda _cfg: MagicMock())
    monkeypatch.setattr(service, "build_stripe_gateway", lambda _cfg: None)
    monkeypatch.setattr(service, "JobQueue", lambda _engine: MagicMock())
    monkeypatch.setattr(
        service, "_build_runtime_factory", lambda *_a, **_k: MagicMock(name="runtime_factory")
    )

    def _spy_verify(engine: object) -> None:
        order.append("verify")
        assert engine is rls_engine, "the verify must run on the OWNER-SCOPED engine"

    monkeypatch.setattr(service, "verify_rls_engine_role", _spy_verify)

    original_build = service.build_connectors

    async def _spy_build(**kwargs: object) -> object:
        order.append("build_connectors")
        return await original_build(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(service, "build_connectors", _spy_build)

    # No platform is configured, so the real refusal is the end of the run. Reaching it
    # proves startup got past the verify rather than skipping it.
    with pytest.raises(ConnectorError, match="no connector configured"):
        asyncio.run(service.run_standalone())

    assert order == ["verify", "build_connectors"], (
        f"the RLS role verify must run at startup, before composition; saw {order}"
    )


# --- T2a: a clean stop must READ as a clean stop (no traceback on deploy) ---


def test_a_deploy_signal_stops_the_service_without_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fly sends SIGINT on deploy; that must not surface as a crash.

    ``kill_signal = "SIGINT"`` raises ``KeyboardInterrupt`` on the main thread. Because it
    surfaces inside a Task it goes to the EVENT LOOP, not to the awaiting coroutine, so it
    comes back out of ``asyncio.run`` chained to the runners' ``CancelledError`` and the
    process exits on a traceback for an ordinary, successful stop.

    This drives the REAL entry point with the REAL signal exception raised out of a real
    runner, rather than asserting a handler exists. Writing it the other way round is what
    found the first attempt at this fix: catching ``KeyboardInterrupt`` inside
    ``run_standalone`` compiles, reads correctly, and never fires. Two things are pinned:
    ``main()`` returns rather than propagating, and the ``finally`` teardown still ran, so
    the quiet exit is not quiet because it skipped cleanup.
    """
    from persona_connectors import __main__ as entry
    from persona_connectors.composition import ConnectorComposition
    from persona_connectors.service import ConnectorsBundle

    closed: list[str] = []

    class _RecordingClient:
        async def aclose(self) -> None:
            closed.append("http")

    monkeypatch.setattr(ConnectorComposition, "make_engine", lambda _self: MagicMock())
    monkeypatch.setattr(ConnectorComposition, "make_dispatch_engine", lambda _self: MagicMock())
    monkeypatch.setattr(service, "build_credits_policy", lambda _cfg: MagicMock())
    monkeypatch.setattr(service, "build_stripe_gateway", lambda _cfg: None)
    monkeypatch.setattr(service, "JobQueue", lambda _engine: MagicMock())
    monkeypatch.setattr(service, "_build_runtime_factory", lambda *_a, **_k: MagicMock())
    monkeypatch.setattr(service, "verify_rls_engine_role", lambda _engine: None)
    monkeypatch.setattr(service.httpx, "AsyncClient", lambda **_k: _RecordingClient())

    async def _runner() -> None:
        raise KeyboardInterrupt  # what the deploy does to a live transport

    async def _fake_build(**_kwargs: object) -> ConnectorsBundle:
        return ConnectorsBundle(
            deliverers={"telegram": object()},  # type: ignore[dict-item]
            runners={"telegram": _runner},
            http_app=None,
            idle_sweep=None,
        )

    monkeypatch.setattr(service, "build_connectors", _fake_build)

    entry.main()  # must return, not propagate

    assert closed == ["http"], "the finally teardown must still run on a signal stop"
