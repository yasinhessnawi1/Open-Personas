"""Integration: the R9-012 ``sidebar.changed`` ping emits at real mutation sites.

Drives the real app + Docker Postgres with a fake verifier and a recording
channel on ``app.state.event_channel``. Asserts the conversation create/delete
routes publish the data-only ping to the OWNER's scope after the durable write
(no hand-called publish — the route itself is the emitter), and that a missing
channel (community / channel unwired) degrades to a silent no-op rather than
failing the mutation.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import make_rls_engine
from persona_api.realtime.events import SidebarChangedEvent
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persona_api.realtime.events import ChannelEvent
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_PREFIX = "r9sc_"


def _auth(uid: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {uid}"}


class _RecordingChannel:
    """A UserEventChannel stand-in that records every publish."""

    def __init__(self) -> None:
        self.published: list[tuple[str, ChannelEvent]] = []

    def publish(self, owner_id: str, event: ChannelEvent) -> None:
        self.published.append((owner_id, event))


@pytest.fixture
def client(
    migrated_engine: Engine,  # noqa: ARG001 — ensures the schema is at head
    tmp_path: object,
) -> Iterator[TestClient]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    cfg = APIConfig(app_database_url=app_url, audit_root=str(tmp_path) + "/audit")
    app = create_app(cfg)

    async def _verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=f"{token}@example.test")

    with TestClient(app) as c:
        app.state.verify_token = _verify
        if hasattr(app.state, "tier_registry"):
            app.state.tier_registry = None
        yield c
        su = make_rls_engine(os.environ["DATABASE_URL"])
        with su.begin() as conn:
            conn.execute(text(f"DELETE FROM users WHERE id LIKE '{_PREFIX}%'"))
        su.dispose()


def _seed_persona(uid: str) -> None:
    su = make_rls_engine(os.environ["DATABASE_URL"])
    with su.begin() as conn:
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:pid, :uid, 'name: P')"),
            {"pid": f"{uid}_p1", "uid": uid},
        )
    su.dispose()


def _sidebar_pings(channel: _RecordingChannel) -> list[tuple[str, str]]:
    return [
        (owner, e.reason) for owner, e in channel.published if isinstance(e, SidebarChangedEvent)
    ]


def test_conversation_create_and_delete_emit_sidebar_changed(client: TestClient) -> None:
    uid = f"{_PREFIX}owner"
    # Provision the users row, then the FK parent persona.
    assert client.get("/v1/me/nav-counts", headers=_auth(uid)).status_code == 200
    _seed_persona(uid)

    channel = _RecordingChannel()
    client.app.state.event_channel = channel  # type: ignore[attr-defined]
    try:
        r = client.post(
            f"/v1/personas/{uid}_p1/conversations",
            json={"title": "t", "origin": "chat"},
            headers=_auth(uid),
        )
        assert r.status_code == 201
        conv_id = r.json()["id"]
        assert _sidebar_pings(channel) == [(uid, "conversation.created")]

        r = client.delete(f"/v1/conversations/{conv_id}", headers=_auth(uid))
        assert r.status_code == 204
        assert _sidebar_pings(channel) == [
            (uid, "conversation.created"),
            (uid, "conversation.deleted"),
        ]
    finally:
        client.app.state.event_channel = None  # type: ignore[attr-defined]


def test_missing_channel_degrades_to_noop_not_failure(client: TestClient) -> None:
    uid = f"{_PREFIX}nochan"
    assert client.get("/v1/me/nav-counts", headers=_auth(uid)).status_code == 200
    _seed_persona(uid)

    client.app.state.event_channel = None  # type: ignore[attr-defined]
    r = client.post(
        f"/v1/personas/{uid}_p1/conversations",
        json={"title": "t", "origin": "chat"},
        headers=_auth(uid),
    )
    assert r.status_code == 201  # the mutation never fails on a missing channel
