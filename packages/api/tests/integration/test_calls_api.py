"""GET /v1/calls — the voice-call history surface (Spec V9, V9-D-5).

Drives the real app against Docker Postgres with a fake JWT verifier. The
call-records have no API write path (the API-free voice ``CallRecorder`` authors
them), so the tests seed the ``calls`` table directly as superuser, then read
through the endpoint. Concerns:

1. **List + transcript link** — newest-first by ``started_at``; each item carries
   ``conversation_id`` (the link to ``GET /v1/conversations/{id}``) + the envelope
   (persona / time / duration / end_reason).
2. **RLS tenant isolation** — a caller sees ONLY their own calls (same posture as
   the T3 calls-table RLS).
3. **Pagination** — ``limit`` / ``offset`` bound the unbounded history.
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

    user_id = "user_calls_a"
    with TestClient(app) as c:
        app.state.verify_token = _fake_verify
        yield c, user_id
    # cleanup: drop the seeded tenants (cascades to conversations + calls).
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id IN ('user_calls_a', 'user_calls_b')"))
    su.dispose()


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


def _seed_call(
    engine: Engine,
    *,
    owner_id: str,
    call_id: str,
    started_at: datetime,
    duration_s: int | None = None,
    end_reason: str | None = None,
    title: str = "",
) -> None:
    """Seed a user → persona → conversation → call chain (superuser bypasses RLS).

    ``title`` (R9-028) seeds the call-hosting conversation's title directly —
    the ``calls`` table has none of its own; ``GET /v1/calls`` joins it in from
    ``conversations.title``.
    """
    persona_id = f"p_{owner_id}"
    conv_id = f"c_{call_id}"
    with engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": owner_id, "e": f"{owner_id}@example.com"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'y') "
                "ON CONFLICT DO NOTHING"
            ),
            {"p": persona_id, "u": owner_id},
        )
        conn.execute(
            text(
                "INSERT INTO conversations (id, owner_id, persona_id, origin, title) "
                "VALUES (:c, :u, :p, 'call', :t)"
            ),
            {"c": conv_id, "u": owner_id, "p": persona_id, "t": title},
        )
        conn.execute(
            text(
                "INSERT INTO calls (call_id, conversation_id, persona_id, owner_id, started_at, "
                "ended_at, duration_s, end_reason) "
                "VALUES (:cid, :conv, :p, :u, :start, :ended, :dur, :reason)"
            ),
            {
                "cid": call_id,
                "conv": conv_id,
                "p": persona_id,
                "u": owner_id,
                "start": started_at,
                "ended": started_at + timedelta(seconds=duration_s) if duration_s else None,
                "dur": duration_s,
                "reason": end_reason,
            },
        )


def test_list_calls_newest_first_with_transcript_link(
    client: tuple[TestClient, str],
) -> None:
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_call(su, owner_id=uid, call_id="call_old", started_at=_T0)
        _seed_call(
            su,
            owner_id=uid,
            call_id="call_new",
            started_at=_T0 + timedelta(hours=1),
            duration_s=125,
            end_reason="disconnect",
            title="Bergen trip planning",
        )
    finally:
        su.dispose()

    rows = c.get("/v1/calls", headers=_auth(uid)).json()
    assert [r["call_id"] for r in rows] == ["call_new", "call_old"]  # newest-first
    newest = rows[0]
    # the transcript link + the envelope.
    assert newest["conversation_id"] == "c_call_new"
    assert newest["persona_id"] == f"p_{uid}"
    assert newest["duration_s"] == 125
    assert newest["end_reason"] == "disconnect"
    # R9-028: the call row surfaces its conversation's (title_refresh-derived) title.
    assert newest["title"] == "Bergen trip planning"
    # a live/just-seeded call with no end is still listed (envelope nullable);
    # an untitled call (no title_refresh run yet) reads as "" — never null/missing.
    assert rows[1]["ended_at"] is None
    assert rows[1]["duration_s"] is None
    assert rows[1]["end_reason"] is None
    assert rows[1]["title"] == ""


def test_calls_list_is_rls_scoped(client: tuple[TestClient, str]) -> None:
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        _seed_call(su, owner_id=uid, call_id="call_mine", started_at=_T0)
        _seed_call(su, owner_id="user_calls_b", call_id="call_theirs", started_at=_T0)
    finally:
        su.dispose()

    rows = c.get("/v1/calls", headers=_auth(uid)).json()
    ids = {r["call_id"] for r in rows}
    assert ids == {"call_mine"}, f"RLS leak on /v1/calls: caller saw {ids}"


def test_calls_list_paginates(client: tuple[TestClient, str]) -> None:
    c, uid = client
    su = make_rls_engine(os.environ["DATABASE_URL"])
    try:
        for i in range(3):
            _seed_call(su, owner_id=uid, call_id=f"call_{i}", started_at=_T0 + timedelta(minutes=i))
    finally:
        su.dispose()

    page1 = c.get("/v1/calls?limit=2&offset=0", headers=_auth(uid)).json()
    page2 = c.get("/v1/calls?limit=2&offset=2", headers=_auth(uid)).json()
    assert len(page1) == 2
    assert len(page2) == 1
    # newest-first, no overlap across pages.
    assert {r["call_id"] for r in page1}.isdisjoint({r["call_id"] for r in page2})
