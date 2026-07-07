"""A7 T6 — unlink hygiene on the real path (criterion 7).

Unlinking a platform sends that platform's triggers dormant (``disabled_reason='unlinked'``) and
does NOT touch other platforms' triggers; re-enable is an explicit act (no silent re-enable).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.events import EnqueueInitiativeCandidate, EventKind, MessageFilter
from persona_api.config import APIConfig
from persona_api.events import EventTriggerRecord, EventTriggerStore
from persona_api.events.connector_hooks import on_connector_unlinked
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_OWNER = "own_a7_unlink"
_PERSONA = "pers_a7_unlink"
_NOW = datetime(2026, 7, 6, 9, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — migrations first
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
            {"u": _OWNER, "e": f"{_OWNER}@x.test"},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'name: A') "
                "ON CONFLICT DO NOTHING"
            ),
            {"p": _PERSONA, "u": _OWNER},
        )


def _msg_trigger(store: EventTriggerStore, trigger_id: str, platform: str) -> None:
    store.create_if_absent(
        EventTriggerRecord(
            id=trigger_id,
            owner_id=_OWNER,
            persona_id=_PERSONA,
            task_id=None,
            event_kind=EventKind.CONNECTOR_MESSAGE_RECEIVED,
            platform=platform,
            filter=MessageFilter(platform=platform),
            action=EnqueueInitiativeCandidate(),
            enabled=True,
            disabled_reason=None,
            last_fired_at=None,
            pending_coalesced_count=0,
            created_at=_NOW,
            updated_at=_NOW,
        ),
        now=_NOW,
    )


def test_unlink_disables_only_that_platforms_triggers(
    app_engine: Engine, migrated_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PERSONA_EVENT_TRIGGERS_ENABLED", "true")
    _seed(migrated_engine)
    store = EventTriggerStore(app_engine)
    _msg_trigger(store, "trg-email", "email")
    _msg_trigger(store, "trg-slack", "slack")

    severed = on_connector_unlinked(
        rls_engine=app_engine,
        config=APIConfig(credits_max_per_day=0),
        owner_id=_OWNER,
        platform="email",
        now=_NOW,
    )
    assert severed == 1  # only the one email trigger

    email = store.get(_OWNER, "trg-email")
    assert email is not None
    assert email.enabled is False
    assert email.disabled_reason == "unlinked"

    slack = store.get(_OWNER, "trg-slack")
    assert slack is not None
    assert slack.enabled is True  # a different platform is untouched

    # No silent re-enable: a second unlink is a no-op, the trigger stays dormant.
    assert (
        on_connector_unlinked(
            rls_engine=app_engine,
            config=APIConfig(credits_max_per_day=0),
            owner_id=_OWNER,
            platform="email",
            now=_NOW,
        )
        == 0
    )
    assert store.get(_OWNER, "trg-email").enabled is False  # type: ignore[union-attr]
