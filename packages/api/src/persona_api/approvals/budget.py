"""Per-task budget — cap over the A2 ledger, pause-at-cap + one-reply extend (A3, T10; A3-D-5).

A task must never silently overrun its cost cap. The **effective cap** is the contract's
``ContractBounds.total_budget_micros`` (or the platform default for an unconfigured task) **plus
the SUM of granted extensions** — the extensions ride ``audit_log`` rows (``budget.extended``),
no new table (the A3-D-X-migration / A1 audit-projection precedent). The check runs at the **leg
boundary** (the continuation's CONTINUE point), not per tool call, and the ``(target, action)``
index keeps the extension-SUM off a full ``audit_log`` scan.

The state machine over ``task.ledger.total_micros`` vs the effective cap:

- **approaching** (≥80%) → noted (``budget.approaching``), the leg continues;
- **reached** (≥100%) → the task is **paused** (A2 overlay — no new legs) + a ``budget.reached``
  account; nothing runs past the cap until the user extends.

**The extension is at-most-once.** "add another 50kr" → a single ``budget.extended`` row that
raises the cap + resumes the task. The CAS lives in :meth:`TaskStore.cas_unpause` (clear the
overlay iff set): only the un-pause winner writes the extension, so a duplicated extension reply
cannot double-extend.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.tasks import ScheduledFire
from sqlalchemy import text

from persona_api.services import audit_service
from persona_api.tasks.handler import enqueue_task_leg

if TYPE_CHECKING:
    from datetime import datetime

    from persona.tasks import Task
    from sqlalchemy import Engine

    from persona_api.jobs.queue import JobQueue
    from persona_api.tasks.continuation import TaskStateSignal
    from persona_api.tasks.store import TaskStore

__all__ = [
    "PLATFORM_DEFAULT_BUDGET_MICROS",
    "BudgetEnforcer",
    "BudgetState",
    "parse_extension_micros",
]

_log = get_logger("api.approvals.budget")

#: The platform default per-task cap for an unconfigured task (env-tunable at the worker:
#: ``PERSONA_TASK_BUDGET_DEFAULT_MICROS``). Conservative — an unconfigured task is still bounded.
PLATFORM_DEFAULT_BUDGET_MICROS = 10_000_000

#: The "approaching" threshold (fraction of the effective cap).
_APPROACHING_FRACTION = 0.8

_BUDGET_EXTENDED = "budget.extended"
_BUDGET_REACHED = "budget.reached"
_BUDGET_APPROACHING = "budget.approaching"

#: Norwegian kroner → micros (the ledger unit). 1 kr = 10_000 micros (the project's credit unit).
_MICROS_PER_KR = 10_000
_KR_AMOUNT = re.compile(r"(\d+(?:[.,]\d+)?)\s*(?:kr|kroner|nok)", re.IGNORECASE)


def parse_extension_micros(reply: str) -> int | None:
    """Parse a one-reply extension amount ("add another 50kr" / "legg til 50kr") → micros.

    Returns the micros to add, or ``None`` if no amount is recognisable (the caller then
    clarifies rather than guessing — the same default-to-not-act floor as reply parsing).
    """
    match = _KR_AMOUNT.search(reply)
    if match is None:
        return None
    kroner = float(match.group(1).replace(",", "."))
    return int(round(kroner * _MICROS_PER_KR))


class BudgetState(StrEnum):
    """Where the task sits against its effective cap."""

    OK = "ok"
    APPROACHING = "approaching"
    REACHED = "reached"


class BudgetEnforcer:
    """Enforces the effective cap at the leg boundary + applies the at-most-once extension."""

    def __init__(
        self,
        *,
        engine: Engine,
        tasks: TaskStore,
        queue: JobQueue,
        default_micros: int = PLATFORM_DEFAULT_BUDGET_MICROS,
        on_state_change: TaskStateSignal | None = None,
    ) -> None:
        self._engine = engine
        self._tasks = tasks
        self._queue = queue
        self._default = default_micros
        # Spec A11/A6 (W8): the task.updated live ping for the budget-paused transition. The A6
        # Tasks surface shows a budget-paused task's "extend?" affordance — so a pause at cap
        # refetches live. Best-effort, None → poll. (Fires once ``enforce`` has a live caller.)
        self._on_state_change = on_state_change

    def effective_cap(self, owner_id: str, task: Task) -> int:
        """The contract cap (or platform default) + the SUM of granted extensions (audit rows)."""
        base = task.contract.bounds.total_budget_micros
        base = base if base is not None else self._default
        return base + self._extensions_total(owner_id, task.id)

    def check(self, owner_id: str, task: Task) -> BudgetState:
        """Classify the task against its effective cap (a read; CQS)."""
        cap = self.effective_cap(owner_id, task)
        spent = task.ledger.total_micros
        if spent >= cap:
            return BudgetState.REACHED
        if cap > 0 and spent >= _APPROACHING_FRACTION * cap:
            return BudgetState.APPROACHING
        return BudgetState.OK

    def enforce(self, owner_id: str, task: Task, *, now: datetime) -> bool:
        """Leg-boundary gate: pause at the cap, note when approaching. Returns *halt?*.

        ``True`` → the task was paused at the cap (the caller must NOT enqueue the next leg —
        the task waits for an extension). ``False`` → continue (possibly after an approaching
        note). The C0 "budget reached; extend?" message is originated by the worker from the
        ``budget.reached`` audit account (the same Originator path as the sweep).
        """
        state = self.check(owner_id, task)
        if state is BudgetState.REACHED:
            return self._pause_at_cap(owner_id, task, now=now)
        if state is BudgetState.APPROACHING:
            self._audit(owner_id, _BUDGET_APPROACHING, task.id, self._usage_meta(owner_id, task))
        return False

    def extend(self, owner_id: str, task_id: str, amount_micros: int, *, now: datetime) -> bool:
        """Apply a one-reply extension: raise the cap + resume — **at most once per pause**.

        The CAS in :meth:`TaskStore.cas_unpause` gates it: only the un-pause winner records the
        ``budget.extended`` row and re-enqueues the next leg, so a duplicated extension reply is
        a clean no-op (no double-extend). Returns whether the extension applied.
        """
        if not self._tasks.cas_unpause(owner_id, task_id, now=now):
            _log.info("budget extend no-op (not budget-paused)", task_id=task_id)
            return False
        self._audit(owner_id, _BUDGET_EXTENDED, task_id, {"amount_micros": str(amount_micros)})
        task = self._tasks.get(owner_id, task_id)
        enqueue_task_leg(
            self._queue,
            owner_id=owner_id,
            task_id=task_id,
            predecessor_seq=task.head_checkpoint_seq,
            trigger=ScheduledFire(schedule_id=f"self:{task_id}", fire_time=now),
            scheduled_at=now,
        )
        _log.info("budget extended + resumed", task_id=task_id, amount_micros=amount_micros)
        return True

    # --- internals ----------------------------------------------------------

    def _pause_at_cap(self, owner_id: str, task: Task, *, now: datetime) -> bool:
        if task.paused:
            return True  # already paused (e.g. a re-checked leg) — still halt, no double-audit
        self._tasks.pause(owner_id, task.id, now=now)
        self._audit(owner_id, _BUDGET_REACHED, task.id, self._usage_meta(owner_id, task))
        _log.info("task paused at budget cap", task_id=task.id, spent=task.ledger.total_micros)
        if self._on_state_change is not None:  # A11 budget-paused ping (best-effort, post-write)
            try:
                self._on_state_change(owner_id, task.id, "budget_paused")
            except Exception:  # noqa: BLE001 — a live-ping failure must not fail the pause
                _log.warning("task.updated signal failed; degrading to poll", task_id=task.id)
        return True

    def _usage_meta(self, owner_id: str, task: Task) -> dict[str, str]:
        return {
            "cap_micros": str(self.effective_cap(owner_id, task)),
            "spent_micros": str(task.ledger.total_micros),
        }

    def _extensions_total(self, owner_id: str, task_id: str) -> int:
        """SUM the granted ``budget.extended`` amounts for a task (owner-scoped, indexed)."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                text(
                    "SELECT metadata FROM audit_log "
                    "WHERE target = :task AND action = :action AND user_id = :owner"
                ),
                {"task": task_id, "action": _BUDGET_EXTENDED, "owner": owner_id},
            ).all()
        total = 0
        for row in rows:
            try:
                total += int(row.metadata.get("amount_micros", 0))
            except (TypeError, ValueError):
                continue
        return total

    def _audit(self, owner_id: str, action: str, task_id: str, metadata: dict[str, str]) -> None:
        audit_service.record(
            engine=self._engine,
            user_id=owner_id,
            action=action,
            target=task_id,
            metadata=metadata,
        )
