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

**The extension is at-most-once, per time the task reaches its cap** (R9-158). "add another $2"
→ a single ``budget.extended`` row that raises the cap, then the task carries on. The gate is
the cap itself, checked and written under a row lock on the task (:meth:`BudgetEnforcer.extend`):
an extension is recorded only while the task is AT its cap, and one that would leave it still
at its cap is refused with the shortfall, so every recorded extension lifts the task off its
cap. A duplicated reply that waited on the lock then finds it no longer at the cap and no-ops.
It no longer depends on ``paused``: a task can reach its cap while waiting on the user, and
the old gate (the un-pause CAS) refused to extend it at all, a dead end the Resume refusal then
pointed the user straight into.
"""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.tasks import Revived, TaskState, is_terminal, micros_from_dollars
from sqlalchemy import insert, select

from persona_api.db.engine import rls_connection
from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import tasks as tasks_t
from persona_api.services import audit_service

if TYPE_CHECKING:
    from collections.abc import Iterator
    from datetime import datetime

    from persona.tasks import Task
    from sqlalchemy import Connection, Engine

    from persona_api.jobs.queue import JobQueue
    from persona_api.tasks.continuation import TaskStateSignal
    from persona_api.tasks.store import TaskStore

__all__ = [
    "PLATFORM_DEFAULT_BUDGET_MICROS",
    "BudgetEnforcer",
    "BudgetState",
    "Extension",
    "ExtensionOutcome",
    "parse_extension_micros",
]

_log = get_logger("api.approvals.budget")

#: The platform default per-task cap for an unconfigured task (env-tunable at the worker:
#: ``PERSONA_TASK_BUDGET_DEFAULT_MICROS``). An unconfigured task is still bounded.
#:
#: REVALUED 2026-09-14. This was 10_000_000 and described as conservative, which was true while
#: the ledger counted TOKENS. R9-161 made the ledger hold real money without changing this number,
#: and 10 000 micros is one dollar, so the default silently became a 1000 dollar cap per task, with
#: ``_MAX_EXTEND_MICROS`` tracking it so one extension could add another 1000. Nothing flagged it
#: because the constant itself never changed, which is exactly how a unit change does damage.
#:
#: 100_000 micros is 10 dollars. A measured leg on this product costs a few cents, so ten dollars
#: is roughly a hundred and fifty legs: generous for an unconfigured task and survivable if one
#: runs away. The NUMBER is a product decision and is registered for the owner; what is not
#: negotiable is that it be stated in the unit it is actually enforced in.
PLATFORM_DEFAULT_BUDGET_MICROS = 100_000

#: The "approaching" threshold (fraction of the effective cap).
_APPROACHING_FRACTION = 0.8

_BUDGET_EXTENDED = "budget.extended"
_BUDGET_REACHED = "budget.reached"
_BUDGET_APPROACHING = "budget.approaching"

#: What a user may write when they name an amount, in a chat reply.
#:
#: Ruled 2026-09-15 (the currency unification): this product has exactly one currency and it
#: is USD. The audit of 2026-09-14 found no currency column and no rate column on any money
#: table in the schema, so a stored charge does not record what the customer was billed or in
#: what currency, and displaying it as anything but what it is would silently restate the
#: value of every past transaction at today's rate. There is therefore nothing to convert
#: FROM: this parser reads a dollar figure.
#:
#: Before this, the pattern accepted ``kr`` / ``kroner`` / ``nok`` and multiplied by 10 000,
#: which is micros per DOLLAR. A user asking for a 100 kr cap was granted a 100 USD cap,
#: about ten times what they authorised, and the error was invisible because the constant was
#: named ``_MICROS_PER_KR`` while holding the dollar scale (R9-172).
#:
#: The kroner spellings stay in the pattern as INPUT only, and are treated as dollars with no
#: conversion, because a Norwegian user typing "legg til 50kr" should not have their reply
#: silently unrecognised and be asked again. Every surface that states the cap back to them
#: says dollars, so the mismatch is visible in the confirmation rather than hidden in a bound.
_AMOUNT = re.compile(
    r"(?:\$\s*(\d+(?:[.,]\d+)?)|(\d+(?:[.,]\d+)?)\s*(?:dollars?|usd|kr|kroner|nok))",
    re.IGNORECASE,
)


def parse_extension_micros(reply: str) -> int | None:
    """Parse a one-reply extension amount ("add another $50" / "50 dollars") → micros.

    Returns the micros to add, or ``None`` if no amount is recognisable (the caller then
    clarifies rather than guessing — the same default-to-not-act floor as reply parsing).
    """
    match = _AMOUNT.search(reply)
    if match is None:
        return None
    amount = match.group(1) or match.group(2)
    return micros_from_dollars(amount.replace(",", "."))


class BudgetState(StrEnum):
    """Where the task sits against its effective cap."""

    OK = "ok"
    APPROACHING = "approaching"
    REACHED = "reached"


class ExtensionOutcome(StrEnum):
    """What a budget extension did (R9-158)."""

    #: Recorded: the cap rose, and the task carries on (see :meth:`BudgetEnforcer.extend`).
    APPLIED = "applied"
    #: The task is not at its cap (never was, or a duplicate found it already extended).
    NOT_AT_CAP = "not_at_cap"
    #: The amount would leave the task at its cap; nothing recorded. Add more than the gap.
    TOO_SMALL = "too_small"
    #: The task has ended; there is nothing for a higher cap to let carry on.
    FINISHED = "finished"
    #: More than one request may add: the per-request ceiling plus the shortfall.
    OVER_CEILING = "over_ceiling"


@dataclass(frozen=True)
class Extension:
    """The result of :meth:`BudgetEnforcer.extend`, from the one read that decided it."""

    outcome: ExtensionOutcome
    #: How far the task is over its cap (spent minus cap), never negative.
    shortfall_micros: int = 0
    #: The most this request could add (the ceiling plus the shortfall), when there is one.
    allowed_micros: int | None = None


#: What a budget-extended leg is told (Spec W1, D-W1-38 amended): the cap was raised, so the
#: work it was cut off from may continue. Not a schedule firing.
_EXTENDED_REASON = "the user extended its budget"


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

    def extend(
        self,
        owner_id: str,
        task_id: str,
        amount_micros: int,
        *,
        now: datetime,
        ceiling_micros: int | None = None,
    ) -> Extension:
        """Raise the cap of a task that is at it, then let it carry on (R9-158).

        **The gate.** In one transaction holding the task row's lock (``FOR UPDATE``): the
        task must be live and at its cap, and ``amount_micros`` must lift it off the cap;
        only then is the ``budget.extended`` row written. Every recorded extension therefore
        leaves the task below its cap, and a concurrent or duplicated extension, which waits
        on the lock and re-reads the committed cap, finds it no longer at the cap and returns
        :attr:`ExtensionOutcome.NOT_AT_CAP`. At most one extension per time the task reaches
        its cap, whether or not it is paused. The lock is :meth:`_row_lock`: ``FOR UPDATE`` on
        Postgres, and on SQLite (where ``FOR UPDATE`` is not rendered) ``BEGIN IMMEDIATE``,
        which takes the database's write lock before the read, so a second extender waits for
        the first to commit and then reads its extension.

        **The ceiling.** ``ceiling_micros`` bounds what one request may add, plus the shortfall:
        a task that ran past its cap by more than the ceiling could otherwise never be lifted
        off it (:attr:`ExtensionOutcome.OVER_CEILING` beyond that).

        **Carrying on**, after the commit (:meth:`_carry_on`): a paused ACTIVE or DEFINED task
        is unpaused and its next leg queued, as before. A WAITING task only has a pause cleared
        and nothing queued: it waits on a reply, a pickup or its own trigger, and a leg queued
        now would run past the question it is waiting on. An unpaused task needs nothing.
        """
        with self._row_lock(owner_id, task_id) as conn:
            if conn is None:
                return Extension(ExtensionOutcome.NOT_AT_CAP)
            task = self._tasks.get(owner_id, task_id)
            if is_terminal(task.state):
                return Extension(ExtensionOutcome.FINISHED)
            cap = self.effective_cap(owner_id, task)
            spent = task.ledger.total_micros
            if spent < cap:
                _log.info("budget extend no-op (not at the cap)", task_id=task_id)
                return Extension(ExtensionOutcome.NOT_AT_CAP)
            shortfall = spent - cap
            if ceiling_micros is not None and amount_micros > ceiling_micros + shortfall:
                return Extension(
                    ExtensionOutcome.OVER_CEILING, shortfall, ceiling_micros + shortfall
                )
            if amount_micros <= shortfall:
                return Extension(ExtensionOutcome.TOO_SMALL, shortfall)
            conn.execute(
                insert(audit_log_t).values(
                    id=f"audit_{uuid.uuid4().hex}",
                    user_id=owner_id,
                    action=_BUDGET_EXTENDED,
                    target=task_id,
                    metadata={"amount_micros": str(amount_micros), "cap_micros": str(cap)},
                )
            )
        self._carry_on(owner_id, task, now=now)
        _log.info("budget extended", task_id=task_id, amount_micros=amount_micros)
        return Extension(ExtensionOutcome.APPLIED, shortfall)

    @contextmanager
    def _row_lock(self, owner_id: str, task_id: str) -> Iterator[Connection | None]:
        """A transaction holding the task row's lock, or ``None`` if the owner has no such task.

        Every budget decision that writes (the extension, the pause at the cap) runs inside
        it, so each reads what the other committed. Postgres: ``SELECT ... FOR UPDATE`` on the
        row, the owner named explicitly as well as enforced by RLS. SQLite renders no
        ``FOR UPDATE`` and its driver begins a transaction only at the first write, so the
        read would be unlocked: ``BEGIN IMMEDIATE`` takes the write lock first. Writes other
        than on the yielded connection must wait until the block ends (on SQLite they would
        wait on this very lock).
        """
        with rls_connection(self._engine, owner_id) as conn:
            if conn.dialect.name == "sqlite":
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            locked = conn.execute(
                select(tasks_t.c.id)
                .where(tasks_t.c.id == task_id, tasks_t.c.owner_id == owner_id)
                .with_for_update()
            ).first()
            yield conn if locked is not None else None

    def _carry_on(self, owner_id: str, task: Task, *, now: datetime) -> None:
        """After a recorded extension: clear a pause, and queue a leg only where one is due."""
        if not task.paused or not self._tasks.cas_unpause(owner_id, task.id, now=now):
            return
        if task.state is TaskState.WAITING:
            return  # its reply, a pickup or its own trigger carries it on
        if task.state is TaskState.DEFINED:
            self._tasks.start(owner_id, task.id, now=now)
        # Through the one resume seam (D-W1-30), not a bare enqueue: a paused task's leg may
        # already have been claimed and consumed at the head (a schedule firing while it sat
        # at the cap), and a bare enqueue at that key would be absorbed by the spent row,
        # leaving the task unpaused with nothing to run (R9-158 re-review). The seam counts
        # the spent attempts and suffixes the key (D-W1-29).
        from persona_api.tasks.continuation import TaskContinuation

        TaskContinuation(task_store=self._tasks, queue=self._queue).resume(
            owner_id,
            task.id,
            # Spec W1 (D-W1-38, amended): the leg is told the budget was raised, not that a
            # schedule fired. It matters: told a fire, a persona may read the wake as its
            # recurrence rather than as permission to carry on the work it was cut off from.
            Revived(reason=_EXTENDED_REASON, revived_at=now),
            now=now,
        )

    # --- internals ----------------------------------------------------------

    def _pause_at_cap(self, owner_id: str, task: Task, *, now: datetime) -> bool:
        """Pause at the cap, deciding again under the row lock (R9-158 re-review).

        ``task`` is the caller's snapshot, read before the lock. A concurrent extension may
        have lifted the cap since; pausing on the stale read would leave the task paused
        BELOW its cap, waiting for an extension that already happened. So the cap is re-read
        under the same lock the extension takes, and a task no longer at its cap carries on.
        """
        with self._row_lock(owner_id, task.id) as conn:
            if conn is None:
                return True  # gone: nothing to continue
            current = self._tasks.get(owner_id, task.id)
            if is_terminal(current.state):
                return True
            if self.check(owner_id, current) is not BudgetState.REACHED:
                _log.info("cap lifted before the pause; the leg carries on", task_id=task.id)
                return False
            if current.paused:
                return True  # already paused (e.g. a re-checked leg): still halt, no re-audit
            if not self._tasks.cas_pause(conn, owner_id, current, now=now):
                return True
        self._audit(owner_id, _BUDGET_REACHED, task.id, self._usage_meta(owner_id, current))
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
        """SUM the granted ``budget.extended`` amounts for a task (owner-scoped, indexed).

        Read through the typed column rather than raw SQL: on the community (SQLite) engine a
        raw ``SELECT metadata`` returns the JSON as text, and the ``.get`` below then raised on
        every effective-cap read once a task had an extension (found in R9-158).
        """
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(audit_log_t.c.metadata).where(
                    audit_log_t.c.target == task_id,
                    audit_log_t.c.action == _BUDGET_EXTENDED,
                    audit_log_t.c.user_id == owner_id,
                )
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
