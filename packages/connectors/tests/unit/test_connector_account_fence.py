"""An erased account's still-valid token is refused by the connector service (R9-284 T9, L6).

The api refuses it in its per-request upsert; the connector routes authenticate a user without
that upsert, so the verifier checks the erasure ledger itself and the routes answer 410.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
import pytest
from fastapi.testclient import TestClient
from jose import jwt
from persona_connectors.config import ConnectorConfig
from persona_connectors.errors import AccountErasedTokenError
from persona_connectors.service import _build_connector_verifier
from persona_connectors.telegram.app import build_telegram_app
from pydantic import SecretStr

if TYPE_CHECKING:
    from persona.auth.jwt_verifier import AuthenticatedUser

_CONFIG = ConnectorConfig(jwt_secret="s3cret", jwt_algorithms="HS256")  # type: ignore[call-arg]


def _token(user_id: str) -> str:
    return jwt.encode({"sub": user_id, "exp": int(time.time()) + 60}, "s3cret", algorithm="HS256")


def test_an_erased_accounts_token_is_refused() -> None:
    asked: list[str] = []

    def fence(user_id: str) -> bool:
        asked.append(user_id)
        return user_id == "user_gone"

    verify = _build_connector_verifier(_CONFIG, httpx.AsyncClient(), fence)

    with pytest.raises(AccountErasedTokenError):
        asyncio.run(verify(_token("user_gone")))
    assert asyncio.run(verify(_token("user_live"))).id == "user_live"
    assert asked == ["user_gone", "user_live"]


def test_without_a_fence_the_verifier_is_the_plain_one() -> None:
    verify = _build_connector_verifier(_CONFIG, httpx.AsyncClient())

    assert asyncio.run(verify(_token("user_gone"))).id == "user_gone"


def test_the_link_route_answers_410_for_an_erased_account() -> None:
    issued: list[str] = []

    async def on_update(_update: dict[str, object]) -> None: ...

    async def issue_deep_link(owner_id: str) -> str:
        issued.append(owner_id)
        return "https://t.me/bot?start=x"

    async def verify_jwt(_token: str) -> AuthenticatedUser:
        raise AccountErasedTokenError("this account was deleted")

    app = build_telegram_app(
        webhook_secret=SecretStr("hook-secret"),
        on_update=on_update,
        issue_deep_link=issue_deep_link,
        verify_jwt=verify_jwt,
    )

    resp = TestClient(app).post(
        "/v1/connectors/telegram/link", headers={"Authorization": "Bearer good"}
    )

    assert resp.status_code == 410
    assert resp.json()["error"] == "account_erased"
    assert issued == [], "no link is minted for an erased account"


@pytest.mark.parametrize(
    "app", ["telegram/app.py", "discord/app.py", "slack/app.py", "email/app.py", "_twilio/app.py"]
)
def test_every_route_that_authenticates_a_user_answers_410_before_401(app: str) -> None:
    """Every connector app catches the erased-account refusal ahead of the generic 401."""
    import persona_connectors  # noqa: PLC0415

    source = (Path(persona_connectors.__file__).parent / app).read_text(encoding="utf-8")
    erased = source.index("except AccountErasedTokenError:")
    generic = source.index("except AuthenticationError:")
    assert erased < generic
    assert "status_code=410" in source[erased:generic]


# ---- every adapter build_connectors makes gets the fenced verifier (review L-4) ----------

_PLATFORM_API_RESPONSES = {
    "getMe": {"ok": True, "result": {"username": "opbot", "id": "1"}},
    "users/@me": {"id": "999", "username": "opbot"},
    "auth.test": {"ok": True, "user_id": "U1", "team_id": "T1"},
}


def _platform(request: httpx.Request) -> httpx.Response:
    for marker, payload in _PLATFORM_API_RESPONSES.items():
        if marker in str(request.url):
            return httpx.Response(200, json=payload)
    return httpx.Response(200, json={"ok": True, "result": {"username": "opbot", "id": "1"}})


@pytest.mark.asyncio
async def test_build_connectors_gives_every_adapter_the_fenced_verifier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drives the real build_connectors: each verifier it hands an adapter refuses an erased
    id, so no adapter can be wired with the plain one."""
    from unittest.mock import MagicMock  # noqa: PLC0415

    from persona_api.config import APIConfig, Edition  # noqa: PLC0415
    from persona_connectors import discord as discord_adapter  # noqa: PLC0415
    from persona_connectors import service  # noqa: PLC0415
    from persona_connectors import slack as slack_adapter  # noqa: PLC0415

    monkeypatch.setattr(service, "account_erased", lambda _engine, user_id: user_id == "user_gone")
    # What each adapter's app is actually built with (round 3 LOW-7): by the verifier it gets,
    # not by how many verifiers were made.
    given: dict[str, Any] = {}

    def _capturing(name: str, real: Any) -> Any:  # noqa: ANN401
        def build(*args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            given[name] = kwargs["verify_jwt"]
            return real(*args, **kwargs)

        return build

    monkeypatch.setattr(
        service, "build_telegram_app", _capturing("telegram", service.build_telegram_app)
    )
    monkeypatch.setattr(
        discord_adapter,
        "build_discord_app",
        _capturing("discord", discord_adapter.build_discord_app),
    )
    monkeypatch.setattr(
        slack_adapter, "build_slack_app", _capturing("slack", slack_adapter.build_slack_app)
    )
    config = ConnectorConfig(  # type: ignore[call-arg]
        edition="cloud",
        jwt_secret="test-jwt-secret",
        telegram_bot_token="tg-token",
        discord_oauth_client_id="discord-client-id",
        discord_oauth_client_secret="discord-client-secret",
        discord_oauth_redirect_uri="https://example.test/discord/oauth/callback",
        discord_bot_token="discord-token",
        slack_oauth_client_id="slack-client-id",
        slack_oauth_client_secret="slack-client-secret",
        slack_oauth_redirect_uri="https://example.test/slack/oauth/callback",
        slack_bot_token="xoxb-token",
        slack_app_token="xapp-token",
    )
    api_config = APIConfig(
        edition=Edition.community,
        community_db_path=str(tmp_path / "community.db"),
        community_memory_path=str(tmp_path / "memory"),
        audit_root=str(tmp_path / "audit"),
        workspace_root=str(tmp_path / "workspace"),
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(_platform)) as http:
        await service.build_connectors(
            connector_config=config,
            api_config=api_config,
            rls_engine=MagicMock(),
            dispatch_engine=MagicMock(),
            runtime_factory=MagicMock(),
            credits_policy=MagicMock(),
            job_queue=MagicMock(),
            stripe_gateway=None,
            http=http,
        )

    assert set(given) == {"telegram", "discord", "slack"}, "every configured adapter was built"

    def _token(user_id: str) -> str:
        return jwt.encode(
            {"sub": user_id, "exp": int(time.time()) + 60}, "test-jwt-secret", algorithm="HS256"
        )

    for name, verify in given.items():
        with pytest.raises(AccountErasedTokenError):
            await verify(_token("user_gone"))
        assert (await verify(_token("user_live"))).id == "user_live", name
