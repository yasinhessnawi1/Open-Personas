"""The morning-digest builder — ordering, caps, deferred-secondary, render (Spec A6, B5).

Composes the digest from real durable state (approvals + terminal tasks) on real Postgres. The
load-bearing properties: the fixed order (waiting → stuck → done → initiatives), the under-a-minute
caps with an honest overflow, and that deferred chatter is a SECONDARY input (the main sections
never depend on it). The report-builder internals are proven in core; not re-exercised here.
"""

# ruff: noqa: ARG001 — ``migrated_engine`` is a dependency-ordering fixture param.
from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from persona.approvals import ActionProposal
from persona.tasks import Contract, Task
from persona.tools import ActionCategory
from persona_api.approvals import ApprovalStore
from persona_api.config import APIConfig
from persona_api.digest import DeferredDigestItem, build_morning_digest, render_digest_message
from persona_api.services import audit_service
from persona_api.tasks.store import TaskStore
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_NOW = datetime(2026, 7, 7, 7, 0, tzinfo=UTC)
_CONFIG = APIConfig(
    database_url="postgresql+psycopg://super@localhost/persona_shell",
    app_database_url="postgresql+psycopg://persona_app@localhost/persona_shell",
)


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Iterator[Engine]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set; skipping digest-builder test")
    engine = create_engine(app_url.replace("+asyncpg", "+psycopg"))
    yield engine
    engine.dispose()


def _seed_persona(su: Engine) -> None:
    with su.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('u','u@x')"))
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) VALUES "
                "('kai','u','identity:\n  name: Kai\n')"
            )
        )


def _task(tasks: TaskStore, task_id: str, goal: str) -> None:
    tasks.create(
        Task(
            id=task_id,
            owner_id="u",
            persona_id="kai",
            contract=Contract(goal=goal),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )
    tasks.start("u", task_id, now=_NOW)


def _pending(store: ApprovalStore, pid: str, task_id: str, description: str) -> None:
    store.create_proposal(
        ActionProposal(
            proposal_id=pid,
            owner_id="u",
            task_id=task_id,
            persona_id="kai",
            categories=frozenset({ActionCategory.COMMUNICATE_AS_USER}),
            tool_name="send_email",
            arguments={"to": "x@y.com"},
            description=description,
            created_at=_NOW,
        )
    )


def test_build_composes_and_orders_the_sections(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_persona(migrated_engine)
    tasks = TaskStore(app_engine)
    _task(tasks, "t_done", "summarise the newsletters")
    tasks.complete("u", "t_done", now=_NOW)
    _task(tasks, "t_stuck", "book the dentist")
    tasks.fail("u", "t_stuck", now=_NOW)
    _task(tasks, "t_wait", "reply to the landlord")
    _pending(ApprovalStore(app_engine), "p1", "t_wait", "Reply to the landlord about the deposit")

    digest = build_morning_digest(app_engine, owner_id="u", config=_CONFIG, now=_NOW)

    kinds = [s.kind for s in digest.sections]
    assert kinds == ["waiting", "stuck", "done"]  # fixed order; initiatives empty → omitted
    assert digest.sections[0].items[0].title == "Reply to the landlord about the deposit"
    assert digest.sections[1].items[0].title == "book the dentist"
    assert digest.sections[2].items[0].title == "summarise the newsletters"
    assert digest.persona_names["kai"] == "Kai"  # self-contained for rendering
    # per-item deep-link refs (A6-D-6, W8): waiting → the approval; stuck/done → the task.
    waiting_ref = digest.sections[0].items[0].ref
    assert waiting_ref is not None
    assert (waiting_ref.kind, waiting_ref.id) == ("approval", "p1")
    stuck_ref = digest.sections[1].items[0].ref
    assert stuck_ref is not None
    assert (stuck_ref.kind, stuck_ref.id) == ("task", "t_stuck")
    done_ref = digest.sections[2].items[0].ref
    assert done_ref is not None
    assert (done_ref.kind, done_ref.id) == ("task", "t_done")

    text_render = render_digest_message(digest)
    assert text_render.index("Waiting on you") < text_render.index("Stuck")
    assert text_render.index("Stuck") < text_render.index("Done overnight")


def test_deferred_chatter_is_secondary_in_done(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_persona(migrated_engine)
    deferred = [
        DeferredDigestItem(
            id="d1",
            persona_id="kai",
            content="filed 3 receipts",
            created_at=_NOW,
            deferred_reason="progress",
        )
    ]
    # No approvals / tasks — the main sections are empty; the chatter appears under "done" alone.
    digest = build_morning_digest(
        app_engine, owner_id="u", config=_CONFIG, now=_NOW, deferred=deferred
    )
    assert [s.kind for s in digest.sections] == ["done"]
    assert digest.sections[0].items[0].title == "filed 3 receipts"


def test_under_a_minute_caps_with_honest_overflow(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    _seed_persona(migrated_engine)
    deferred = [
        DeferredDigestItem(
            id=f"d{i}",
            persona_id="kai",
            content=f"chatter {i}",
            created_at=_NOW,
            deferred_reason="progress",
        )
        for i in range(10)
    ]
    digest = build_morning_digest(
        app_engine, owner_id="u", config=_CONFIG, now=_NOW, deferred=deferred
    )
    done = digest.sections[0]
    assert len(done.items) == 6  # capped for the one-minute read
    assert done.overflow == 4  # honest "+4 more", never a wall of text


def test_empty_digest_reads_as_a_quiet_night(migrated_engine: Engine, app_engine: Engine) -> None:
    _seed_persona(migrated_engine)
    digest = build_morning_digest(app_engine, owner_id="u", config=_CONFIG, now=_NOW)
    assert digest.sections == ()
    assert render_digest_message(digest) == "Nothing to report: a quiet night."


def test_ran_because_renders_from_the_event_fired_audit_provenance(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """A7-D-9 (merge-back wiring): a task that ran from an event trigger shows "ran because: …".

    The A7 door-a writes an ``event_trigger.fired`` audit row (``target=task_id``,
    ``metadata["human"]``); the builder sources ``ran_because`` from the latest such row per task.
    A task with no fired row leaves ``ran_because`` None (no overclaim).
    """
    _seed_persona(migrated_engine)
    tasks = TaskStore(app_engine)
    _task(tasks, "t_evt", "summarise the landlord email")
    tasks.complete("u", "t_evt", now=_NOW)
    _task(tasks, "t_plain", "file the receipts")
    tasks.complete("u", "t_plain", now=_NOW)
    # The real A7 fired-audit row for the event-driven task (the exact shape door-a writes).
    audit_service.record(
        engine=app_engine,
        user_id="u",
        action="event_trigger.fired",
        target="t_evt",
        metadata={
            "trigger_id": "trg1",
            "event_kind": "connector.message_received",
            "event_id": "evt1",
            "human": "an email from landlord@example.com arrived",
            "door": "fire_task_leg",
        },
    )

    digest = build_morning_digest(app_engine, owner_id="u", config=_CONFIG, now=_NOW)

    done_items = {i.title: i for s in digest.sections if s.kind == "done" for i in s.items}
    assert (
        done_items["summarise the landlord email"].ran_because
        == "an email from landlord@example.com arrived"
    )
    assert done_items["file the receipts"].ran_because is None  # never ran from an event
    # …and the render carries it (the one string the user reads).
    assert "ran because: an email from landlord@example.com arrived" in render_digest_message(
        digest
    )
