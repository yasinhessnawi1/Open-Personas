"""Apply a conversational steering verb to a live task (Spec A4, T9b; criterion 8).

The api side of the steering seam: the chat-turn worker hands this the ``task_steering`` event the
runtime emitted (the runtime resolved the verb + task and, for a cancel, already took the
consequence-aware confirmation). This applies it through the owner-scoped ``TaskStore`` — the
worker injects ``owner_id`` from its handle, so every mutation is RLS-scoped to the caller.

**Cancel-failure-visibility (A4-D-X):** the persona already told the user "cancelled" in chat, so a
cancel that fails to take would leave the task running silently — a false confirmation. On a cancel
that raises, this re-reads the task's real state: still non-terminal → an **un-suppressible**
``MessagePriority.FAILURE`` account is surfaced (the cadence-bypass floor); already-terminal or gone
→ a benign no-op (the cancel's intent was already satisfied). Pause/resume are reversible and apply
immediately with no such escalation.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Protocol

from persona.errors import TaskNotFoundError
from persona.logging import get_logger
from persona.tasks import is_terminal
from persona_runtime.task_origination import SteeringOutcome, SteeringVerb

from persona_api.approvals.failure import account_for_cancel_failure

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.schema.origination import PersonaIdentityTag
    from persona.tasks import Task
    from sqlalchemy import Engine

    from persona_api.services.origination_service import FailureNotifier
    from persona_api.services.task_control_service import ControlOutcome

__all__ = ["ControlSteeringPort", "TaskMutator", "TaskSteeringService"]

_logger = get_logger("api.task_steering")


class TaskMutator(Protocol):
    """The owner-scoped task ops steering needs (``TaskControlMutator`` is the real one).

    Pause and resume answer the shared controls' :class:`ControlOutcome`, whose note is
    what the user is told when nothing changed (R9-241).
    """

    def get(self, owner_id: str, task_id: str) -> Task: ...
    def pause(self, owner_id: str, task_id: str, *, now: datetime) -> ControlOutcome: ...
    def unpause(self, owner_id: str, task_id: str, *, now: datetime) -> ControlOutcome: ...
    def cancel(self, owner_id: str, task_id: str, *, now: datetime) -> Task: ...


class TaskSteeringService:
    """Maps a steering verb to the owner-scoped task mutation (DI; composition wires the store).

    ``notifier`` + ``persona_tag_resolver`` are optional: wired, a failed cancel that leaves the
    task active surfaces an un-suppressible failure account; absent (unit paths), the cancel-failure
    still logs but cannot notify.
    """

    def __init__(
        self,
        *,
        tasks: TaskMutator,
        notifier: FailureNotifier | None = None,
        persona_tag_resolver: Callable[[str], PersonaIdentityTag | None] | None = None,
    ) -> None:
        """Inject the owner-scoped task mutator (+ optional failure-notification collaborators)."""
        self._tasks = tasks
        self._notifier = notifier
        self._resolve_tag = persona_tag_resolver

    async def steer(self, data: Mapping[str, Any]) -> SteeringOutcome | None:
        """Apply ``verb`` to ``task_id`` for ``owner_id`` (pause / resume / cancel).

        Returns what a pause or resume did, with the control's own note when nothing changed
        (R9-241), so the caller can say why; ``None`` for a cancel, which keeps its own flow.
        """
        owner_id = str(data["owner_id"])
        task_id = str(data["task_id"])
        verb = str(data["verb"])
        now = datetime.now(UTC)
        if verb == SteeringVerb.PAUSE.value:
            return _steered(self._tasks.pause(owner_id, task_id, now=now))
        if verb == SteeringVerb.RESUME.value:
            return _steered(self._tasks.unpause(owner_id, task_id, now=now))
        if verb == SteeringVerb.CANCEL.value:
            await self._cancel(owner_id, task_id, data, now=now)
            return None
        _logger.warning("unknown steering verb {verb!r}", verb=verb)  # pragma: no cover
        return None

    async def _cancel(
        self, owner_id: str, task_id: str, data: Mapping[str, Any], *, now: datetime
    ) -> None:
        """Cancel, and if the cancel does NOT take, surface an un-suppressible failure (A4-D-X)."""
        try:
            self._tasks.cancel(owner_id, task_id, now=now)
            return  # cancelled cleanly
        except Exception as exc:  # noqa: BLE001 — classify by the task's REAL post-attempt state
            if self._still_active(owner_id, task_id):
                _logger.warning("cancel left the task active; surfacing failure", task_id=task_id)
                await self._notify_cancel_failure(owner_id, task_id, data, cause=str(exc))
            else:
                # Already terminal / already gone → the cancel's intent is satisfied; benign no-op.
                _logger.info("cancel no-op (task already terminal/gone)", task_id=task_id)

    def _still_active(self, owner_id: str, task_id: str) -> bool:
        """True iff the task exists and is NOT terminal (a cancel that genuinely failed)."""
        try:
            task = self._tasks.get(owner_id, task_id)
        except TaskNotFoundError:
            return False  # already gone — the cancel's intent is satisfied
        return not is_terminal(task.state)

    async def _notify_cancel_failure(
        self, owner_id: str, task_id: str, data: Mapping[str, Any], *, cause: str
    ) -> None:
        """Emit the cadence-bypass cancel-failure account (best-effort; never raises)."""
        if self._notifier is None or self._resolve_tag is None:
            return
        persona = self._resolve_tag(str(data.get("persona_id", "")))
        if persona is None:
            return
        account = account_for_cancel_failure(task_id, cause=cause)
        try:
            await self._notifier.notify(
                account,
                persona=persona,
                owner_id=owner_id,
                conversation_id=str(data.get("conversation_id", "")),
            )
        except Exception:  # noqa: BLE001 — a notify failure is logged; we cannot do more here
            _logger.error(
                "cancel-failure account could not be delivered task_id={tid}", tid=task_id
            )


def _steered(outcome: ControlOutcome) -> SteeringOutcome:
    """A control's outcome as the runtime's steering outcome (R9-241)."""
    return SteeringOutcome(changed=outcome.changed, note=outcome.note)


class ControlSteeringPort:
    """The loop's :class:`~persona_runtime.task_origination.TaskSteeringPort` (R9-241).

    Applies a chat pause or resume DURING the turn through the same controls the routes
    use, so the persona's reply can say what happened (D-ORCH-11-18-d (4)). The owner is the
    request's RLS scope, read when the verb is applied; off-request there is no owner and the
    task is not found, exactly as the pickup port behaves.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def steer(self, verb: SteeringVerb, task_id: str) -> SteeringOutcome:
        from persona_api.middleware.rls_context import current_user_id
        from persona_api.services.task_control_service import TaskControlMutator

        owner = current_user_id.get()
        if not owner:
            raise TaskNotFoundError("no task context", context={"id": task_id})
        mutator = TaskControlMutator(self._engine)
        now = datetime.now(UTC)
        if verb is SteeringVerb.PAUSE:
            return _steered(mutator.pause(owner, task_id, now=now))
        if verb is SteeringVerb.RESUME:
            return _steered(mutator.unpause(owner, task_id, now=now))
        msg = f"the steering port applies pause and resume only, not {verb.value}"
        raise ValueError(msg)
