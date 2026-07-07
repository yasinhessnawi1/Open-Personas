"""The A7 storm concurrency proof — CONCURRENT arrivals fire exactly once (Spec A7, T3).

The binding condition from the DDL gate: the ``claim_fire`` cooldown claim is atomic, so a burst of
arrivals hitting the dispatcher AT THE SAME TIME (real threads, real Postgres) produces exactly ONE
fire — the rest coalesce. This is the concurrent case the T2 store test's sequential claim could not
prove; it drives the whole dispatcher, not just the store primitive.
"""

from __future__ import annotations

import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from persona.events import (
    ConnectorMessageReceived,
    EnqueueInitiativeCandidate,  # noqa: F401 — parity import; door-a is exercised here
    EventKind,
    FireTaskLeg,
    MessageFilter,
)
from persona_api.events import (
    DispatchDisposition,
    EventDispatcher,
    EventTriggerRecord,
    EventTriggerStore,
)
from persona_api.middleware.rls_context import make_rls_engine
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_OWNER = "own_a7_storm"
_PERSONA = "pers_a7_storm"
_TASK = "task_a7_storm"
_NOW = datetime(2026, 7, 5, 9, 0, tzinfo=UTC)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:  # noqa: ARG001 — ordering dep: migrate+truncate first
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping storm test")
    engine = make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


class _ThreadSafeQueue:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.enqueued: list[dict[str, Any]] = []

    def enqueue(self, **kwargs: Any) -> None:  # noqa: ANN401
        with self._lock:
            self.enqueued.append(kwargs)


class _FakeTaskStore:
    def get(self, owner_id: str, task_id: str) -> object:  # noqa: ARG002
        return SimpleNamespace(head_checkpoint_seq=1)


class _ThreadSafeRecorder:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.audits: list[tuple[str, str, str, object]] = []

    def audit(self, owner: str, action: str, target: str, meta: object) -> None:
        with self._lock:
            self.audits.append((owner, action, target, meta))

    def surface(self, owner: str, trigger_id: str, human: str) -> None:  # noqa: ARG002
        return


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
        conn.execute(
            text(
                "INSERT INTO tasks (id, owner_id, persona_id, contract_json) "
                "VALUES (:t, :u, :p, '{}') ON CONFLICT DO NOTHING"
            ),
            {"t": _TASK, "u": _OWNER, "p": _PERSONA},
        )


def _event() -> ConnectorMessageReceived:
    return ConnectorMessageReceived(
        event_id="evt-storm",
        owner_id=_OWNER,
        occurred_at=_NOW,
        platform="email",
        sender_id="landlord@example.com",
        body="rent",
        conversation_id="conv-1",
        persona_id=_PERSONA,
        message_id="msg-1",
    )


def test_concurrent_arrivals_fire_exactly_once(app_engine: Engine, migrated_engine: Engine) -> None:
    _seed(migrated_engine)
    store = EventTriggerStore(app_engine)
    store.create_if_absent(
        EventTriggerRecord(
            id="trg-storm",
            owner_id=_OWNER,
            persona_id=_PERSONA,
            task_id=_TASK,
            event_kind=EventKind.CONNECTOR_MESSAGE_RECEIVED,
            platform="email",
            filter=MessageFilter(platform="email"),
            action=FireTaskLeg(task_id=_TASK),
            enabled=True,
            disabled_reason=None,
            last_fired_at=None,
            pending_coalesced_count=0,
            created_at=_NOW,
            updated_at=_NOW,
        ),
        now=_NOW,
    )
    queue, rec = _ThreadSafeQueue(), _ThreadSafeRecorder()
    dispatcher = EventDispatcher(
        store=store,
        queue=queue,  # type: ignore[arg-type]
        task_store=_FakeTaskStore(),  # type: ignore[arg-type]
        settings_cooldown_seconds=300,
        settings_max_chain_depth=3,
        audit=rec.audit,
        surface_drop=rec.surface,
    )

    fanout = 12
    barrier = threading.Barrier(fanout)

    def _arrive() -> list[DispatchDisposition]:
        barrier.wait()  # release all threads at once — a genuine simultaneous burst
        return [o.disposition for o in dispatcher.dispatch(_event(), now=_NOW)]

    with ThreadPoolExecutor(max_workers=fanout) as pool:
        results = [d for future in pool.map(lambda _i: _arrive(), range(fanout)) for d in future]

    fired = [d for d in results if d is DispatchDisposition.FIRED]
    coalesced = [d for d in results if d is DispatchDisposition.COALESCED]
    assert len(fired) == 1  # EXACTLY one arrival won the window under concurrency
    assert len(coalesced) == fanout - 1  # every other arrival coalesced
    assert len(queue.enqueued) == 1  # exactly one leg enqueued — no storm
