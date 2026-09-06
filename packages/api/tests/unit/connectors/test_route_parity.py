"""The connectors' HTTP surface, served by the api, must behave as it did standalone.

Spec I1 T3. Five provider registrations pin ``https://connectors.openpersonasai.com/...``
paths: the Discord and Slack OAuth redirects, the Slack events URL, the Postmark inbound
webhook and the Twilio webhooks. Option A moves the hostname rather than the paths, so the
cutover is one DNS record and not five re-registrations at four providers. That only holds
if the routes behave IDENTICALLY once mounted, which is what these tests measure.

They do not re-assert the connectors' own expectations by hand. Each probe is sent to BOTH
hostings and the two responses are compared, so the standalone app is the oracle and a
divergence fails no matter which side moved. The api app under test carries the real
exception handlers and the real ``RequestTelemetryMiddleware``, because those are the two
things mounting actually changes.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from persona_api.background.connectors_host import mount_connector_routes
from persona_api.errors import register_exception_handlers
from persona_api.middleware.request_telemetry import RequestTelemetryMiddleware
from persona_connectors import service
from persona_connectors.config import ConnectorConfig

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

_TWILIO_TOKEN = "twilio-auth-token"  # noqa: S105 — test literal


def _platform_handler(request: httpx.Request) -> httpx.Response:
    """One transport answering every platform's identity probe at setup."""
    url = str(request.url)
    if "users/@me" in url:
        return httpx.Response(200, json={"id": "999", "username": "opbot"})
    if "auth.test" in url:
        return httpx.Response(200, json={"ok": True, "user_id": "U1", "team_id": "T1"})
    return httpx.Response(200, json={"ok": True, "result": {"username": "opbot", "id": "1"}})


def _all_platforms_config() -> ConnectorConfig:
    """Every transport configured, so one bundle carries the whole HTTP surface."""
    return ConnectorConfig(  # type: ignore[call-arg]
        edition="cloud",
        jwt_secret="test-jwt-secret",  # noqa: S106 — test literal
        telegram_bot_token="tg-token",  # noqa: S106 — test literal
        discord_bot_token="dc-token",  # noqa: S106 — test literal
        discord_oauth_client_id="discord-client-id",
        discord_oauth_client_secret="discord-client-secret",  # noqa: S106 — test literal
        discord_oauth_redirect_uri="https://example.test/discord/oauth/callback",
        slack_bot_token="sl-token",  # noqa: S106 — test literal
        slack_transport="http",
        slack_signing_secret="slack-signing-secret",  # noqa: S106 — test literal
        slack_oauth_client_id="slack-client-id",
        slack_oauth_client_secret="slack-client-secret",  # noqa: S106 — test literal
        slack_oauth_redirect_uri="https://example.test/slack/oauth/callback",
        twilio_account_sid="AC123",
        twilio_auth_token=_TWILIO_TOKEN,
        # SEPARATE from twilio_auth_token by design: signing vs API auth are
        # distinct fields, and the signature check fails CLOSED when this is unset.
        twilio_webhook_auth_token=_TWILIO_TOKEN,
        twilio_whatsapp_from="whatsapp:+15550001111",
        twilio_sms_from="+15550002222",
        postmark_server_token="pm-token",  # noqa: S106 — test literal
        postmark_webhook_username="pmuser",
        postmark_webhook_password="pmpass",  # noqa: S106 — test literal
        email_inbound_address="persona@example.test",
    )


@pytest.fixture(scope="module")
def connectors_app() -> Iterator[FastAPI]:
    """The merged connectors app, built by the REAL composition with fakes underneath."""
    import asyncio

    http = httpx.AsyncClient(transport=httpx.MockTransport(_platform_handler))

    async def _build() -> Any:  # noqa: ANN401 — the bundle
        return await service.build_connectors(
            connector_config=_all_platforms_config(),
            api_config=MagicMock(),
            rls_engine=MagicMock(),
            dispatch_engine=MagicMock(),
            runtime_factory=MagicMock(),
            credits_policy=MagicMock(),
            job_queue=MagicMock(),
            stripe_gateway=None,
            http=http,
        )

    bundle = asyncio.run(_build())
    assert bundle.http_app is not None
    yield bundle.http_app
    asyncio.run(http.aclose())


@pytest.fixture(scope="module")
def standalone_client(connectors_app: FastAPI) -> Iterator[TestClient]:
    """The connectors app as the standalone service serves it. The ORACLE."""
    with TestClient(connectors_app, raise_server_exceptions=False) as client:
        yield client


@pytest.fixture(scope="module")
def mounted_client(connectors_app: FastAPI) -> Iterator[TestClient]:
    """The same routes mounted on an api-shaped app: real handlers, real telemetry."""
    api = FastAPI(title="persona-api (parity harness)")
    register_exception_handlers(api)
    api.add_middleware(RequestTelemetryMiddleware)

    @api.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    mount_connector_routes(api, connectors_app)
    with TestClient(api, raise_server_exceptions=False) as client:
        yield client


def _twilio_signature(url: str, params: Mapping[str, str]) -> str:
    base = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    return base64.b64encode(
        hmac.new(_TWILIO_TOKEN.encode(), base.encode(), hashlib.sha1).digest()
    ).decode()


# Every path a provider is registered against, plus the authenticated link routes. Each is
# probed UNAUTHENTICATED / UNSIGNED, which is the security-relevant case: the mounted route
# must refuse exactly as the standalone one does, with the same status and the same body.
_PROBES: list[tuple[str, str, dict[str, Any]]] = [
    ("POST", "/v1/connectors/telegram/link", {}),
    ("POST", "/v1/connectors/discord/link", {}),
    ("POST", "/v1/connectors/slack/link", {}),
    ("POST", "/v1/connectors/whatsapp/link", {}),
    ("POST", "/v1/connectors/sms/link", {}),
    ("POST", "/v1/connectors/email/link", {}),
    ("GET", "/discord/oauth/callback", {}),
    ("GET", "/slack/oauth/callback", {}),
    ("POST", "/telegram/webhook", {"json": {"message": {}}}),
    ("POST", "/slack/events", {"json": {"type": "url_verification"}}),
    ("POST", "/email/webhook", {"json": {"From": "a@b.test"}}),
    ("POST", "/whatsapp/webhook", {"data": {"From": "whatsapp:+15550009999", "Body": "hi"}}),
    ("POST", "/whatsapp/status", {"data": {"MessageStatus": "delivered"}}),
    ("POST", "/sms/webhook", {"data": {"From": "+15550009999", "Body": "hi"}}),
    ("POST", "/sms/status", {"data": {"MessageStatus": "delivered"}}),
]


@pytest.mark.parametrize(("method", "path", "kwargs"), _PROBES, ids=[p[1] for p in _PROBES])
def test_every_pinned_path_answers_identically_mounted_and_standalone(
    method: str,
    path: str,
    kwargs: dict[str, Any],
    standalone_client: TestClient,
    mounted_client: TestClient,
) -> None:
    """The parity contract, path by path, with the standalone app as the oracle.

    Comparing the two hostings rather than asserting literal codes means the test cannot
    drift into restating what the code does: if either side changes, they stop matching.
    A 404 on either side would also fail the comparison against the other, so a route that
    silently failed to mount cannot pass.
    """
    solo = standalone_client.request(method, path, **kwargs)
    mounted = mounted_client.request(method, path, **kwargs)

    assert mounted.status_code == solo.status_code, (
        f"{method} {path}: mounted returned {mounted.status_code}, "
        f"standalone returned {solo.status_code}"
    )
    assert mounted.content == solo.content, f"{method} {path}: body differs once mounted"
    assert solo.status_code != 404, f"{method} {path} is not served at all; the probe is stale"


def test_the_twilio_signature_still_verifies_once_mounted(
    standalone_client: TestClient, mounted_client: TestClient
) -> None:
    """The raw body must survive mounting, or every inbound WhatsApp message 403s.

    Twilio signs the request URL plus the sorted POST parameters, so the check depends on
    the body arriving byte-identical. Any middleware that consumed or re-wrapped it would
    break this while every unauthenticated probe above still passed, which is why this
    signs a real request rather than only probing rejections.
    ``RequestTelemetryMiddleware`` is installed on the mounted app for exactly this
    reason: it must never call ``.body()``.

    The assertion is the SIGNATURE decision, not the eventual status. Past the signature
    the request enters the real inbound flow, which in this harness runs on fake
    collaborators and fails; comparing final statuses there would be testing the mocks.
    Forged-versus-valid on both hostings is the property mounting could actually break.
    """
    params = {"From": "whatsapp:+15550009999", "Body": "hello", "MessageSid": "SM1"}
    # Twilio signs the URL the app RECONSTRUCTS. Under TestClient that is
    # ``http://testserver/...``; in production it is https, because uvicorn runs with
    # ``--proxy-headers --forwarded-allow-ips=*`` and Fly terminates TLS upstream. That is
    # a server-level flag the api already sets and mounting does not touch.
    url = "http://testserver/whatsapp/webhook"
    valid = {"X-Twilio-Signature": _twilio_signature(url, params)}
    forged = {"X-Twilio-Signature": "forged=="}

    for client, hosting in ((standalone_client, "standalone"), (mounted_client, "mounted")):
        rejected = client.post("/whatsapp/webhook", data=params, headers=forged)
        assert rejected.status_code == 403, f"{hosting}: a forged signature must be refused"
        accepted = client.post("/whatsapp/webhook", data=params, headers=valid)
        assert accepted.status_code != 403, (
            f"{hosting}: a VALID signature was rejected, so the raw body did not survive"
        )


def test_an_escaping_error_maps_to_the_apis_handler_which_can_change_the_status(
    standalone_client: TestClient, mounted_client: TestClient
) -> None:
    """The one measured divergence, recorded rather than discovered later in production.

    D-I1-2 settled that a connector route which HANDLES its own error keeps its own
    response (every real route does, and the 15 probes above prove it path by path), while
    an error that ESCAPES reaches the api's handler stack. That decision recorded the
    escape case as "500 either way, only the body differs". That is true only for an
    exception type the api has no specific handler for.

    Measured here: the api registers a handler for Pydantic's ``ValidationError``
    (``errors.py``) mapping to 422, so an escaping ``ValidationError`` is **500 standalone
    and 422 mounted**. Not provider-visible in practice, since every provider treats any
    non-2xx as a retry, and 422 is arguably the more honest code. But it is a real
    difference and the parity claim is narrower than D-I1-2 stated, so it is pinned here
    instead of being rediscovered from a log.

    The escaping error is produced by this harness's fake collaborators, which is the only
    way to reach the escape path without a broken production dependency.
    """
    params = {"From": "whatsapp:+15550009999", "Body": "hello", "MessageSid": "SM1"}
    url = "http://testserver/whatsapp/webhook"
    headers = {"X-Twilio-Signature": _twilio_signature(url, params)}

    solo = standalone_client.post("/whatsapp/webhook", data=params, headers=headers)
    mounted = mounted_client.post("/whatsapp/webhook", data=params, headers=headers)

    # Both refuse the request; neither pretends it succeeded.
    assert solo.status_code >= 400
    assert mounted.status_code >= 400
    # And the difference is exactly the api's own ValidationError handler.
    assert solo.status_code == 500
    assert mounted.status_code == 422


def test_mounting_leaves_the_api_openapi_schema_unchanged(connectors_app: FastAPI) -> None:
    """D-I1-11: webhooks are provider contracts, never part of the api's public one.

    Without ``include_in_schema=False`` the generated web client would grow six webhook
    endpoints it must never call; without dropping the cached schema, a schema built
    earlier in the boot would be served stale. Both halves are required, so both are
    asserted here against a schema that was deliberately generated BEFORE the mount.
    """
    api = FastAPI(title="persona-api (schema harness)")

    @api.get("/livez")
    async def livez() -> dict[str, str]:
        return {"status": "ok"}

    before = sorted(api.openapi()["paths"])  # generated first, so the cache is populated
    assert before == ["/livez"]

    mounted = mount_connector_routes(api, connectors_app)
    assert mounted > 0, "nothing was mounted; the rest of this assertion would be vacuous"

    # The cache must be DROPPED, not merely happen to agree. With every route hidden the
    # recomputed schema equals the cached one, so a stale cache is invisible to the path
    # comparison below (verified by mutation: removing the invalidation leaves that
    # comparison green). Asserted directly, because its job is to guarantee the next
    # openapi() recomputes from the real route table instead of serving a pre-mount
    # snapshot that could mask a route which failed to get include_in_schema=False.
    assert api.openapi_schema is None, "the cached schema survived the mount"

    after = sorted(api.openapi()["paths"])
    assert after == before, f"mounting leaked {set(after) - set(before)} into the api schema"
