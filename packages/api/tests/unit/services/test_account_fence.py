"""The account fence in the per-request user upsert (R9-284 T9, D-R9-284-account-erase).

An id with any account erasure, finished or not, is refused with 410 and never provisioned
again. The statement needs Postgres and its behaviour runs in CI
(tests/integration/test_account_fence_postgres.py); these pin the Python side and the shape.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig, Edition
from persona_api.errors import AccountErasedError
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.services import user_service

if TYPE_CHECKING:
    from collections.abc import Iterator


class _Result:
    def __init__(self, value: bool) -> None:  # noqa: FBT001
        self._value = value

    def scalar_one(self) -> bool:
        return self._value


class _Engine:
    """Answers the fenced upsert with ``erased``; records each statement and its params."""

    def __init__(self, *, erased: bool) -> None:
        self.erased = erased
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @contextmanager
    def begin(self) -> Iterator[_Engine]:
        yield self

    def execute(self, stmt: Any, params: dict[str, Any]) -> _Result:  # noqa: ANN401
        self.calls.append((str(stmt), params))
        return _Result(self.erased)


def test_an_erased_id_is_refused() -> None:
    engine = _Engine(erased=True)

    with pytest.raises(AccountErasedError):
        user_service.ensure_user(engine, user_id="user_gone", email="a@x.test")  # type: ignore[arg-type]


def test_a_live_id_is_provisioned_in_the_same_single_statement() -> None:
    engine = _Engine(erased=False)

    user_service.ensure_user(engine, user_id="user_live", email="a@x.test")  # type: ignore[arg-type]

    assert len(engine.calls) == 1, "the fence costs no extra round trip"
    sql, params = engine.calls[0]
    assert params["id"] == "user_live"
    assert "INSERT INTO users" in sql


def test_the_insert_only_happens_when_no_account_erasure_names_the_id() -> None:
    """The shape the CI test exercises: the probe reads account erasures of this id, finished
    ones included (no state filter), and guards the INSERT."""
    sql = str(user_service._FENCED_UPSERT)  # noqa: SLF001

    probe = sql[sql.index("WITH erased AS (") : sql.index("), upserted AS (")]
    assert "subject_kind = 'account'" in probe
    assert "subject_id = :id" in probe
    assert "state" not in probe, "a finished erasure fences too"
    assert "WHERE NOT EXISTS (SELECT 1 FROM erased)" in sql
    assert sql.rstrip().endswith("SELECT EXISTS (SELECT 1 FROM erased)")


@pytest.fixture
def client() -> TestClient:
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://u:p@localhost:1/none",  # never connected
            app_database_url="postgresql+psycopg://persona_app@localhost:1/none",
            edition=Edition.cloud,
        )
    )

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    app.state.verify_token = _verify
    app.state.admin_engine = _Engine(erased=True)
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )
    return TestClient(app)


def test_a_request_from_an_erased_account_is_a_410(client: TestClient) -> None:
    resp = client.get("/v1/me/credits", headers={"Authorization": "Bearer user_gone_123"})

    assert resp.status_code == 410
    assert "account_erased" in resp.text
    assert "user_gone_123" not in resp.text, "the id is not echoed back"
