"""One door for "just run this": an ad hoc task and its first leg (Spec W1, T2; D-W1-1).

Drives the REAL ``dispatch_ad_hoc`` against a real database (the community SQLite engine:
real FK, CHECK and default enforcement) with a recording queue in place of A0's Postgres-only
enqueue. What is pinned: the task that lands (ad hoc, active, the brief as its goal, the
default category policy), the leg that is enqueued (the first-leg key, the dispatch trigger),
the two refusals (a persona that is not the caller's; a brief over the cap, refused with a
human sentence), and that nothing is written when a refusal fires.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.errors import PersonaNotFoundError
from persona.tasks import TaskKind, TaskState
from persona.tools import DEFAULT_POLICY
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import personas as personas_t
from persona_api.db.models import tasks as tasks_t
from persona_api.errors import WorkBriefTooLongError
from persona_api.services.work_dispatch_service import (
    BRIEF_TOO_LONG_MESSAGE,
    MAX_BRIEF_CHARS,
    dispatch_ad_hoc,
)
from persona_api.tasks import TaskStore, task_leg_idempotency_key
from persona_api.tasks.handler import TaskLegPayload
from sqlalchemy import func, insert, select

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 6, 9, 0, tzinfo=UTC)
_OWNER = "user_dispatch"
_PERSONA = "persona_dispatch"
_BRIEF = "list the three newest AI persona repos on GitHub"


class _Queue:
    """Records enqueues the way A0's queue would key them (the Postgres queue is A0's)."""

    def __init__(self) -> None:
        self.enqueued: list[dict[str, object]] = []

    def enqueue(self, **kwargs: object) -> None:
        self.enqueued.append(kwargs)


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "dispatch.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="d@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


def _task_count(engine: Engine) -> int:
    with engine.begin() as conn:
        return int(conn.execute(select(func.count()).select_from(tasks_t)).scalar_one())


# --- the happy path ------------------------------------------------------------------


def test_a_dispatch_creates_an_active_ad_hoc_task_with_the_brief_as_its_goal(
    engine: Engine,
) -> None:
    queue = _Queue()
    dispatched = dispatch_ad_hoc(
        rls_engine=engine,
        queue=queue,  # type: ignore[arg-type]
        owner_id=_OWNER,
        persona_id=_PERSONA,
        brief=f"  {_BRIEF}  ",
        now=_NOW,
    )
    assert dispatched.kind == "ad_hoc"
    assert dispatched.state == "active"
    task = TaskStore(engine).get(_OWNER, dispatched.task_id)
    assert task.kind is TaskKind.AD_HOC
    assert task.state is TaskState.ACTIVE  # started, so the first leg may append
    assert task.contract.goal == _BRIEF  # stripped, verbatim
    assert task.contract.category_policy == DEFAULT_POLICY  # the consent envelope (D-W1-7)
    assert task.schedule_id is None
    assert task.conversation_id is None


def test_a_dispatch_enqueues_exactly_the_first_leg_with_the_dispatch_trigger(
    engine: Engine,
) -> None:
    queue = _Queue()
    dispatched = dispatch_ad_hoc(
        rls_engine=engine,
        queue=queue,  # type: ignore[arg-type]
        owner_id=_OWNER,
        persona_id=_PERSONA,
        brief=_BRIEF,
        now=_NOW,
    )
    assert len(queue.enqueued) == 1
    job = queue.enqueued[0]
    assert job["type"] == "task_leg"
    assert job["owner_id"] == _OWNER
    payload = TaskLegPayload.model_validate(job["payload"])
    assert payload.task_id == dispatched.task_id
    assert payload.predecessor_seq is None  # the first leg (A2-R-4 anchor "init")
    assert payload.trigger.kind == "user_dispatch"
    assert job["idempotency_key"] == task_leg_idempotency_key(payload)
    assert job.get("scheduled_at") is None  # now, not later


def test_two_dispatches_are_two_tasks(engine: Engine) -> None:
    queue = _Queue()
    first = dispatch_ad_hoc(
        rls_engine=engine,
        queue=queue,
        owner_id=_OWNER,
        persona_id=_PERSONA,
        brief=_BRIEF,  # type: ignore[arg-type]
    )
    second = dispatch_ad_hoc(
        rls_engine=engine,
        queue=queue,
        owner_id=_OWNER,
        persona_id=_PERSONA,
        brief=_BRIEF,  # type: ignore[arg-type]
    )
    assert first.task_id != second.task_id
    assert _task_count(engine) == 2


# --- the refusals ----------------------------------------------------------------------


def test_a_persona_that_is_not_the_callers_is_not_found_and_nothing_is_written(
    engine: Engine,
) -> None:
    queue = _Queue()
    with pytest.raises(PersonaNotFoundError):
        dispatch_ad_hoc(
            rls_engine=engine,
            queue=queue,  # type: ignore[arg-type]
            owner_id=_OWNER,
            persona_id="someone-elses-persona",
            brief=_BRIEF,
        )
    assert _task_count(engine) == 0
    assert queue.enqueued == []


def test_a_brief_over_the_cap_is_refused_with_a_human_sentence_before_any_write(
    engine: Engine,
) -> None:
    queue = _Queue()
    with pytest.raises(WorkBriefTooLongError) as excinfo:
        dispatch_ad_hoc(
            rls_engine=engine,
            queue=queue,  # type: ignore[arg-type]
            owner_id=_OWNER,
            persona_id=_PERSONA,
            brief="x" * (MAX_BRIEF_CHARS + 1),
        )
    assert excinfo.value.message == BRIEF_TOO_LONG_MESSAGE
    assert "—" not in BRIEF_TOO_LONG_MESSAGE
    assert excinfo.value.context["max_chars"] == str(MAX_BRIEF_CHARS)
    assert _task_count(engine) == 0
    assert queue.enqueued == []


def test_a_brief_exactly_at_the_cap_is_accepted(engine: Engine) -> None:
    dispatched = dispatch_ad_hoc(
        rls_engine=engine,
        queue=_Queue(),  # type: ignore[arg-type]
        owner_id=_OWNER,
        persona_id=_PERSONA,
        brief="y" * MAX_BRIEF_CHARS,
    )
    assert len(TaskStore(engine).get(_OWNER, dispatched.task_id).contract.goal) == MAX_BRIEF_CHARS
