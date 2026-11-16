"""Clerk's ``user.deleted`` webhook: the Svix check and what a delivery starts (R9-284 T9).

The signature check is pinned to Svix's documented test vector. Nothing reaches Clerk.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.config import APIConfig, Edition
from persona_api.db.models import erasures, users
from persona_api.errors import ClerkWebhookRejectedError
from persona_api.middleware.rate_limit import InMemoryRateLimitStore, RateLimiter
from persona_api.routes import clerk_webhook as route
from persona_api.services import clerk_webhook as hook
from persona_api.services.erasure_ledger import ErasureLedger, SubjectKind
from sqlalchemy import create_engine, insert

if TYPE_CHECKING:
    from sqlalchemy import Engine

# Svix's documented example (docs.svix.com, "Verifying payloads manually").
_VECTOR_SECRET = "whsec_MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw"
_VECTOR_ID = "msg_p5jXN8AQM9LWM0D4loKWxJek"
_VECTOR_TS = "1614265330"
_VECTOR_BODY = b'{"test": 2432232314}'
_VECTOR_SIG = "v1,g0hM9SsE+OTPJTGt/tmIKtSyZlE3uFJELVlNIOLJ1OE="
_VECTOR_NOW = datetime.fromtimestamp(int(_VECTOR_TS), tz=UTC)


def _verify(**overrides: Any) -> None:  # noqa: ANN401
    args: dict[str, Any] = {
        "msg_id": _VECTOR_ID,
        "timestamp": _VECTOR_TS,
        "signatures": _VECTOR_SIG,
        "body": _VECTOR_BODY,
        "now": _VECTOR_NOW,
    }
    secret = overrides.pop("secret", _VECTOR_SECRET)
    args.update(overrides)
    hook.verify_signature(secret, **args)


def test_svixs_documented_vector_verifies() -> None:
    _verify()


@pytest.mark.parametrize(
    "override",
    [
        {"body": b'{"test": 2432232315}'},
        {"msg_id": "msg_other"},
        {"timestamp": str(int(_VECTOR_TS) + 1), "now": _VECTOR_NOW},
        {"signatures": "v1,AAAA" + _VECTOR_SIG[7:]},
        {"signatures": "v2," + _VECTOR_SIG[3:]},
        {"secret": "whsec_" + base64.b64encode(b"another key").decode()},
    ],
)
def test_anything_changed_is_rejected(override: dict[str, Any]) -> None:
    with pytest.raises(ClerkWebhookRejectedError):
        _verify(**override)


def test_one_matching_signature_among_several_is_enough() -> None:
    _verify(signatures=f"v1,bm9wZQ== {_VECTOR_SIG}")


@pytest.mark.parametrize("skew", [timedelta(minutes=6), -timedelta(minutes=6)])
def test_a_delivery_outside_five_minutes_is_rejected(skew: timedelta) -> None:
    with pytest.raises(ClerkWebhookRejectedError):
        _verify(now=_VECTOR_NOW + skew)


def test_a_delivery_within_five_minutes_is_accepted() -> None:
    _verify(now=_VECTOR_NOW + timedelta(minutes=4))


@pytest.mark.parametrize("secret", ["MfKQ9r8GKYqrTwjUPD8ILPZIo2LaLaSw", "whsec_not base64!"])
def test_a_malformed_secret_is_rejected(secret: str) -> None:
    with pytest.raises(ClerkWebhookRejectedError):
        _verify(secret=secret)


# ---- what a delivery starts -------------------------------------------------------------


@pytest.fixture
def engine() -> Engine:
    eng = create_engine("sqlite://")
    users.create(eng)
    erasures.create(eng)
    return eng


def _event(kind: str = "user.deleted", user_id: str = "user_2abc") -> bytes:
    return json.dumps({"type": kind, "data": {"id": user_id, "deleted": True}}).encode()


@pytest.fixture
def claims(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, str]]:
    seen: list[dict[str, str]] = []

    def _begin(eng: Engine, *, owner_id: str, origin: str) -> Any:  # noqa: ANN401
        seen.append({"owner_id": owner_id, "origin": origin})
        return ErasureLedger(eng).claim(
            owner_id=owner_id, subject_kind=SubjectKind.ACCOUNT, subject_id=owner_id, captured={}
        )

    monkeypatch.setattr(hook, "begin_account_erasure", _begin)
    return seen


def _provision(engine: Engine, user_id: str = "user_2abc") -> None:
    with engine.begin() as conn:
        conn.execute(insert(users).values(id=user_id, email=f"{user_id}@x.test"))


def test_user_deleted_starts_the_erasure_from_clerk(
    engine: Engine, claims: list[dict[str, str]]
) -> None:
    _provision(engine)

    result = hook.handle_event(engine, _event())

    assert result.outcome == "started"
    assert claims == [{"owner_id": "user_2abc", "origin": "clerk"}]


def test_another_event_type_changes_nothing(engine: Engine, claims: list[dict[str, str]]) -> None:
    _provision(engine)

    assert hook.handle_event(engine, _event("user.updated")).outcome == "ignored"
    assert claims == []


def test_a_user_never_provisioned_changes_nothing(
    engine: Engine, claims: list[dict[str, str]]
) -> None:
    assert hook.handle_event(engine, _event()).outcome == "unknown"
    assert claims == []


def test_a_redelivery_finds_the_running_erasure(
    engine: Engine, claims: list[dict[str, str]]
) -> None:
    _provision(engine)
    first = hook.handle_event(engine, _event())

    again = hook.handle_event(engine, _event())

    assert again.outcome == "running"
    assert again.erasure is not None
    assert first.erasure is not None
    assert again.erasure.id == first.erasure.id
    assert len(claims) == 1


@pytest.mark.parametrize("bad", [b"not json", _event(user_id="user_a/../b"), _event(user_id="")])
def test_an_unreadable_delivery_is_rejected(engine: Engine, bad: bytes) -> None:
    with pytest.raises(ClerkWebhookRejectedError):
        hook.handle_event(engine, bad)


# ---- the route --------------------------------------------------------------------------

_SECRET = "whsec_" + base64.b64encode(b"a test signing key, 32 bytes!!!").decode()


def _signed(body: bytes, *, ts: int | None = None) -> dict[str, str]:
    stamp = str(ts if ts is not None else int(datetime.now(UTC).timestamp()))
    key = base64.b64decode(_SECRET.removeprefix("whsec_"))
    mac = hmac.new(key, b"msg_1." + stamp.encode() + b"." + body, hashlib.sha256).digest()
    return {
        "svix-id": "msg_1",
        "svix-timestamp": stamp,
        "svix-signature": "v1," + base64.b64encode(mac).decode(),
    }


class _Runner:
    def __init__(self) -> None:
        self.submitted: list[Any] = []

    def submit(self, erasure: Any) -> None:  # noqa: ANN401
        self.submitted.append(erasure)


def _client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    edition: Edition = Edition.cloud,
    secret: str | None = _SECRET,
) -> tuple[TestClient, _Runner, list[bytes]]:
    app = create_app(
        APIConfig(
            database_url="postgresql+psycopg://u:p@localhost:1/none",  # never connected
            app_database_url="postgresql+psycopg://persona_app@localhost:1/none",
            edition=edition,
            clerk_webhook_secret=secret,
        )
    )
    handled: list[bytes] = []

    def _handle(_engine: object, body: bytes) -> SimpleNamespace:
        handled.append(body)
        return SimpleNamespace(erasure=SimpleNamespace(id="erasure_1"), outcome="started")

    monkeypatch.setattr(route, "handle_event", _handle)
    runner = _Runner()
    app.state.platform_engine = object()
    app.state.account_erase_runner = runner
    app.state.rate_limiter = RateLimiter(
        InMemoryRateLimitStore(), default_limit=1000, per_endpoint={}
    )
    return TestClient(app), runner, handled


def test_a_signed_delivery_is_handled_and_handed_to_the_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runner, handled = _client(monkeypatch)
    body = _event()

    resp = client.post("/v1/webhooks/clerk", content=body, headers=_signed(body))

    assert resp.status_code == 200
    assert resp.json() == {"outcome": "started"}
    assert handled == [body]
    assert [e.id for e in runner.submitted] == ["erasure_1"]


def test_an_unsigned_or_forged_delivery_is_400_and_handles_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, runner, handled = _client(monkeypatch)
    body = _event()
    forged = {**_signed(body), "svix-signature": "v1,Zm9yZ2Vk"}

    assert client.post("/v1/webhooks/clerk", content=body).status_code == 400
    assert client.post("/v1/webhooks/clerk", content=body, headers=forged).status_code == 400
    assert handled == []
    assert runner.submitted == []


def test_a_replayed_old_delivery_is_400(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, handled = _client(monkeypatch)
    body = _event()
    old = int((datetime.now(UTC) - timedelta(hours=1)).timestamp())

    resp = client.post("/v1/webhooks/clerk", content=body, headers=_signed(body, ts=old))

    assert resp.status_code == 400
    assert handled == []


def test_a_body_over_the_limit_is_refused_before_it_is_verified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _, handled = _client(monkeypatch)
    checked: list[bytes] = []
    monkeypatch.setattr(route, "verify_signature", lambda *_a, **k: checked.append(k["body"]))
    body = b"x" * (hook.MAX_BODY_BYTES + 1)

    resp = client.post("/v1/webhooks/clerk", content=body, headers=_signed(body))

    assert resp.status_code == 413
    assert checked == [], "refused unread: never verified"
    assert handled == []


def test_without_a_secret_it_is_503_so_clerk_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, handled = _client(monkeypatch, secret=None)
    body = _event()

    assert client.post("/v1/webhooks/clerk", content=body, headers=_signed(body)).status_code == 503
    assert handled == []


def test_off_cloud_there_is_no_such_route(monkeypatch: pytest.MonkeyPatch) -> None:
    client, _, _ = _client(monkeypatch, edition=Edition.community)
    body = _event()

    assert client.post("/v1/webhooks/clerk", content=body, headers=_signed(body)).status_code == 404


def test_a_chunked_body_over_the_limit_is_refused_as_it_streams(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No Content-Length to go by: the stream is counted as it is read, and cut off."""
    client, _, handled = _client(monkeypatch)
    checked: list[bytes] = []
    monkeypatch.setattr(route, "verify_signature", lambda *_a, **k: checked.append(k["body"]))
    chunk = b"x" * 4096

    def _chunks() -> Any:  # noqa: ANN401
        for _ in range(hook.MAX_BODY_BYTES // len(chunk) + 2):
            yield chunk

    resp = client.post("/v1/webhooks/clerk", content=_chunks(), headers=_signed(b""))

    assert resp.status_code == 413
    assert checked == []
    assert handled == []


@pytest.mark.parametrize("declared", [str(hook.MAX_BODY_BYTES + 1), "not a number"])
def test_a_declared_length_over_the_limit_is_refused_before_reading(
    monkeypatch: pytest.MonkeyPatch, declared: str
) -> None:
    client, _, handled = _client(monkeypatch)
    body = _event()

    resp = client.post(
        "/v1/webhooks/clerk", content=body, headers={**_signed(body), "content-length": declared}
    )

    assert resp.status_code == 413, "refused on what it declares, before the stream is read"
    assert handled == []
