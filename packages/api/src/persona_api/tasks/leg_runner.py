"""The production leg runner — a task leg runs the identical persona-runtime (Spec A4, no-bypass).

A2's :class:`~persona_api.tasks.handler.LegRunnerBuilder` needs a concrete plug that builds the
:class:`~persona_runtime.legs.executor.AgenticRunner` for a boxed leg. This is that plug: it builds
the **same** :class:`~persona_runtime.agentic.loop.AgenticLoop` the interactive run path uses (via
:meth:`RuntimeFactory.build_agentic_loop`), so a leg and a chat compose the identical runtime — the
no-bypass guarantee made structural at the composition root, not merely asserted.

``build`` is synchronous (the A2 Protocol), but ``build_agentic_loop`` is async; the async
construction is deferred into the runner's own async ``run`` (:class:`_DeferredLoopRunner`), which
builds a fresh loop per leg. Fresh-per-leg is deliberate: each leg is an independent unit of work
(A2's design), so no conversational state bleeds between legs.

**The policy gate lives here (Spec W1, T2; D-W1-1).** Spec A3 documented its
``PolicyGatedToolbox`` as "injected only at the leg runner", but no production module ever
constructed it, so every leg ran on the bare toolbox and no contract's category policy was
enforced. Given a ``recorder`` (the api ``ApprovalStore``), this builder now derives a
:class:`~persona_api.tasks.gate.LegGate` from the task it is handed and builds the loop over the
gated toolbox: an allowed category dispatches unchanged, a denied one returns a recoverable error,
a gated one records the proposal durably and ends the leg ``WAITING_APPROVAL`` (A3-D-X-gate-
mechanism). Ad hoc tasks carry the default policy (D-W1-7), so a one-off is gated by construction.
Without a recorder (a bare A2 worker in tests) the loop is built ungated, as before.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_api.errors import LegGateMissingTaskError
from persona_api.tasks.gate import LegGate

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.approvals.gating import ProposalRecorder
    from persona.tasks import LegBox, Task
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.agentic.run import CancelToken, Run, StepUsage

    from persona_api.services.runtime_factory import RuntimeFactory

__all__ = ["RuntimeFactoryLegRunnerBuilder"]


class _DeferredLoopRunner:
    """An :class:`AgenticRunner` that builds its :class:`AgenticLoop` on first ``run`` (async)."""

    def __init__(self, factory: RuntimeFactory, persona_id: str, *, gate: LegGate | None) -> None:
        self._factory = factory
        self._persona_id = persona_id
        self._gate = gate

    async def run(
        self,
        task: str,
        *,
        on_event: Callable[[RunEvent], Awaitable[None]],
        cancel_token: CancelToken,
        on_step_usage: Callable[[StepUsage], Awaitable[None]] | None = None,
    ) -> Run:
        """Build the identical-runtime loop, then delegate the leg's execution to it.

        Spec M3 (T4b): ``on_step_usage`` (the leg-billing hook) is forwarded to the
        loop so the api handler can meter the leg's real per-step cost. Spec W1: the
        toolbox is the policy-gated one whenever this runner carries a gate, and the loop
        parks on a question rather than answering it for the user (D-W1-34).
        """
        loop = await self._factory.build_agentic_loop(
            self._persona_id,
            toolbox_factory=self._gate.toolbox_factory() if self._gate is not None else None,
            # Spec W1 (D-W1-34): in a leg the user is reachable through the task, so a
            # question parks the task instead of being answered on their behalf.
            park_on_question=True,
        )
        return await loop.run(
            task, on_event=on_event, cancel_token=cancel_token, on_step_usage=on_step_usage
        )


class RuntimeFactoryLegRunnerBuilder:
    """Build a leg's runner from the shared :class:`RuntimeFactory` (the identical runtime).

    Args:
        factory: The app's runtime factory (the same one the chat/run paths use).
        recorder: The durable proposal sink the gate records into (the api
            ``ApprovalStore``). ``None`` builds ungated legs (the bare A2 shape in tests).
        network_enabled: Whether the sandbox has network egress (A3-D-1 escalation).
    """

    def __init__(
        self,
        factory: RuntimeFactory,
        *,
        recorder: ProposalRecorder | None = None,
        network_enabled: bool = False,
    ) -> None:
        self._factory = factory
        self._recorder = recorder
        self._network_enabled = network_enabled

    def build(
        self,
        task_id: str,
        persona_id: str,
        box: LegBox,  # noqa: ARG002 — enforced by the executor, not the loop
        *,
        task: Task | None = None,
    ) -> _DeferredLoopRunner:
        """Return the runner for this leg, gated on ``task``'s policy when a recorder is wired.

        Raises:
            LegGateMissingTaskError: A recorder is wired but no task was handed over, so the
                leg cannot be gated. Fail fast rather than silently run ungated.
        """
        gate: LegGate | None = None
        if self._recorder is not None:
            if task is None:
                raise LegGateMissingTaskError(
                    "a policy-gated leg needs its task to derive the gate",
                    context={"task_id": task_id, "persona_id": persona_id},
                )
            gate = LegGate.for_task(
                task, recorder=self._recorder, network_enabled=self._network_enabled
            )
        return _DeferredLoopRunner(self._factory, persona_id, gate=gate)
