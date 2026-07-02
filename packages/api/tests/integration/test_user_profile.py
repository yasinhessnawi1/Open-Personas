"""Integration: the K6 profile surface — ``GET`` / ``PATCH /v1/me/profile``.

Drives the real app + Docker Postgres (migrated to head, so migration 028's
``first_name``/``last_name`` columns exist) with a fake verifier. Asserts: a fresh
account is null-safe (no name), a PATCH sets/clears/partial-updates the name,
names are normalised, and a caller can only ever touch their own row.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures schema (incl. migration 028) is at head
    tmp_path: object,
) -> Iterator[TestClient]:
    import os

    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(app_database_url=app_url, audit_root=str(tmp_path) + "/audit")
    app = create_app(cfg)

    async def _verify(token: str) -> AuthenticatedUser:
        # The bearer token IS the user id; email carried so the row's email is real.
        return AuthenticatedUser(id=token, email=f"{token}@example.test")

    with TestClient(app) as c:
        app.state.verify_token = _verify
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        yield c
        # Clean up any users this test provisioned (ensure_user auto-creates them).
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text("DELETE FROM users WHERE id LIKE 'k6_%'"))
        su.dispose()


def test_fresh_account_is_null_safe(client: TestClient) -> None:
    r = client.get("/v1/me/profile", headers=_auth("k6_nameless"))
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == "k6_nameless"
    assert body["first_name"] is None
    assert body["last_name"] is None
    assert body["email"] == "k6_nameless@example.test"


def test_patch_sets_then_get_reflects(client: TestClient) -> None:
    uid = "k6_ada"
    r = client.patch(
        "/v1/me/profile",
        json={"first_name": "Ada", "last_name": "Lovelace"},
        headers=_auth(uid),
    )
    assert r.status_code == 200
    assert r.json()["first_name"] == "Ada"
    assert r.json()["last_name"] == "Lovelace"

    got = client.get("/v1/me/profile", headers=_auth(uid)).json()
    assert got["first_name"] == "Ada"
    assert got["last_name"] == "Lovelace"


def test_patch_partial_leaves_other_field_untouched(client: TestClient) -> None:
    uid = "k6_partial"
    client.patch(
        "/v1/me/profile",
        json={"first_name": "Grace", "last_name": "Hopper"},
        headers=_auth(uid),
    )
    # Only last_name provided → first_name must be unchanged.
    r = client.patch("/v1/me/profile", json={"last_name": "H."}, headers=_auth(uid))
    assert r.status_code == 200
    assert r.json()["first_name"] == "Grace"
    assert r.json()["last_name"] == "H."


def test_patch_explicit_null_clears(client: TestClient) -> None:
    uid = "k6_clear"
    client.patch(
        "/v1/me/profile",
        json={"first_name": "Alan", "last_name": "Turing"},
        headers=_auth(uid),
    )
    r = client.patch("/v1/me/profile", json={"first_name": None}, headers=_auth(uid))
    assert r.status_code == 200
    assert r.json()["first_name"] is None
    assert r.json()["last_name"] == "Turing"  # untouched


def test_empty_patch_is_noop_read(client: TestClient) -> None:
    uid = "k6_noop"
    client.patch("/v1/me/profile", json={"first_name": "Edsger"}, headers=_auth(uid))
    r = client.patch("/v1/me/profile", json={}, headers=_auth(uid))
    assert r.status_code == 200
    assert r.json()["first_name"] == "Edsger"


def test_name_is_normalised(client: TestClient) -> None:
    uid = "k6_norm"
    r = client.patch(
        "/v1/me/profile",
        json={"first_name": "  \n Katherine \t "},
        headers=_auth(uid),
    )
    assert r.status_code == 200
    assert r.json()["first_name"] == "Katherine"


def test_whitespace_only_name_reads_unset(client: TestClient) -> None:
    uid = "k6_blank"
    r = client.patch("/v1/me/profile", json={"first_name": "   "}, headers=_auth(uid))
    assert r.status_code == 200
    assert r.json()["first_name"] is None


def test_overlong_name_rejected_422(client: TestClient) -> None:
    r = client.patch("/v1/me/profile", json={"first_name": "x" * 101}, headers=_auth("k6_long"))
    assert r.status_code == 422


def test_caller_only_touches_own_row(client: TestClient) -> None:
    # A patches their name; B patches theirs; neither sees the other's.
    client.patch("/v1/me/profile", json={"first_name": "AliceA"}, headers=_auth("k6_iso_a"))
    client.patch("/v1/me/profile", json={"first_name": "BobB"}, headers=_auth("k6_iso_b"))
    a = client.get("/v1/me/profile", headers=_auth("k6_iso_a")).json()
    b = client.get("/v1/me/profile", headers=_auth("k6_iso_b")).json()
    assert a["first_name"] == "AliceA"
    assert b["first_name"] == "BobB"
