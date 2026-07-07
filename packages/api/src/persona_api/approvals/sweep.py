"""The approval expiry + reminder sweep — no zombie waits (Spec A3, T9; A3-D-2).

A pending approval must not rot. The sweep runs periodically (leader-gated, the N2 catalog-sync
pattern) and, against the platform thresholds:

- **remind once at ~24h** — for a pending proposal in the ``[remind_after, expire_after)``
  window, CAS the ``reminded_at`` marker NULL→now (so a double-sweep reminds exactly once),
  and surface it for a single C0 reminder;
- **auto-pause at ~72h** — for a pending proposal past ``expire_after``, CAS ``pending →
  expired`` (**terminal** — an expired proposal can never be approved; the resolver's
  pending-only pre-check + the approve CAS both reject it), then **pause the task** (the A2
  ``paused`` overlay) so it stops waiting silently, and surface it for a single honest C0
  account.

Both passes are **at-most-once** (the CAS in :meth:`ApprovalStore.mark_reminded` /
:meth:`ApprovalStore.transition_proposal`), so a double-sweep / a multi-machine future never
double-reminds or double-expires. The cross-tenant scan is a **privileged** read (the worker's
dispatch engine, like A0's dead-letter sweep); each per-proposal action is owner-scoped through
the RLS store. The sweep returns the claimed proposals; the worker originates the C0
reminder/expiry messages from the result (so the honest note rides the same Originator path).
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Protocol

from persona.approvals import ProposalStatus
from persona.logging import get_logger
from persona.tasks import is_terminal
from sqlalchemy import text

if TYPE_CHECKING:
    from datetime import datetime

    from persona.approvals import ActionProposal
    from sqlalchemy import Engine

    from persona_api.approvals.store import ApprovalStore
    from persona_api.schedules.leadership import SchedulerLeader
    from persona_api.tasks.store import TaskStore

__all__ = [
    "APPROVAL_SWEEP_LOCK_KEY",
    "EXPIRE_AFTER_DEFAULT",
    "REMIND_AFTER_DEFAULT",
    "ApprovalSweepRunner",
    "ApprovalSweepVoicer",
    "ApprovalSweeper",
    "SweepResult",
]

_log = get_logger("api.approvals.sweep")

#: Platform defaults (A3-D-2); the worker composition injects the env-configured values
#: (``PERSONA_APPROVAL_REMIND_AFTER_HOURS`` / ``PERSONA_APPROVAL_EXPIRE_AFTER_HOURS``).
REMIND_AFTER_DEFAULT = timedelta(hours=24)
EXPIRE_AFTER_DEFAULT = timedelta(hours=72)

#: A stable 64-bit key for the approval-sweep leader lock — independent of the scheduler-tick
#: lock, so "one sweeper" holds across a multi-machine future without contending the tick.
APPROVAL_SWEEP_LOCK_KEY: int = zlib.crc32(b"persona:approvals:sweep:leader")


@dataclass(frozen=True)
class SweepResult:
    """The proposals this sweep claimed — the worker originates one C0 message per entry."""

    reminded: tuple[ActionProposal, ...] = ()
    expired: tuple[ActionProposal, ...] = ()


class ApprovalSweeper:
    """Reminds-once + auto-pauses-on-expiry, idempotently (A3-D-2, T9).

    Args:
        dispatch_engine: A privileged (non-RLS) engine for the cross-tenant scan — the
            worker's dispatch engine, as A0's dead-letter sweep uses.
        approvals: The RLS-scoped store (the per-proposal CAS runs owner-scoped).
        tasks: The RLS-scoped task store (the auto-pause runs owner-scoped).
        remind_after: How long pending before a single reminder (default 24h).
        expire_after: How long pending before auto-pause (default 72h).
    """

    def __init__(
        self,
        *,
        dispatch_engine: Engine,
        approvals: ApprovalStore,
        tasks: TaskStore,
        remind_after: timedelta = REMIND_AFTER_DEFAULT,
        expire_after: timedelta = EXPIRE_AFTER_DEFAULT,
    ) -> None:
        self._engine = dispatch_engine
        self._approvals = approvals
        self._tasks = tasks
        self._remind_after = remind_after
        self._expire_after = expire_after

    def maybe_sweep(self, leader: SchedulerLeader, *, now: datetime) -> SweepResult | None:
        """Leader-gated entry: sweep only if this worker holds the sweep lock (else ``None``)."""
        if not leader.try_become_leader():
            return None
        return self.sweep(now=now)

    def sweep(self, *, now: datetime) -> SweepResult:
        """Run the reminder pass then the expiry pass; idempotent under a double-sweep."""
        reminded: list[ActionProposal] = []
        for owner_id, proposal_id in self._scan_due_for_reminder(now):
            claimed = self._approvals.mark_reminded(owner_id, proposal_id, now=now)
            if claimed is not None:
                reminded.append(claimed)

        expired: list[ActionProposal] = []
        for owner_id, proposal_id in self._scan_due_for_expiry(now):
            ended = self._approvals.transition_proposal(
                owner_id,
                proposal_id,
                expected=ProposalStatus.PENDING,
                new=ProposalStatus.EXPIRED,
                now=now,
            )
            if ended is not None:
                self._auto_pause(owner_id, ended.task_id, now)
                expired.append(ended)

        if reminded or expired:
            _log.info("approval sweep", reminded=len(reminded), expired=len(expired))
        return SweepResult(reminded=tuple(reminded), expired=tuple(expired))

    # --- internals ----------------------------------------------------------

    def _scan_due_for_reminder(self, now: datetime) -> list[tuple[str, str]]:
        """Pending + un-reminded proposals in the ``[remind_after, expire_after)`` window."""
        remind_before = now - self._remind_after
        expire_before = now - self._expire_after
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, owner_id FROM approval_proposals "
                    "WHERE status = 'pending' AND reminded_at IS NULL "
                    "AND created_at <= :remind_before AND created_at > :expire_before"
                ),
                {"remind_before": remind_before, "expire_before": expire_before},
            ).all()
        return [(r.owner_id, r.id) for r in rows]

    def _scan_due_for_expiry(self, now: datetime) -> list[tuple[str, str]]:
        """Pending proposals older than ``expire_after`` (reminded or not)."""
        expire_before = now - self._expire_after
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT id, owner_id FROM approval_proposals "
                    "WHERE status = 'pending' AND created_at <= :expire_before"
                ),
                {"expire_before": expire_before},
            ).all()
        return [(r.owner_id, r.id) for r in rows]

    def _auto_pause(self, owner_id: str, task_id: str, now: datetime) -> None:
        """Pause the task (A2 overlay) so an expired wait stops silently. Idempotent-guarded."""
        task = self._tasks.get(owner_id, task_id)
        if not task.paused and not is_terminal(task.state):
            self._tasks.pause(owner_id, task_id, now=now)


class ApprovalSweepVoicer(Protocol):
    """The narrow C0 voice the runner drives — a persona-voiced reminder / expiry note.

    Satisfied by :class:`~persona_api.services.origination_adapters.OriginatorApprovalNotifier`
    (each method takes only the proposal — it resolves the persona tag + conversation itself).
    Structural so ``sweep.py`` keeps ZERO import of the heavy origination composition.
    """

    async def remind(self, proposal: ActionProposal) -> None: ...
    async def expired(self, proposal: ActionProposal) -> None: ...


class ApprovalSweepRunner:
    """The leader-gated worker periodic that runs the sweep + voices its claimed proposals.

    The worker loop calls :meth:`run_once` on the approval-sweep cadence. It is leader-gated
    (the :class:`ApprovalSweeper.maybe_sweep` gate on this runner's own advisory lock — at most
    one process actually sweeps), and every voiced message is **best-effort**: a C0 origination
    failure is logged and never crashes the sweep, and the state changes (remind-once CAS,
    terminal expiry + auto-pause) have ALREADY happened inside ``maybe_sweep`` before any voice.

    ``notifier`` is ``None`` on a community / keyless boot (no memory backend / no C0 transport):
    the STATE changes still apply — approvals still expire + auto-pause — but the voiced message
    degrades to the persist-only floor (no-op here). This is the memory-backend gate.
    """

    def __init__(
        self,
        *,
        sweeper: ApprovalSweeper,
        leader: SchedulerLeader,
        notifier: ApprovalSweepVoicer | None = None,
    ) -> None:
        self._sweeper = sweeper
        self._leader = leader
        self._notifier = notifier

    async def run_once(self, *, now: datetime) -> SweepResult | None:
        """Sweep (if leader) then voice each reminded / expired proposal (best-effort)."""
        result = self._sweeper.maybe_sweep(self._leader, now=now)
        if result is None:
            return None  # not the leader this tick — a follower does no work
        if self._notifier is None:
            return result  # persist-only floor: state changed, no C0 voice (no backend)
        for proposal in result.reminded:
            await self._voice(self._notifier.remind, proposal, kind="remind")
        for proposal in result.expired:
            await self._voice(self._notifier.expired, proposal, kind="expired")
        return result

    @staticmethod
    async def _voice(deliver: object, proposal: ActionProposal, *, kind: str) -> None:
        """Originate one approval C0 message; a voicing failure never fails the sweep.

        Runs bound to the proposal's owner scope: the sweep loop has no ambient tenant context
        (unlike a per-job handler), so the notifier's RLS reads (persona tag, task conversation)
        + its recorder writes are scoped by setting the ``current_user_id`` contextvar here — the
        worker's RLS engine checkout listener reads it. Reset in ``finally`` so no scope leaks.
        """
        from persona_api.middleware.rls_context import current_user_id

        token = current_user_id.set(proposal.owner_id)
        try:
            await deliver(proposal)  # type: ignore[operator]
        except Exception:  # noqa: BLE001 — a voice failure must not crash the sweep
            _log.warning(
                "approval sweep voicing failed",
                kind=kind,
                proposal_id=proposal.proposal_id,
            )
        finally:
            current_user_id.reset(token)
