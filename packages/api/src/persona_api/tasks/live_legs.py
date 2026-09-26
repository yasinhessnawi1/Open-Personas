"""Whether a task has a leg still to run: one base question, two askers (R9-158).

Both filter a task's own leg jobs, and differ only in which of them count:

- the revival sweep (R9-148) asks whether ANY leg is still going to run, running included,
  so it never revives a task something is already working on (:func:`live_task_leg_ids`);
- the task detail asks whether the NEXT leg is about to start, so the page can say
  "Starting" between a Resume and the worker picking the leg up (:func:`has_starting_leg`).
  A leg queued for later (a timed continuation, a retry backing off) is not starting, so
  it counts a leg claimed, or queued and due, or running whose run row has not opened yet
  (the handler opens it a moment after the job starts running; until then the page would
  otherwise show neither a live run nor "Starting", and stop polling in the gap).

Migration 058 indexes the shared base (``idx_jobs_live_task_leg``), and the SQL here is
written so the planner can use it even under a generic plan (a prepared statement reused
with any task id): the payload key, the job type and the states are rendered as SQL
literals, and on Postgres the key expression is spelled exactly as the index spells it.
Rendered as bind parameters, the planner cannot prove a generic plan's WHERE implies the
partial index's predicate, nor match ``payload ->> $1`` to ``payload ->> 'task_id'``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Final

from sqlalchemy import Text, and_, bindparam, exists, literal_column, or_, select
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.sql.expression import ColumnElement

from persona_api.db.engine import rls_connection
from persona_api.db.models import jobs as jobs_t
from persona_api.db.models import runs as runs_t
from persona_api.tasks.handler import TASK_LEG_JOB_TYPE

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import Engine, Select
    from sqlalchemy.sql.compiler import SQLCompiler

__all__ = [
    "LIVE_JOB_STATES",
    "STARTING_JOB_STATES",
    "has_starting_leg",
    "live_task_leg_ids",
    "starting_task_leg_ids",
]

#: The states that mean a job is still going to run (or is running). Anything else is spent.
LIVE_JOB_STATES: Final = ("queued", "claimed", "running")

#: The states in which the next leg may be starting (see :func:`starting_task_leg_ids`).
STARTING_JOB_STATES: Final = ("queued", "claimed", "running")

_PLAIN_WORD = re.compile(r"[a-z_]+")


def _literal(value: str) -> ColumnElement[str]:
    """A constant rendered as an SQL literal, not a bind parameter (see the module docstring).

    Only fixed vocabulary words reach here (a job type, a job state), and the pattern keeps it
    that way: nothing a user sends can become SQL text through this.
    """
    if _PLAIN_WORD.fullmatch(value) is None:  # pragma: no cover - a programming error
        msg = f"not a plain vocabulary word: {value!r}"
        raise ValueError(msg)
    return literal_column(f"'{value}'", Text)


class _TaskIdOfPayload(ColumnElement[str]):
    """The task id inside a job's payload.

    On Postgres it is spelled ``(jobs.payload ->> 'task_id')``, the expression
    ``idx_jobs_live_task_leg`` indexes (SQLAlchemy's own ``payload["task_id"].as_string()``
    renders ``CAST((jobs.payload ->> %(param)s) AS VARCHAR)``, a bind parameter the index can
    never match under a generic plan). Elsewhere (community SQLite) the portable JSON path.
    """

    type = Text()
    inherit_cache = True


@compiles(_TaskIdOfPayload, "postgresql")
def _task_id_on_postgres(element: _TaskIdOfPayload, compiler: SQLCompiler, **kw: Any) -> str:  # noqa: ARG001, ANN401 - the compiler hook's signature
    return f"({compiler.process(jobs_t.c.payload, **kw)} ->> 'task_id')"


@compiles(_TaskIdOfPayload)
def _task_id_elsewhere(element: _TaskIdOfPayload, compiler: SQLCompiler, **kw: Any) -> str:  # noqa: ARG001, ANN401 - the compiler hook's signature
    return str(compiler.process(jobs_t.c.payload["task_id"].as_string(), **kw))


def _task_id(task_id: ColumnElement[str] | str) -> ColumnElement[str]:
    """A task id as one named parameter (so a query naming it twice binds it once)."""
    return bindparam("task_id", task_id) if isinstance(task_id, str) else task_id


def _task_legs(task_id: ColumnElement[str] | str) -> Select[tuple[str]]:
    """The ids of this task's leg jobs (any state). ``task_id`` may be a correlated column."""
    return select(jobs_t.c.id).where(
        jobs_t.c.type == _literal(TASK_LEG_JOB_TYPE),
        _TaskIdOfPayload() == _task_id(task_id),
    )


def live_task_leg_ids(task_id: ColumnElement[str] | str) -> Select[tuple[str]]:
    """The task's legs still going to run, running included (the revival sweep's question)."""
    return _task_legs(task_id).where(jobs_t.c.state.in_([_literal(s) for s in LIVE_JOB_STATES]))


def starting_task_leg_ids(task_id: str, *, now: datetime) -> Select[tuple[str]]:
    """The task's next leg if it is starting.

    Claimed; or queued and due by ``now``; or running with no run row opened for it yet. A
    leg's run row is opened after its job was scheduled, so "opened" is a run of this task
    that started at or after the job's ``scheduled_at``; an earlier leg's run started before.
    """
    run_opened = exists(
        select(runs_t.c.id).where(
            runs_t.c.task_id == _task_id(task_id),
            runs_t.c.started_at >= jobs_t.c.scheduled_at,
        )
    )
    return _task_legs(task_id).where(
        jobs_t.c.state.in_([_literal(s) for s in STARTING_JOB_STATES]),
        or_(
            jobs_t.c.state == _literal("claimed"),
            and_(jobs_t.c.state == _literal("queued"), jobs_t.c.scheduled_at <= now),
            and_(jobs_t.c.state == _literal("running"), ~run_opened),
        ),
    )


def has_starting_leg(engine: Engine, owner_id: str, task_id: str, *, now: datetime) -> bool:
    """Whether the task's next leg is starting, in the owner's scope (the task detail).

    The owner filter is explicit as well as enforced by RLS: task ids are unique, but a
    query that names its tenant does not depend on the connection's scope to be right, and
    it stays right on the community engine, which has no RLS.
    """
    legs = starting_task_leg_ids(task_id, now=now).where(jobs_t.c.owner_id == owner_id)
    with rls_connection(engine, owner_id) as conn:
        query = select(exists(legs))
        return bool(conn.execute(query).scalar_one())
