"""``head_checkpoint_seq`` is the dead-job correlation key, not only a chain pointer (R9-173).

Three sites have to spell one string the same way: ``persona.tasks.entity.Task`` defines the
field, ``persona_api.tasks.handler.task_leg_idempotency_key`` mints ``task:{id}:after:{head}``
when a leg is enqueued, and ``RevivalSweeper._dead_cause_at_head`` finds the dead leg by
matching that key at the task's CURRENT head. Nothing in the type system ties the three
together, and the failure mode is silence: a park path that appends a checkpoint moves the
head, the dead row stops matching, and a transient failure is never picked up on the user's
behalf again.

So the agreement gets a test that says its name. This one is the cheap half and runs anywhere:
the key under test is minted by the real handler and matched by the real sweep, with no
Postgres in the way. The full chain (enqueue, dead-letter, park, append, sweep) is
``test_appending_a_checkpoint_at_the_park_loses_the_dead_leg`` in the integration suite.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.tasks import ScheduledFire
from persona_api.approvals.kill_switch import KillSwitchStore
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import jobs as jobs_t
from persona_api.jobs.queue import JobQueue
from persona_api.tasks import TaskLegPayload, TaskStore
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.handler import TASK_LEG_JOB_TYPE, task_leg_idempotency_key
from persona_api.tasks.revival_sweep import RevivalSweeper
from persona_api.tasks.store import CheckpointStore
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine

_NOW = datetime(2026, 9, 17, 9, 0, tzinfo=UTC)
_OWNER = "user_correlation"
_TASK = "t_correlation"
_CAUSE = "rate limit exceeded on every provider"


class _AlwaysLeader:
    """The leadership gate, held. Leadership itself is A1's, tested there."""

    def try_become_leader(self) -> bool:
        return True


@pytest.fixture
def engine(tmp_path: Path) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "correlation.db")
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="a@example.com")
    yield eng
    eng.dispose()


def _key(head: int | None, *, retry: int = 0) -> str:
    """The leg key the REAL handler mints for a leg enqueued at ``head``."""
    payload = TaskLegPayload(
        task_id=_TASK,
        predecessor_seq=head,
        trigger=ScheduledFire(schedule_id="s1", fire_time=_NOW),
    )
    return task_leg_idempotency_key(payload, retry=retry)


def _dead_leg(engine: Engine, key: str) -> None:
    """A leg that died at the head ``key`` was minted for, the shape A0 leaves behind."""
    with engine.begin() as conn:
        conn.execute(
            insert(jobs_t).values(
                id=f"job-{key}",
                type=TASK_LEG_JOB_TYPE,
                owner_id=_OWNER,
                payload={"task_id": _TASK},
                idempotency_key=key,
                state="dead",
                attempt=5,
                max_attempts=5,
                last_error=_CAUSE,
                scheduled_at=_NOW,
                created_at=_NOW,
            )
        )


def _sweeper(engine: Engine) -> RevivalSweeper:
    return RevivalSweeper(
        continuation=TaskContinuation(
            task_store=TaskStore(engine),
            queue=JobQueue(engine),
            checkpoint_store=CheckpointStore(engine),
        ),
        dispatch_engine=engine,
        rls_engine=engine,
        task_store=TaskStore(engine),
        kill_switch=KillSwitchStore(engine),
        leader=_AlwaysLeader(),  # type: ignore[arg-type]
    )


def test_the_sweep_finds_the_dead_leg_at_the_head_the_handler_keyed_it_at(engine: Engine) -> None:
    """The agreement, stated once: what the handler mints is what the sweep looks for.

    Mint the key the way an enqueue does, leave a dead row under it, and ask the sweep for the
    cause at that same head. Change the string at either site alone and this goes red, which
    is the whole point of writing it down.
    """
    _dead_leg(engine, _key(None))

    assert _sweeper(engine)._dead_cause_at_head(_TASK, None) == _CAUSE


def test_a_retried_attempt_at_the_same_head_is_still_the_same_dead_leg(engine: Engine) -> None:
    """D-W1-20's ``:retry:N`` suffix rides on the same anchor, so it must still match."""
    _dead_leg(engine, _key(None, retry=1))

    assert _sweeper(engine)._dead_cause_at_head(_TASK, None) == _CAUSE


def test_a_moved_head_loses_the_dead_leg_that_was_keyed_at_the_old_one(engine: Engine) -> None:
    """R9-173, by name: append a checkpoint and the dead row stops being findable.

    The dead leg was enqueued before the first checkpoint, so its key anchors on ``init``. One
    appended checkpoint moves the head to ``0`` and the sweep asks for ``after:0`` instead: no
    row, no cause, no revival, and nothing anywhere that says so. This asserts the CURRENT
    behaviour on purpose. Teaching the sweep to read the cause off the head checkpoint would
    decouple the two roles and is its own work; until then the rule stands, and it is written
    at all three sites: a park or gate path that appends a checkpoint must re-key or
    re-enqueue, or the dead-job match is lost.
    """
    _dead_leg(engine, _key(None))

    assert _sweeper(engine)._dead_cause_at_head(_TASK, 0) is None
