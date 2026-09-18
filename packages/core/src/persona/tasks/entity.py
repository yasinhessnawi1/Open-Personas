"""The Task entity — the durable thing above runs (Spec A2, T2).

A task spans days through many bounded legs. This is its in-memory shape: identity, the
A4-authored :class:`Contract` (the anti-drift anchor a leg cannot write), the lifecycle
:class:`TaskState` (+ the ``paused`` overlay + the qualifying :class:`WaitKind`), the
cumulative :class:`CostLedger`, the monotonic checkpoint-sequence anchor
(``head_checkpoint_seq`` — the A2-R-4 CAS predecessor), and the linkage points A4/A6
consume (conversation pointer, run ids, workspace id, schedule id).

Frozen, like the A0 ``Job``: every transition is a pure functional update returning a new
``Task``, never an in-place mutation. The clock is injected (``now``) so core stays
clock-free. The durable RLS store (persona-api) persists these; the leg executor
(persona-runtime) drives the transitions.

See ``docs/specs/phase3/spec_A2/decisions.md`` (D-A2-X-core-api-split, D-A2-X-idempotency)
and the spec §2 lifecycle.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, field_validator, model_validator

from persona.errors import TaskStateError
from persona.tasks.contract import Contract  # noqa: TC001 — Pydantic needs runtime access
from persona.tasks.ledger import CostLedger, SpendKind
from persona.tasks.state import TaskKind, TaskState, WaitKind, is_terminal, validate_transition

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.tasks.contract import AcceptanceCriterion

__all__ = ["TASK_SCHEMA_VERSION", "Task"]

#: Task schema version (mirrors the persona/checkpoint schema-version discipline).
TASK_SCHEMA_VERSION = "1.0"


def _ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes; normalise tz-aware ones to UTC (house rule)."""
    if value.tzinfo is None:
        msg = "naive datetime not allowed; use datetime.now(UTC) or attach a tzinfo"
        raise ValueError(msg)
    return value.astimezone(UTC)


class Task(BaseModel):
    """A durable task — the entity A3/A4/A5/A6 attach to.

    Attributes:
        id: Durable task id.
        owner_id: The tenant the task runs as — the RLS scope.
        persona_id: The persona executing the task.
        contract: The A4-authored anchor (goal/scope/criteria/bounds). Immutable here.
        kind: ``STANDING`` (confirmed, usually scheduled) or ``AD_HOC`` (a one-off
            dispatch with a degenerate contract). Fixed at create (Spec W1, D-W1-2).
        state: Lifecycle state (defaults to ``DEFINED``).
        paused: User-imposed overlay — no new legs while ``True`` (orthogonal to ``state``).
        wait_kind: Why the task is waiting; set iff ``state == WAITING``.
        ledger: Cumulative spend (A2 accounts what A0 meters).
        head_checkpoint_seq: The latest committed checkpoint sequence (``None`` before the
            first checkpoint); the CAS predecessor a re-delivered leg keys on (A2-R-4).
            It has a second job (R9-173): it is the correlation key between a task and
            the job that died on it. ``persona_api.tasks.handler.task_leg_idempotency_key``
            keys every enqueued leg ``task:{id}:after:{head}``, and the revival sweep
            (``persona_api.tasks.revival_sweep.RevivalSweeper._dead_cause_at_head``) finds
            a dead leg by matching a dead job on that same key at the CURRENT head. So a
            park or gate path that appends a checkpoint must re-key or re-enqueue, or the
            dead-job match is lost and a transient failure is never picked up again.
        conversation_id: Originating/linked conversation (A4/A6 consume).
        run_ids: The leg run ids accumulated so far.
        workspace_id: The task-scoped Spec-12 workspace.
        schedule_id: The A1 schedule that fires legs (the contract's cadence aspect).
        created_at: Creation time (tz-aware UTC).
        updated_at: Last-transition time (tz-aware UTC; bumped by every transition).
        schema_version: The task schema version.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str
    owner_id: str
    persona_id: str
    contract: Contract
    kind: TaskKind = TaskKind.STANDING

    state: TaskState = TaskState.DEFINED
    paused: bool = False
    wait_kind: WaitKind | None = None
    ledger: CostLedger = CostLedger()
    head_checkpoint_seq: int | None = None

    conversation_id: str | None = None
    run_ids: tuple[str, ...] = ()
    workspace_id: str | None = None
    schedule_id: str | None = None

    created_at: datetime
    updated_at: datetime
    schema_version: str = TASK_SCHEMA_VERSION

    @field_validator("created_at", "updated_at", mode="after")
    @classmethod
    def _require_tz_aware(cls, value: datetime) -> datetime:
        return _ensure_utc(value)

    @model_validator(mode="after")
    def _wait_kind_iff_waiting(self) -> Task:
        waiting = self.state == TaskState.WAITING
        if waiting and self.wait_kind is None:
            msg = "a WAITING task must carry a wait_kind"
            raise ValueError(msg)
        if not waiting and self.wait_kind is not None:
            msg = "wait_kind is only valid in the WAITING state"
            raise ValueError(msg)
        return self

    # ----- lifecycle transitions (pure functional updates) -------------------

    @property
    def next_checkpoint_seq(self) -> int:
        """The sequence the next checkpoint must claim (0, then strictly +1)."""
        return 0 if self.head_checkpoint_seq is None else self.head_checkpoint_seq + 1

    def _transition(
        self,
        new_state: TaskState,
        *,
        now: datetime,
        wait_kind: WaitKind | None = None,
        **updates: object,
    ) -> Task:
        """Validate the edge and return a new ``Task`` in ``new_state``.

        ``wait_kind`` defaults to ``None`` so every transition *except* ``begin_wait``
        clears it — the set-iff-WAITING invariant falls out for free.
        """
        validate_transition(self.state, new_state)
        return self.model_copy(
            update={"state": new_state, "wait_kind": wait_kind, "updated_at": now, **updates}
        )

    def _require_state(self, expected: TaskState) -> None:
        """Guard an operation whose source state the edge table can't disambiguate.

        ``start`` and ``resume`` both target ``ACTIVE`` (``DEFINED→ACTIVE`` and
        ``WAITING→ACTIVE`` are both legal edges), so the edge check alone would let a
        never-started task ``resume`` or a waiting task ``start``. The source guard keeps
        each operation to its one valid origin.
        """
        if self.state != expected:
            raise TaskStateError(
                "operation requires a different state",
                context={"state": self.state.value, "required": expected.value},
            )

    def start(self, *, now: datetime) -> Task:
        """``DEFINED → ACTIVE`` — the first leg may run."""
        self._require_state(TaskState.DEFINED)
        return self._transition(TaskState.ACTIVE, now=now)

    def begin_wait(self, kind: WaitKind, *, now: datetime) -> Task:
        """``ACTIVE → WAITING(kind)`` — the leg ended posing/scheduling."""
        return self._transition(TaskState.WAITING, now=now, wait_kind=kind)

    def resume(self, *, now: datetime) -> Task:
        """``WAITING → ACTIVE`` — a trigger arrived; the next leg runs."""
        self._require_state(TaskState.WAITING)
        return self._transition(TaskState.ACTIVE, now=now)

    def complete(self, *, now: datetime) -> Task:
        """``ACTIVE → COMPLETED`` — the task is done (a completion report follows)."""
        return self._transition(TaskState.COMPLETED, now=now)

    def fail(self, *, now: datetime) -> Task:
        """``ACTIVE | WAITING → FAILED`` — an unrecoverable terminal."""
        return self._transition(TaskState.FAILED, now=now)

    def cancel(self, *, now: datetime) -> Task:
        """``→ CANCELLED`` from any non-terminal state; clears the ``paused`` overlay."""
        return self._transition(TaskState.CANCELLED, now=now, paused=False)

    # ----- the paused overlay (orthogonal to the lifecycle state) ------------

    def pause(self, *, now: datetime) -> Task:
        """Set the ``paused`` overlay — no new legs. Valid on a non-terminal, unpaused task."""
        if is_terminal(self.state):
            raise TaskStateError(
                "cannot pause a terminal task", context={"state": self.state.value}
            )
        if self.paused:
            raise TaskStateError("task is already paused", context={"task_id": self.id})
        return self.model_copy(update={"paused": True, "updated_at": now})

    def unpause(self, *, now: datetime) -> Task:
        """Clear the ``paused`` overlay. Valid only on a paused task."""
        if not self.paused:
            raise TaskStateError("task is not paused", context={"task_id": self.id})
        return self.model_copy(update={"paused": False, "updated_at": now})

    # ----- ledger + checkpoint-sequence anchor -------------------------------

    def record_spend(self, kind: SpendKind, amount_micros: int, *, now: datetime) -> Task:
        """Add a metered leg spend to the ledger. Rejected once terminal."""
        if is_terminal(self.state):
            raise TaskStateError(
                "cannot record spend on a terminal task", context={"state": self.state.value}
            )
        return self.model_copy(
            update={"ledger": self.ledger.record(kind, amount_micros), "updated_at": now}
        )

    def advance_checkpoint(self, seq: int, *, now: datetime) -> Task:
        """Advance the head checkpoint sequence to ``seq`` (must be the strict successor).

        The pure half of the A2-R-4 idempotency story: a checkpoint claims exactly
        ``next_checkpoint_seq``. A re-delivered leg recomputing the same predecessor would
        target an already-claimed seq — rejected here; the durable store detects the
        existing checkpoint and no-ops (the CAS on ``head_seq``).

        Raises:
            TaskStateError: If ``seq`` is not the successor, or the task is terminal.
        """
        if is_terminal(self.state):
            raise TaskStateError(
                "cannot advance the checkpoint of a terminal task",
                context={"state": self.state.value},
            )
        expected = self.next_checkpoint_seq
        if seq != expected:
            raise TaskStateError(
                "checkpoint sequence must be the strict successor",
                context={"expected": str(expected), "got": str(seq), "task_id": self.id},
            )
        return self.model_copy(update={"head_checkpoint_seq": seq, "updated_at": now})

    def settle_criteria(self, criteria: Sequence[AcceptanceCriterion], *, now: datetime) -> Task:
        """Advance acceptance-criterion STATUSES — the controlled path the contract names.

        The contract is the anti-drift anchor and carries no mutation method: a leg
        structurally cannot rewrite the goal, the scope, or a criterion's wording. Status is
        the one part that moves as work progresses, and it moves through here, where the ids
        and the statements are checked against the ones already on the contract. So the only
        thing this call can ever change is a status, however the caller assembled the tuple.

        Args:
            criteria: The full criteria tuple, in contract order, with statuses applied.
            now: The write time.

        Returns:
            A new ``Task``, or ``self`` unchanged when no status actually moved.

        Raises:
            TaskStateError: If the task is terminal, or the ids/statements do not match the
                contract exactly (a rewrite wearing a status update's clothes).
        """
        if is_terminal(self.state):
            raise TaskStateError(
                "cannot settle criteria on a terminal task", context={"state": self.state.value}
            )
        existing = self.contract.acceptance_criteria
        if [(c.id, c.statement) for c in existing] != [(c.id, c.statement) for c in criteria]:
            raise TaskStateError(
                "acceptance criteria may change status only, never id or statement",
                context={"task_id": self.id},
            )
        if tuple(criteria) == existing:
            return self
        contract = self.contract.model_copy(update={"acceptance_criteria": tuple(criteria)})
        return self.model_copy(update={"contract": contract, "updated_at": now})
