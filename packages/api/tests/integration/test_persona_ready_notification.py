"""Server-authored persona-ready notifications (Spec P6, D4-d).

Drives the REAL avatar-landed chokepoints against Docker Postgres:

1. In-process — ``persona_service.set_avatar_url`` (the create-path hook) writes a
   ``persona_ready`` notification with the persona name in ``params``.
2. **Idempotent** — a repeated ``set_avatar_url`` (or the queue path also firing)
   converges on ONE row via ``(owner, kind, ref_id)`` (P6-D-11/D-P6-4).
3. Queue-path core — ``persona_service.write_persona_ready`` on an owner-scoped
   connection (exactly what the durable-queue handler calls) writes the row.
4. **Best-effort** — when the notification write fails, ``set_avatar_url`` still
   sets the avatar and no exception escapes (D-P6-12).
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from persona_api.db.engine import rls_connection
from persona_api.middleware.rls_context import current_user_id, make_rls_engine
from persona_api.services import notifications_service, persona_service
from sqlalchemy import text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "user_pready_a"
_PERSONA = "persona_pready_a"


@pytest.fixture
def seeded(migrated_engine: Engine) -> Iterator[Engine]:
    """Seed owner → persona (named, avatar null); yield the superuser engine."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e)"),
            {"u": _OWNER, "e": f"{_OWNER}@example.com"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml, avatar_url) "
                "VALUES (:p, :o, 'identity:\n  name: Ada', NULL)"
            ),
            {"p": _PERSONA, "o": _OWNER},
        )
    yield migrated_engine
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :u"), {"u": _OWNER})


@pytest.fixture
def app_engine() -> Iterator[Engine]:
    """The non-superuser app engine, owner bound (as the create hook / worker do)."""
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    engine = make_rls_engine(app_url)
    token = current_user_id.set(_OWNER)
    try:
        yield engine
    finally:
        current_user_id.reset(token)
        engine.dispose()


def _notifications(engine: Engine) -> list[dict[str, object]]:
    with engine.begin() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text("SELECT * FROM notifications WHERE owner_id = :o"), {"o": _OWNER}
            )
            .mappings()
            .all()
        ]


def test_set_avatar_url_writes_persona_ready(seeded: Engine, app_engine: Engine) -> None:
    persona_service.set_avatar_url(
        rls_engine=app_engine, persona_id=_PERSONA, avatar_url="uploads/x.png"
    )
    rows = _notifications(seeded)
    assert len(rows) == 1
    row = rows[0]
    assert row["kind"] == "persona_ready"
    assert row["ref_id"] == _PERSONA
    assert row["level"] == "success"
    assert row["message_key"] == "notifications.persona.ready"
    assert row["params"] == {"persona": "Ada"}


def test_persona_ready_is_idempotent(seeded: Engine, app_engine: Engine) -> None:
    persona_service.set_avatar_url(
        rls_engine=app_engine, persona_id=_PERSONA, avatar_url="uploads/x.png"
    )
    # A second landing (or the queue path also firing) converges on one row.
    persona_service.set_avatar_url(
        rls_engine=app_engine, persona_id=_PERSONA, avatar_url="uploads/x.png"
    )
    assert len(_notifications(seeded)) == 1


def test_write_persona_ready_on_connection_is_the_queue_core(
    seeded: Engine, app_engine: Engine
) -> None:
    # Exactly what the durable-queue handler calls: write on an owner-scoped conn.
    with rls_connection(app_engine, _OWNER) as conn:
        persona_service.write_persona_ready(conn, _PERSONA)
    rows = _notifications(seeded)
    assert len(rows) == 1
    assert rows[0]["kind"] == "persona_ready"
    assert rows[0]["params"] == {"persona": "Ada"}


def test_avatar_write_commits_when_notification_fails(
    seeded: Engine, app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(**_kwargs: object) -> None:
        raise RuntimeError("feed down")

    monkeypatch.setattr(notifications_service, "create_notification", _boom)
    # Best-effort: no exception escapes...
    persona_service.set_avatar_url(
        rls_engine=app_engine, persona_id=_PERSONA, avatar_url="uploads/x.png"
    )
    # ...the avatar write still committed...
    with seeded.begin() as conn:
        avatar = conn.execute(
            text("SELECT avatar_url FROM personas WHERE id = :p"), {"p": _PERSONA}
        ).scalar_one()
    assert avatar == "uploads/x.png"
    # ...and no notification row was written.
    assert _notifications(seeded) == []
