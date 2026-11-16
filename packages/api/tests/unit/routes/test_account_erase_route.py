"""``POST /v1/me/erase`` (R9-284 T9). Nothing reaches Stripe or Clerk; the runner is a fake."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig, Edition
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.routes import account_erase as route
from persona_api.services.erasure_ledger import ErasureState

_USER = "user_2abc"


class _Runner:
    def __init__(self, reason: str | None = None) -> None:
        self.submitted: list[Any] = []
        self.reason = reason
        self.asked: list[tuple[str, str]] = []

    def unready_reason(self, *, owner_id: str, origin: str) -> str | None:
        self.asked.append((owner_id, origin))
        return self.reason

    def submit(self, erasure: Any) -> None:  # noqa: ANN401
        self.submitted.append(erasure)


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    edition: Edition = Edition.cloud,
    email: str | None = "Ann@Example.test",
    runner: _Runner | None = None,
) -> tuple[TestClient, list[dict[str, str]]]:
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://u:p@localhost:1/none",  # never connected
            app_database_url="postgresql+psycopg://persona_app@localhost:1/none",
            edition=edition,
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    claims: list[dict[str, str]] = []

    def _begin(_engine: object, *, owner_id: str, origin: str) -> SimpleNamespace:
        claims.append({"owner_id": owner_id, "origin": origin})
        return SimpleNamespace(id="erasure_1", state=ErasureState.PENDING)

    monkeypatch.setattr(route, "begin_account_erasure", _begin)
    monkeypatch.setattr(
        route, "get_user_profile", lambda _e, *, user_id: {"id": user_id, "email": email}
    )
    app.state.verify_token = _verify
    app.state.admin_engine = None  # no per-request upsert in this test (the fence has its own)
    app.state.platform_engine = object()
    app.state.account_erase_runner = runner
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )
    return TestClient(app), claims


def _post(client: TestClient, confirm: str) -> Any:  # noqa: ANN401
    return client.post(
        "/v1/me/erase", json={"confirm": confirm}, headers={"Authorization": f"Bearer {_USER}"}
    )


def test_a_confirmed_delete_claims_hands_over_and_answers_202(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = _Runner()
    client, claims = _client(monkeypatch, runner=runner)

    resp = _post(client, "  ann@example.test ")

    assert resp.status_code == 202
    assert resp.json() == {"erasure_id": "erasure_1", "state": "pending"}
    assert claims == [{"owner_id": _USER, "origin": "route"}]
    assert [e.id for e in runner.submitted] == ["erasure_1"]


def test_a_wrong_confirmation_erases_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _Runner()
    client, claims = _client(monkeypatch, runner=runner)

    resp = _post(client, "someone@else.test")

    assert resp.status_code == 422
    assert "account_erase_unconfirmed" in resp.text
    assert claims == []
    assert runner.submitted == []


@pytest.mark.parametrize("email", [None, f"{_USER}@users.noreply"])
def test_an_account_without_a_real_email_confirms_with_the_phrase(
    monkeypatch: pytest.MonkeyPatch, email: str | None
) -> None:
    runner = _Runner()
    client, _ = _client(monkeypatch, email=email, runner=runner)

    assert _post(client, f"{_USER}@users.noreply").status_code == 422
    assert _post(client, route.CONFIRM_PHRASE).status_code == 202


def test_off_cloud_there_is_no_such_route(monkeypatch: pytest.MonkeyPatch) -> None:
    client, claims = _client(monkeypatch, edition=Edition.community, runner=_Runner())

    resp = client.post("/v1/me/erase", json={"confirm": route.CONFIRM_PHRASE})

    assert resp.status_code == 404
    assert claims == []


def test_without_a_runner_it_is_unavailable_and_claims_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, claims = _client(monkeypatch, runner=None)

    resp = _post(client, "ann@example.test")

    assert resp.status_code == 503
    assert claims == [], "nothing is claimed that no runner would carry out"


def test_a_deletion_the_runner_could_not_finish_is_503_and_claims_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review H2: no Clerk key (or no Stripe for a customer) means no deletion is accepted."""
    runner = _Runner(reason="clerk_not_configured")
    client, claims = _client(monkeypatch, runner=runner)

    resp = _post(client, "ann@example.test")

    assert resp.status_code == 503
    assert runner.asked == [(_USER, "route")]
    assert claims == []
    assert runner.submitted == []
