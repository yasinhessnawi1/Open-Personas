"""An erased account's still-valid token is refused by the connector service (R9-284 T9, L6).

The api refuses it in its per-request upsert; the connector routes authenticate a user without
that upsert, so the verifier checks the erasure ledger itself and the routes answer 410.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import TYPE_CHECKING

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
