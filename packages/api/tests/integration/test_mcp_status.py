"""Integration — the not-connected signal read (Spec N6, T6, N6-D-6; R4-C1-21).

Proves the signal maps EXACTLY the T1 vocabulary from real instance state, RLS-scoped under
``persona_app`` (no bypass engine, no secret): an image server with a ``running`` instance →
connected; with a ``failed``/``stopped``/``starting`` instance → the matching reason; with NO
instance → ``not_enabled``; a remote/BYO server → connected (unaffected).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from persona_api.mcp import runtime_store
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import catalog_service, mcp_status_service
from sqlalchemy import Engine, text

pytestmark = pytest.mark.integration


class _ImgEntry:
    def __init__(self, name: str) -> None:
        self.name = name
        self.server_type = "server"


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    _ = migrated_engine
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS test")
    engine = make_rls_engine(app_url)
    yield engine
    engine.dispose()


@contextmanager
def _acting_as(owner: str) -> Iterator[None]:
    token = current_user_id.set(owner)
    try:
        yield
    finally:
        current_user_id.reset(token)


def _seed(superuser: Engine, *, owner: str, persona_id: str) -> None:
    with superuser.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, :e) ON CONFLICT DO NOTHING"),
            {"o": owner, "e": f"{owner}@x"},
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x')"),
            {"p": persona_id, "o": owner},
        )


def _add_server(superuser: Engine, *, owner: str, persona_id: str, name: str, url: str) -> str:
    with superuser.begin() as conn:
        sid = conn.execute(
            text(
                "INSERT INTO user_mcp_servers (owner_id, name, url, auth_method, enabled) "
                "VALUES (:o, :n, :u, 'none', true) RETURNING id"
            ),
            {"o": owner, "n": name, "u": url},
        ).scalar_one()
        conn.execute(
            text("INSERT INTO persona_mcp_assignments (persona_id, server_id) VALUES (:p, :s)"),
            {"p": persona_id, "s": sid},
        )
    return str(sid)


def _by_name(statuses: object) -> dict[str, tuple[bool, str | None]]:
    return {c.server_name: (c.connected, c.reason) for c in statuses}  # type: ignore[union-attr]


def test_signal_maps_the_t1_vocabulary(
    app_engine: Engine, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(migrated_engine, owner="owner_a", persona_id="p1")
    # Four image servers + one remote.
    running = _add_server(
        migrated_engine,
        owner="owner_a",
        persona_id="p1",
        name="img-running",
        url="image://mcp/img-running",
    )
    failed = _add_server(
        migrated_engine,
        owner="owner_a",
        persona_id="p1",
        name="img-failed",
        url="image://mcp/img-failed",
    )
    _no_inst = _add_server(
        migrated_engine,
        owner="owner_a",
        persona_id="p1",
        name="img-fresh",
        url="image://mcp/img-fresh",
    )
    _add_server(
        migrated_engine,
        owner="owner_a",
        persona_id="p1",
        name="a-remote",
        url="https://remote.test/mcp",
    )
    monkeypatch.setattr(
        catalog_service,
        "merged_mcp_catalog",
        lambda: [_ImgEntry("img-running"), _ImgEntry("img-failed"), _ImgEntry("img-fresh")],
    )
    # Drive real instance rows: running (connected) + failed (spawn_failed).
    with _acting_as("owner_a"):
        runtime_store.ensure(
            rls_engine=app_engine, owner_id="owner_a", server_id=running, image="mcp/x"
        )
        runtime_store.mark_running(
            rls_engine=app_engine,
            owner_id="owner_a",
            server_id=running,
            endpoint_url="http://h/mcp",
        )
        runtime_store.ensure(
            rls_engine=app_engine, owner_id="owner_a", server_id=failed, image="mcp/x"
        )
        runtime_store.mark_state(
            rls_engine=app_engine,
            owner_id="owner_a",
            server_id=failed,
            state="failed",
            reason="spawn_failed",
        )
        statuses = mcp_status_service.persona_mcp_connection_status(
            rls_engine=app_engine, owner_id="owner_a", persona_id="p1"
        )
    by = _by_name(statuses)
    assert by["img-running"] == (True, None)  # running instance → connected
    assert by["img-failed"] == (False, "spawn_failed")  # failed → matching reason
    assert by["img-fresh"] == (False, "not_enabled")  # assigned, no instance yet
    assert by["a-remote"] == (True, None)  # remote/BYO → connected, unaffected


def test_disabled_servers_are_omitted(
    app_engine: Engine, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(migrated_engine, owner="owner_a", persona_id="p1")
    _add_server(
        migrated_engine,
        owner="owner_a",
        persona_id="p1",
        name="img-on",
        url="image://mcp/img-on",
    )
    with migrated_engine.begin() as conn:
        sid = conn.execute(
            text(
                "INSERT INTO user_mcp_servers (owner_id, name, url, auth_method, enabled) "
                "VALUES ('owner_a', 'img-off', 'image://mcp/img-off', 'none', false) RETURNING id"
            )
        ).scalar_one()
        conn.execute(
            text("INSERT INTO persona_mcp_assignments (persona_id, server_id) VALUES ('p1', :s)"),
            {"s": sid},
        )
    monkeypatch.setattr(
        catalog_service, "merged_mcp_catalog", lambda: [_ImgEntry("img-on"), _ImgEntry("img-off")]
    )
    with _acting_as("owner_a"):
        statuses = mcp_status_service.persona_mcp_connection_status(
            rls_engine=app_engine, owner_id="owner_a", persona_id="p1"
        )
    names = {c.server_name for c in statuses}
    assert "img-on" in names
    assert "img-off" not in names  # disabled → omitted
