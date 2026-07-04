"""GET/POST /v1/me/notifications — the durable bell feed API (Spec P6, D4-b).

Drives the real app against Docker Postgres with a fake JWT verifier. The feed is
authored server-side (D4-c/d); these tests seed the ``notifications`` table as
superuser, then read/mutate through the endpoints. Concerns:

1. **List** — newest-first by ``created_at``; each item carries the deep-link
   fields (``kind`` / ``ref_id``) + locale-neutral copy (``message_key`` / ``params``).
2. **RLS tenant isolation** — a caller sees ONLY their own notifications (same
   posture as the table-level RLS in ``test_notifications_schema``).
3. **Pagination** — ``limit`` / ``offset`` bound the feed.
4. **Mark-read** — read-all clears the caller's unread; single mark-read touches
   one and returns 0 for a non-owned / absent id (no cross-tenant leak).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
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
    from pathlib import Path

    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


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

    user_id = "user_notif_a"
    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        yield c, user_id
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id IN ('user_notif_a', 'user_notif_b')"))
    su.dispose()


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def _seed_notification(
    engine: Engine,
    *,
    nid: str,
    owner: str,
    created_at: datetime,
    kind: str = "run_terminal",
    ref: str = "run1",
    level: str = "success",
    key: str = "notifications.run.completed",
    read: bool = False,
) -> None:
    """Seed a user + notification (superuser bypasses RLS)."""
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": owner, "e": f"{owner}@example.com"},
        )
        conn.execute(
            text(
                "INSERT INTO notifications "
                "(id, owner_id, kind, ref_id, level, message_key, read, created_at) "
                "VALUES (:i, :o, :k, :r, :l, :key, :read, :ts)"
            ),
            {
                "i": nid,
                "o": owner,
                "k": kind,
                "r": ref,
                "l": level,
                "key": key,
                "read": read,
                "ts": created_at,
            },
        )


def test_list_notifications_newest_first_with_deeplink_fields(
    client: tuple[TestClient, str],
) -> None:
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_notification(su, nid="n_old", owner=uid, created_at=_T0, ref="run_old")
        _seed_notification(
            su,
            nid="n_new",
            owner=uid,
            created_at=_T0 + timedelta(hours=1),
            kind="persona_ready",
            ref="persona_x",
            key="notifications.persona.ready",
        )
    finally:
        su.dispose()

    rows = c.get("/v1/me/notifications", headers=_auth(uid)).json()
    assert [r["id"] for r in rows] == ["n_new", "n_old"]  # newest-first
    newest = rows[0]
    assert newest["kind"] == "persona_ready"
    assert newest["ref_id"] == "persona_x"
    assert newest["message_key"] == "notifications.persona.ready"
    assert newest["read"] is False


def test_notifications_list_is_rls_scoped(client: tuple[TestClient, str]) -> None:
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_notification(su, nid="n_mine", owner=uid, created_at=_T0, ref="run_mine")
        _seed_notification(
            su, nid="n_theirs", owner="user_notif_b", created_at=_T0, ref="run_theirs"
        )
    finally:
        su.dispose()

    rows = c.get("/v1/me/notifications", headers=_auth(uid)).json()
    ids = {r["id"] for r in rows}
    assert ids == {"n_mine"}, f"RLS leak on /v1/me/notifications: caller saw {ids}"


def test_notifications_list_paginates(client: tuple[TestClient, str]) -> None:
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        for i in range(3):
            _seed_notification(
                su, nid=f"n_{i}", owner=uid, created_at=_T0 + timedelta(minutes=i), ref=f"run_{i}"
            )
    finally:
        su.dispose()

    page1 = c.get("/v1/me/notifications?limit=2&offset=0", headers=_auth(uid)).json()
    page2 = c.get("/v1/me/notifications?limit=2&offset=2", headers=_auth(uid)).json()
    assert len(page1) == 2
    assert len(page2) == 1
    assert {r["id"] for r in page1}.isdisjoint({r["id"] for r in page2})


def test_mark_all_read_clears_unread(client: tuple[TestClient, str]) -> None:
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_notification(su, nid="n1", owner=uid, created_at=_T0, ref="r1")
        _seed_notification(su, nid="n2", owner=uid, created_at=_T0, ref="r2")
    finally:
        su.dispose()

    result = c.post("/v1/me/notifications/read-all", headers=_auth(uid)).json()
    assert result["updated"] == 2
    rows = c.get("/v1/me/notifications", headers=_auth(uid)).json()
    assert all(r["read"] is True for r in rows)


def test_mark_one_read(client: tuple[TestClient, str]) -> None:
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_notification(su, nid="n1", owner=uid, created_at=_T0, ref="r1")
        _seed_notification(su, nid="n2", owner=uid, created_at=_T0, ref="r2")
    finally:
        su.dispose()

    result = c.post("/v1/me/notifications/n1/read", headers=_auth(uid)).json()
    assert result["updated"] == 1
    rows = {r["id"]: r["read"] for r in c.get("/v1/me/notifications", headers=_auth(uid)).json()}
    assert rows["n1"] is True
    assert rows["n2"] is False


def test_mark_read_on_foreign_id_is_noop(client: tuple[TestClient, str]) -> None:
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_notification(su, nid="n_theirs", owner="user_notif_b", created_at=_T0, ref="r_theirs")
    finally:
        su.dispose()

    # RLS hides the foreign row → the UPDATE matches nothing; no leak of its existence.
    result = c.post("/v1/me/notifications/n_theirs/read", headers=_auth(uid)).json()
    assert result["updated"] == 0
