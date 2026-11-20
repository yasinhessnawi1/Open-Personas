"""The chat steering port applies pause and resume through the shared controls (R9-241).

``ControlSteeringPort`` is what the loop calls during the turn. Driven on the community
engine with the real controls, so the note it hands back is exactly the one the task page
shows for that door, including the budget refusal that points to Raise cap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.errors import TaskNotFoundError
from persona.tasks import Contract, ContractBounds, CostLedger, Task, TaskState
from persona_api.db.community import create_community_schema, ensure_owner, make_community_engine
from persona_api.db.models import personas as personas_t
from persona_api.middleware.rls_context import current_user_id
from persona_api.services.task_control_service import BUDGET_REACHED_RESUME_NOTE
from persona_api.services.task_steering_service import ControlSteeringPort
from persona_api.tasks.store import TaskStore
from persona_runtime.task_origination import SteeringOutcome, SteeringVerb
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_NOW = datetime(2026, 11, 18, 9, 0, tzinfo=UTC)
_OWNER = "user_port"
_PERSONA = "persona_port"
_CAP = 50_000


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "port.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="p@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: x"))
    yield eng
    eng.dispose()


@pytest.fixture
def as_owner() -> Iterator[None]:
    token = current_user_id.set(_OWNER)
    yield
    current_user_id.reset(token)


def _task(engine: Engine, *, spent: int, paused: bool) -> None:
    TaskStore(engine).create(
        Task(
            id="t1",
            owner_id=_OWNER,
            persona_id=_PERSONA,
            contract=Contract(goal="find a flat", bounds=ContractBounds(total_budget_micros=_CAP)),
            state=TaskState.ACTIVE,
            paused=paused,
            ledger=CostLedger(model_micros=spent),
            created_at=_NOW,
            updated_at=_NOW,
        )
    )


@pytest.mark.usefixtures("as_owner")
def test_a_resume_at_the_cap_comes_back_refused_with_the_raise_cap_note(engine: Engine) -> None:
    _task(engine, spent=_CAP, paused=True)

    outcome = ControlSteeringPort(engine).steer(SteeringVerb.RESUME, "t1")

    assert outcome == SteeringOutcome(changed=False, note=BUDGET_REACHED_RESUME_NOTE)
    assert TaskStore(engine).get(_OWNER, "t1").paused is True


@pytest.mark.usefixtures("as_owner")
def test_a_pause_takes_and_a_second_pause_says_it_already_is(engine: Engine) -> None:
    _task(engine, spent=0, paused=False)
    port = ControlSteeringPort(engine)

    assert port.steer(SteeringVerb.PAUSE, "t1") == SteeringOutcome(changed=True)
    assert port.steer(SteeringVerb.PAUSE, "t1") == SteeringOutcome(
        changed=False, note="Already paused."
    )


def test_off_request_there_is_no_task_to_steer(engine: Engine) -> None:
    _task(engine, spent=0, paused=False)

    with pytest.raises(TaskNotFoundError):
        ControlSteeringPort(engine).steer(SteeringVerb.PAUSE, "t1")
