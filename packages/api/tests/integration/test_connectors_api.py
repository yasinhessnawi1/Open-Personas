"""GET /v1/me/connectors — the connector management list (Spec C6, T1).

Drives the real app against Docker Postgres with a fake JWT verifier. Bindings
are written server-side by the connector service (C1's ``redeem_and_bind``); this
test seeds ``connector_identities`` as superuser, then reads through the endpoint.
Concerns:

1. **List** — the caller's ACTIVE bindings, each carrying the connected identity
   (``platform_identity``) + ``linked_at``, newest-first.
2. **RLS tenant isolation (criterion 11, non-vacuous)** — a caller sees ONLY their
   own connectors; another user's binding on the same platform never leaks, and
   the connected-identity display cannot cross users.
3. **Revoked ≠ connected** — a disconnected (revoked) binding is not listed.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine

# The REAL inbound resolution path (C1's spine) — used to prove a disconnect truly
# severs the binding (criterion 9): after revoke, resolve_owner fails closed.
from persona_connectors.domain.linking import LinkingService
from persona_connectors.errors import IdentityNotLinkedError
from persona_connectors.infra.link_store import PostgresLinkStore
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)
_USER_A = "user_conn_a"
_USER_B = "user_conn_b"


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema + persona_app grants
    embedder: HashEmbedder384,  # noqa: ARG001 — app lifespan wants an embedder
    tmp_path: Path,
) -> Iterator[tuple[TestClient, str]]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(app_database_url=app_url, audit_root=str(tmp_path / "audit"))
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)  # token == user_id

    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        yield c, _USER_A
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": _USER_A, "b": _USER_B})
    su.dispose()


_CONNECTOR_URL = "http://connector.test"


@pytest.fixture
def link_client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema + persona_app grants
    embedder: HashEmbedder384,  # noqa: ARG001 — app lifespan wants an embedder
    tmp_path: Path,
) -> Iterator[tuple[TestClient, str]]:
    """A client whose app has ``connector_service_url`` set (for the link proxy, T3)."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(
        app_database_url=app_url,
        audit_root=str(tmp_path / "audit"),
        connector_service_url=_CONNECTOR_URL,
    )
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)

    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        yield c, _USER_A
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id IN (:a, :b)"), {"a": _USER_A, "b": _USER_B})
    su.dispose()


def _install_mock_httpx(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    """Swap ``httpx.AsyncClient`` for one whose transport is a MockTransport (house pattern).

    The proxy route looks up ``httpx.AsyncClient`` at call time, so patching the module
    attribute routes its upstream POST into ``handler`` — no real network.
    """
    real = httpx.AsyncClient

    def factory(*_a: object, **_k: object) -> httpx.AsyncClient:
        return real(transport=httpx.MockTransport(handler))

    monkeypatch.setattr(httpx, "AsyncClient", factory)


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def _resolver(su: Engine) -> LinkingService:
    """C1's real resolver over a BYPASSRLS engine (superuser bypasses RLS), exactly
    as the connector service resolves an inbound sender pre-auth (dispatch read)."""
    return LinkingService(PostgresLinkStore(rls_engine=su, dispatch_engine=su))


def _binding_status(su: Engine, *, platform: str, identity: str) -> tuple[str, object] | None:
    with su.begin() as conn:
        row = conn.execute(
            text(
                "SELECT status, revoked_at FROM connector_identities "
                "WHERE platform = :p AND platform_identity = :pi"
            ),
            {"p": platform, "pi": identity},
        ).first()
    return (str(row[0]), row[1]) if row is not None else None


def _seed_binding(
    engine: Engine,
    *,
    bid: str,
    owner: str,
    platform: str,
    identity: str,
    linked_at: datetime,
    status: str = "active",
    revoked_at: datetime | None = None,
) -> None:
    """Seed a user + a connector binding (superuser bypasses RLS)."""
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": owner, "e": f"{owner}@example.com"},
        )
        conn.execute(
            text(
                "INSERT INTO connector_identities "
                "(id, platform, platform_identity, owner_id, status, linked_at, revoked_at) "
                "VALUES (:i, :p, :pi, :o, :s, :la, :ra)"
            ),
            {
                "i": bid,
                "p": platform,
                "pi": identity,
                "o": owner,
                "s": status,
                "la": linked_at,
                "ra": revoked_at,
            },
        )


def test_list_connectors_returns_active_bindings_with_identity(
    client: tuple[TestClient, str],
) -> None:
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_binding(
            su, bid="b_tg", owner=uid, platform="telegram", identity="12345", linked_at=_T0
        )
        _seed_binding(
            su,
            bid="b_sms",
            owner=uid,
            platform="sms",
            identity="+4790000000",
            linked_at=_T0 + timedelta(hours=1),
        )
    finally:
        su.dispose()

    rows = c.get("/v1/me/connectors", headers=_auth(uid)).json()
    # Newest-first by linked_at.
    assert [r["platform"] for r in rows] == ["sms", "telegram"]
    sms = rows[0]
    assert sms["platform_identity"] == "+4790000000"  # the connected identity is shown
    assert "linked_at" in sms
    # No token/secret ever leaks into the connection view (extra="forbid" on the schema).
    assert set(sms.keys()) == {"platform", "platform_identity", "linked_at"}


def test_connectors_list_is_rls_scoped(client: tuple[TestClient, str]) -> None:
    """Criterion 11, non-vacuous: same platform, two users — no cross-user leak."""
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_binding(
            su, bid="b_mine", owner=uid, platform="discord", identity="mine_999", linked_at=_T0
        )
        _seed_binding(
            su,
            bid="b_theirs",
            owner=_USER_B,
            platform="discord",
            identity="theirs_888",
            linked_at=_T0,
        )
    finally:
        su.dispose()

    rows = c.get("/v1/me/connectors", headers=_auth(uid)).json()
    identities = {r["platform_identity"] for r in rows}
    assert identities == {"mine_999"}, f"RLS leak on /v1/me/connectors: caller saw {identities}"


def test_revoked_bindings_are_not_listed(client: tuple[TestClient, str]) -> None:
    """A disconnected (revoked) binding is not a connection."""
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_binding(
            su, bid="b_active", owner=uid, platform="email", identity="me@x.io", linked_at=_T0
        )
        _seed_binding(
            su,
            bid="b_revoked",
            owner=uid,
            platform="whatsapp",
            identity="+4791111111",
            linked_at=_T0,
            status="revoked",
            revoked_at=_T0 + timedelta(minutes=5),
        )
    finally:
        su.dispose()

    rows = c.get("/v1/me/connectors", headers=_auth(uid)).json()
    platforms = {r["platform"] for r in rows}
    assert platforms == {"email"}, f"revoked binding leaked as connected: {platforms}"


def test_list_connectors_empty_when_none(client: tuple[TestClient, str]) -> None:
    """A user with no bindings gets an empty list (and no one else's rows)."""
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_binding(
            su, bid="b_other", owner=_USER_B, platform="slack", identity="T1:U1", linked_at=_T0
        )
    finally:
        su.dispose()

    rows = c.get("/v1/me/connectors", headers=_auth(uid)).json()
    assert rows == []


def test_disconnect_severs_real_binding(client: tuple[TestClient, str]) -> None:
    """Criterion 9: after disconnect, C1's resolve_owner fails closed (the real sever).

    Not the UI chip — the actual inbound resolution path can no longer reach the
    persona, and the row is revoked (audit-preserving), not hard-deleted.
    """
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_binding(
            su, bid="b_tg", owner=uid, platform="telegram", identity="tg_123", linked_at=_T0
        )
        resolver = _resolver(su)
        # Linked before: the inbound path resolves to the owner.
        assert resolver.resolve_owner(platform="telegram", platform_identity="tg_123") == uid

        resp = c.delete("/v1/me/connectors/telegram/tg_123", headers=_auth(uid))
        assert resp.status_code == 200
        assert resp.json() == {"severed": True}

        # Severed: the inbound path now fails closed (zero access, never a persona).
        with pytest.raises(IdentityNotLinkedError):
            resolver.resolve_owner(platform="telegram", platform_identity="tg_123")
        # Audit-preserving: the row survives, flipped active → revoked (not deleted).
        status = _binding_status(su, platform="telegram", identity="tg_123")
        assert status is not None, "disconnect hard-deleted the row (should revoke)"
        assert status[0] == "revoked"
        assert status[1] is not None  # revoked_at stamped
    finally:
        su.dispose()


def test_disconnect_cross_tenant_is_noop_and_leaves_binding(
    client: tuple[TestClient, str],
) -> None:
    """Non-vacuous: A disconnecting B's binding severs nothing; B still resolves."""
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_binding(
            su, bid="b_b", owner=_USER_B, platform="discord", identity="disc_b", linked_at=_T0
        )
        resolver = _resolver(su)

        resp = c.delete("/v1/me/connectors/discord/disc_b", headers=_auth(uid))  # A attacks B
        assert resp.status_code == 200
        assert resp.json() == {"severed": False}  # RLS made it a no-op — nothing severed

        # The miss didn't sever anything: B's binding still resolves (still active).
        assert resolver.resolve_owner(platform="discord", platform_identity="disc_b") == _USER_B
        status = _binding_status(su, platform="discord", identity="disc_b")
        assert status is not None
        assert status[0] == "active"
    finally:
        su.dispose()


def test_disconnect_is_idempotent(client: tuple[TestClient, str]) -> None:
    """Locked semantics: a repeat disconnect is a clean severed=false, not a 404."""
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_binding(
            su, bid="b_sms", owner=uid, platform="sms", identity="+4790000000", linked_at=_T0
        )
    finally:
        su.dispose()

    enc = quote("+4790000000", safe="")
    first = c.delete(f"/v1/me/connectors/sms/{enc}", headers=_auth(uid))
    assert first.json() == {"severed": True}
    second = c.delete(f"/v1/me/connectors/sms/{enc}", headers=_auth(uid))
    assert second.status_code == 200, "double-disconnect must be idempotent, not an error"
    assert second.json() == {"severed": False}


# --- T3: the link-initiation proxy (POST /v1/me/connectors/{platform}/link) ---


def test_link_proxies_normalizes_and_forwards_the_caller_bearer(
    link_client: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner crosses the boundary as the caller's bearer (never a param); the artifact
    is normalized and stray upstream fields are dropped (no leak)."""
    c, uid = link_client
    captured: dict[str, str | None] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["auth"] = request.headers.get("Authorization")
        # A stray/secret upstream field MUST NOT survive the front-door.
        return httpx.Response(
            200,
            json={
                "code": "ABCD1234",
                "destination": "+15557654321",
                "expires_at": "2026-07-03T12:10:00+00:00",
                "leaked_token": "SHOULD_NOT_APPEAR",
            },
        )

    _install_mock_httpx(monkeypatch, handler)
    resp = c.post("/v1/me/connectors/sms/link", headers=_auth(uid))
    assert resp.status_code == 200
    body = resp.json()
    assert body["code"] == "ABCD1234"
    assert body["destination"] == "+15557654321"
    assert body["deep_link"] is None
    assert body["authorize_url"] is None
    assert body["expires_at"].startswith("2026-07-03T12:10:00")
    assert "leaked_token" not in body  # extra="forbid" + known-key copy → no leak
    # Forwarded to the right upstream path (no owner in it), carrying the caller's bearer.
    assert captured["url"] == f"{_CONNECTOR_URL}/v1/connectors/sms/link"
    assert captured["auth"] == f"Bearer {uid}"


def test_link_unknown_platform_is_422_before_any_proxy(
    link_client: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-enum platform is rejected by FastAPI (no upstream call) — closes path-injection."""
    c, uid = link_client
    called = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        called["n"] += 1
        return httpx.Response(200, json={"code": "x", "expires_at": "2026-07-03T12:10:00+00:00"})

    _install_mock_httpx(monkeypatch, handler)
    resp = c.post("/v1/me/connectors/..%2f..%2fevil/link", headers=_auth(uid))
    assert resp.status_code in (404, 422)  # never routed to the proxy
    assert called["n"] == 0


def test_link_fails_soft_when_service_unconfigured(client: tuple[TestClient, str]) -> None:
    """No connector_service_url → honest 503, never a dead spinner (the plain client)."""
    c, uid = client
    resp = c.post("/v1/me/connectors/telegram/link", headers=_auth(uid))
    assert resp.status_code == 503
    assert resp.json()["error"] == "connector_unavailable"


def test_link_fails_soft_on_upstream_error(
    link_client: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-2xx upstream is a single honest 'unavailable' — no upstream status leaks."""
    c, uid = link_client
    _install_mock_httpx(monkeypatch, lambda _r: httpx.Response(500, text="boom"))
    resp = c.post("/v1/me/connectors/discord/link", headers=_auth(uid))
    assert resp.status_code == 503
    assert resp.json()["error"] == "connector_unavailable"


def test_link_fails_soft_on_connection_error(
    link_client: tuple[TestClient, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dead connector service (connection refused) fails soft, never hangs or 500s."""
    c, uid = link_client

    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    _install_mock_httpx(monkeypatch, handler)
    resp = c.post("/v1/me/connectors/telegram/link", headers=_auth(uid))
    assert resp.status_code == 503
    assert resp.json()["error"] == "connector_unavailable"
