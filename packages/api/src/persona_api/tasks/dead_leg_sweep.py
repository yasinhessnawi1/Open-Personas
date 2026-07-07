"""The dead-leg sweep — a retry-exhausted task must never orphan silently (Spec A3, T13).

A0 dead-letters a ``task_leg`` job once its retries are exhausted (``jobs.state='dead'``). Left
alone the task stays ``active`` forever with no leg running — a silent orphan. This leader-gated
worker periodic closes that gap: per tick it reads A0's cross-tenant dead-letter queue and, per
dead ``task_leg``, drives :meth:`TaskContinuation.react_to_dead_leg` (owner-scoped) so the task
transitions ``active → waiting(on_user)`` with an honest :class:`~persona.tasks.StuckReport`, then
**voices** that report as a persona-voiced C0 :class:`~persona_api.approvals.FailureAccount` on the
task's conversation (the un-suppressible failure floor — criterion 7).

Leader-gated on its OWN advisory lock (``DEAD_LEG_SWEEP_LOCK_KEY`` — distinct from the scheduler
tick + the approval sweep, so "one dead-leg sweeper" holds without contending them). Every voice
is best-effort (a C0 failure is logged, never crashes the sweep). Idempotent: ``react_to_dead_leg``
returns ``None`` for a task that is not ``active`` (already parked / terminal), so a dead job the
sweep re-reads next tick re-voices nothing. Memory-backend-gated: with no backend (community /
keyless) the STATE change still applies — the task still parks ``waiting(on_user)`` — only the
voiced C0 note degrades to the persist-only floor (no-op here).
"""

from __future__ import annotations

import zlib
from typing import TYPE_CHECKING

from persona.logging import get_logger

from persona_api.approvals.failure import FailureKind, account_for_stuck
from persona_api.services.origination_adapters import resolve_persona_tag
from persona_api.tasks.handler import TASK_LEG_JOB_TYPE

if TYPE_CHECKING:
    from datetime import datetime

    from persona.tasks import StuckReport
    from sqlalchemy import Engine

    from persona_api.jobs.queue import JobQueue
    from persona_api.schedules.leadership import SchedulerLeader
    from persona_api.services.origination_adapters import OriginatorFailureNotifier
    from persona_api.tasks.continuation import TaskContinuation
    from persona_api.tasks.store import TaskStore

__all__ = ["DEAD_LEG_SWEEP_LOCK_KEY", "DeadLegSweeper"]

_log = get_logger("api.tasks.dead_leg_sweep")

#: A stable 64-bit key for the dead-leg-sweep leader lock — distinct from the scheduler-tick and
#: the approval-sweep keys, so "one dead-leg sweeper" holds across a multi-machine future without
#: contending either of the others.
DEAD_LEG_SWEEP_LOCK_KEY: int = zlib.crc32(b"persona:deadleg:sweep:leader")


class DeadLegSweeper:
    """Reads A0's dead-letters, parks each dead-legged task on the user, and voices the report.

    Args:
        continuation: The A2 continuation owning ``react_to_dead_leg`` (owner-scoped transition).
        dead_letter_queue: A JobQueue on the worker's cross-tenant **dispatch** engine — the
            privileged read of ``dead_letters()`` (like A0's own maintenance sweep).
        leader: The leadership gate on ``DEAD_LEG_SWEEP_LOCK_KEY`` (own dispatch-engine session).
        rls_engine: The ``persona_app`` engine — used to resolve the persona tag for the voice.
        task_store: The RLS-scoped task store — resolves the task's ``conversation_id`` to voice on.
        notifier: The C0 failure notifier. ``None`` on a backend-less boot ⇒ the persist-only floor
            (the task still parks; the voiced C0 account is a no-op).
        limit: Max dead-letters scanned per tick (bounded work).
    """

    def __init__(
        self,
        *,
        continuation: TaskContinuation,
        dead_letter_queue: JobQueue,
        leader: SchedulerLeader,
        rls_engine: Engine,
        task_store: TaskStore,
        notifier: OriginatorFailureNotifier | None = None,
        limit: int = 50,
    ) -> None:
        self._continuation = continuation
        self._queue = dead_letter_queue
        self._leader = leader
        self._engine = rls_engine
        self._tasks = task_store
        self._notifier = notifier
        self._limit = limit

    async def run_once(self, *, now: datetime) -> int:
        """Leader-gated: park + voice each dead ``task_leg``. Returns the count parked this tick."""
        if not self._leader.try_become_leader():
            return 0  # a follower does no work — at most one sweeper runs
        reacted = 0
        for job in self._queue.dead_letters(limit=self._limit):
            if job.type != TASK_LEG_JOB_TYPE:
                continue
            task_id = str(job.payload.get("task_id", ""))
            if not task_id:
                continue
            cause = job.last_error or "leg failed after retries"
            report = self._continuation.react_to_dead_leg(job.owner_id, task_id, cause, now=now)
            if report is None:
                continue  # not active (already parked / terminal) — idempotent no-op
            reacted += 1
            await self._voice(job.owner_id, task_id, report)
        if reacted:
            _log.info("dead-leg sweep parked tasks", reacted=reacted)
        return reacted

    async def _voice(self, owner_id: str, task_id: str, report: StuckReport) -> None:
        """Originate the stuck account as a C0 message (best-effort; persist-only floor else).

        Runs bound to the task owner's scope: the sweep loop carries no ambient tenant context
        (unlike a per-job handler), so the RLS reads (persona tag, task conversation) + the
        notifier's recorder writes are scoped by setting the ``current_user_id`` contextvar here —
        the worker's RLS engine checkout listener reads it. Reset in ``finally`` (no scope leak).
        """
        if self._notifier is None:
            return  # no backend — the state change stands; the voice degrades to the floor
        from persona_api.middleware.rls_context import current_user_id

        token = current_user_id.set(owner_id)
        try:
            task = self._tasks.get(owner_id, task_id)
            if task.conversation_id is None:
                return  # nowhere to voice — the Tasks surface still shows waiting(on_user)
            persona = resolve_persona_tag(self._engine, task.persona_id)
            if persona is None:
                return  # deleted persona → nothing to voice
            account = account_for_stuck(report, kind=FailureKind.LEG_DEAD_LETTER)
            await self._notifier.notify(
                account,
                persona=persona,
                owner_id=owner_id,
                conversation_id=task.conversation_id,
            )
        except Exception:  # noqa: BLE001 — a voice failure must not crash the sweep
            _log.warning("dead-leg sweep voicing failed", task_id=task_id)
        finally:
            current_user_id.reset(token)
