"""Integration tests for the A7 event-trigger registry store (Spec A7, T2).

Runs on the real Postgres under the **non-superuser** ``persona_app`` role (``APP_DATABASE_URL``),
so RLS is proven non-vacuous (hard condition 3): owner B's events never match A's triggers, and B's
own DO. Also covers the T2-gate conditions: the ONE-write-path filter+platform sync invariant, the
reflect-on-PK-conflict idempotency, the atomic cooldown claim (sequential; the concurrent case is
T3's storm test), the unlink-hygiene disable, and FK CASCADE on persona/task deletion.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from persona.events import (
    EnqueueInitiativeCandidate,
    EventKind,
    FireTaskLeg,
    LifecycleFilter,
    LinkFilter,
    MessageFilter,
)
from persona_api.events import EventTriggerRecord, EventTriggerStore
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_A = "own_a7_a"
_B = "own_a7_b"
_PERSONA_A = "pers_a7_a"
_PERSONA_B = "pers_a7_b"
_TASK_A = "task_a7_a"
_NOW = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — ordering dep: migrate+truncate first
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping RLS store test")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed(migrated_engine: Engine) -> None:
    """Seed the two owners, their personas, and one door-a task (all FK parents)."""
    with migrated_engine.begin() as conn:
        for owner in (_A, _B):
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:u, :e) ON CONFLICT DO NOTHING"),
                {"u": owner, "e": f"{owner}@x.test"},
            )
        for persona, owner in ((_PERSONA_A, _A), (_PERSONA_B, _B)):
            conn.execute(
                text(
                    "INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :u, 'name: A') "
                    "ON CONFLICT DO NOTHING"
                ),
                {"p": persona, "u": owner},
            )
        conn.execute(
            text(
                "INSERT INTO tasks (id, owner_id, persona_id, contract_json) "
                "VALUES (:t, :u, :p, '{}') ON CONFLICT DO NOTHING"
            ),
            {"t": _TASK_A, "u": _A, "p": _PERSONA_A},
        )


def _message_trigger(trigger_id: str, owner: str, persona: str) -> EventTriggerRecord:
    return EventTriggerRecord(
        id=trigger_id,
        owner_id=owner,
        persona_id=persona,
        task_id=None,
        event_kind=EventKind.CONNECTOR_MESSAGE_RECEIVED,
        platform=None,  # ignored by the store — derived from filter (the sync invariant)
        filter=MessageFilter(platform="email", sender="landlord@example.com", keywords=("rent",)),
        action=EnqueueInitiativeCandidate(),
        enabled=True,
        disabled_reason=None,
        last_fired_at=None,
        pending_coalesced_count=0,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_round_trip_and_filter_platform_sync(app_engine: Engine, migrated_engine: Engine) -> None:
    _seed(migrated_engine)
    store = EventTriggerStore(app_engine)
    stored = store.create_if_absent(_message_trigger("trg-rt", _A, _PERSONA_A), now=_NOW)

    # The ONE write path derived platform FROM the filter (the sync invariant) — even though the
    # record passed platform=None, the message filter's platform lands on the column.
    assert stored.platform == "email"
    fetched = store.get(_A, "trg-rt")
    assert fetched is not None
    assert fetched.platform == "email"
    assert isinstance(fetched.filter, MessageFilter)
    assert fetched.filter.keywords == ("rent",)
    assert isinstance(fetched.action, EnqueueInitiativeCandidate)


def test_lifecycle_trigger_has_no_platform(app_engine: Engine, migrated_engine: Engine) -> None:
    _seed(migrated_engine)
    store = EventTriggerStore(app_engine)
    record = EventTriggerRecord(
        id="trg-life",
        owner_id=_A,
        persona_id=_PERSONA_A,
        task_id=_TASK_A,
        event_kind=EventKind.TASK_LEG_FAILED,
        platform=None,
        filter=LifecycleFilter(task_id=_TASK_A, min_failure_count=2),
        action=FireTaskLeg(task_id=_TASK_A),
        enabled=True,
        disabled_reason=None,
        last_fired_at=None,
        pending_coalesced_count=0,
        created_at=_NOW,
        updated_at=_NOW,
    )
    stored = store.create_if_absent(record, now=_NOW)
    assert stored.platform is None  # lifecycle filters carry no platform
    assert stored.task_id == _TASK_A  # door-a target


def test_create_if_absent_reflects_existing_on_conflict(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    _seed(migrated_engine)
    store = EventTriggerStore(app_engine)
    first = store.create_if_absent(_message_trigger("trg-idem", _A, _PERSONA_A), now=_NOW)
    # A re-confirm with the SAME id but a different filter must NOT overwrite — it reflects the
    # stored row (idempotency rides the PK; A4-D-X-create-seam), never errors.
    conflicting = _message_trigger("trg-idem", _A, _PERSONA_A).model_copy(
        update={"filter": MessageFilter(keywords=("different",))}
    )
    second = store.create_if_absent(conflicting, now=_NOW + timedelta(hours=1))
    assert second.id == first.id
    assert isinstance(second.filter, MessageFilter)
    assert second.filter.keywords == ("rent",)  # the ORIGINAL, not the conflicting write


def test_list_active_is_kind_scoped_and_enabled_only(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    _seed(migrated_engine)
    store = EventTriggerStore(app_engine)
    store.create_if_absent(_message_trigger("trg-on", _A, _PERSONA_A), now=_NOW)
    disabled = _message_trigger("trg-off", _A, _PERSONA_A).model_copy(update={"enabled": False})
    store.create_if_absent(disabled, now=_NOW)
    # A different-kind trigger must not appear in the message match set.
    other_kind = EventTriggerRecord(
        id="trg-life2",
        owner_id=_A,
        persona_id=_PERSONA_A,
        task_id=_TASK_A,
        event_kind=EventKind.TASK_LEG_COMPLETED,
        platform=None,
        filter=LifecycleFilter(task_id=_TASK_A),
        action=FireTaskLeg(task_id=_TASK_A),
        enabled=True,
        disabled_reason=None,
        last_fired_at=None,
        pending_coalesced_count=0,
        created_at=_NOW,
        updated_at=_NOW,
    )
    store.create_if_absent(other_kind, now=_NOW)

    matched = store.list_active(_A, EventKind.CONNECTOR_MESSAGE_RECEIVED)
    assert {r.id for r in matched} == {"trg-on"}  # enabled + right kind only


def test_pause_resume_and_delete(app_engine: Engine, migrated_engine: Engine) -> None:
    _seed(migrated_engine)
    store = EventTriggerStore(app_engine)
    store.create_if_absent(_message_trigger("trg-life-cycle", _A, _PERSONA_A), now=_NOW)

    paused = store.pause(_A, "trg-life-cycle", now=_NOW)
    assert paused is not None
    assert paused.enabled is False
    assert paused.disabled_reason == "user_paused"
    assert store.list_active(_A, EventKind.CONNECTOR_MESSAGE_RECEIVED) == []

    resumed = store.resume(_A, "trg-life-cycle", now=_NOW)
    assert resumed is not None
    assert resumed.enabled is True
    assert resumed.disabled_reason is None

    assert store.delete(_A, "trg-life-cycle") is True
    assert store.get(_A, "trg-life-cycle") is None
    assert store.delete(_A, "missing") is False


def test_unlink_disables_platform_triggers_and_no_silent_reenable(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    _seed(migrated_engine)
    store = EventTriggerStore(app_engine)
    store.create_if_absent(_message_trigger("trg-email", _A, _PERSONA_A), now=_NOW)
    # A link-kind trigger on the same platform is also swept.
    link_trigger = EventTriggerRecord(
        id="trg-link",
        owner_id=_A,
        persona_id=_PERSONA_A,
        task_id=None,
        event_kind=EventKind.CONNECTOR_UNLINKED,
        platform=None,
        filter=LinkFilter(platform="email"),
        action=EnqueueInitiativeCandidate(),
        enabled=True,
        disabled_reason=None,
        last_fired_at=None,
        pending_coalesced_count=0,
        created_at=_NOW,
        updated_at=_NOW,
    )
    store.create_if_absent(link_trigger, now=_NOW)

    severed = store.disable_for_platform(_A, "email", now=_NOW)
    assert severed == 2
    disabled = store.get(_A, "trg-email")
    assert disabled is not None
    assert disabled.enabled is False
    assert disabled.disabled_reason == "unlinked"
    # Re-link does NOT silently re-enable: nothing re-enables it (criterion 7).
    assert store.disable_for_platform(_A, "email", now=_NOW) == 0  # already dormant
    assert store.get(_A, "trg-email").enabled is False  # type: ignore[union-attr]


def test_claim_fire_coalesces_within_cooldown_then_fires_after(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    _seed(migrated_engine)
    store = EventTriggerStore(app_engine)
    store.create_if_absent(_message_trigger("trg-storm", _A, _PERSONA_A), now=_NOW)

    first = store.claim_fire(_A, "trg-storm", now=_NOW, cooldown_seconds=300)
    assert first.fired is True  # first fire...
    assert first.coalesced_count == 0  # ...nothing coalesced yet

    # Two more arrivals inside the window coalesce (do not fire), accumulating the count.
    c1 = store.claim_fire(_A, "trg-storm", now=_NOW + timedelta(seconds=10), cooldown_seconds=300)
    c2 = store.claim_fire(_A, "trg-storm", now=_NOW + timedelta(seconds=20), cooldown_seconds=300)
    assert c1.fired is False
    assert c1.coalesced_count == 1
    assert c2.fired is False
    assert c2.coalesced_count == 2

    # After the cooldown, the next arrival fires and reports the 2 it coalesced.
    later = store.claim_fire(
        _A, "trg-storm", now=_NOW + timedelta(seconds=400), cooldown_seconds=300
    )
    assert later.fired is True
    assert later.coalesced_count == 2
    # ...and the pending count reset — an immediate follow-up coalesces from zero again.
    after = store.claim_fire(
        _A, "trg-storm", now=_NOW + timedelta(seconds=405), cooldown_seconds=300
    )
    assert after.fired is False
    assert after.coalesced_count == 1


def test_rls_is_non_vacuous_under_non_superuser(
    app_engine: Engine, migrated_engine: Engine
) -> None:
    _seed(migrated_engine)
    store = EventTriggerStore(app_engine)
    store.create_if_absent(_message_trigger("trg-owned-by-a", _A, _PERSONA_A), now=_NOW)
    store.create_if_absent(_message_trigger("trg-owned-by-b", _B, _PERSONA_B), now=_NOW)

    # B cannot see A's trigger (RLS hides it) — non-vacuous the strict way.
    assert store.get(_B, "trg-owned-by-a") is None
    assert [r.id for r in store.list_active(_B, EventKind.CONNECTOR_MESSAGE_RECEIVED)] == [
        "trg-owned-by-b"
    ]
    # ...and B's OWN trigger DOES match (the non-vacuous half — RLS isn't just hiding everything).
    assert store.get(_A, "trg-owned-by-a") is not None
    assert [r.id for r in store.list_active(_A, EventKind.CONNECTOR_MESSAGE_RECEIVED)] == [
        "trg-owned-by-a"
    ]
    # B cannot fire A's trigger either (claim under B's scope touches zero rows).
    claim = store.claim_fire(_B, "trg-owned-by-a", now=_NOW, cooldown_seconds=300)
    assert claim.fired is False
    assert claim.coalesced_count == 0


def test_persona_delete_cascades_the_trigger(app_engine: Engine, migrated_engine: Engine) -> None:
    _seed(migrated_engine)
    store = EventTriggerStore(app_engine)
    store.create_if_absent(_message_trigger("trg-cascade", _A, _PERSONA_A), now=_NOW)
    assert store.get(_A, "trg-cascade") is not None
    # Deleting the persona CASCADE-removes its triggers (DDL choice 1 — no orphan reaction).
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM personas WHERE id = :p"), {"p": _PERSONA_A})
    assert store.get(_A, "trg-cascade") is None
