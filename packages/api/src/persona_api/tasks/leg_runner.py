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
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.tasks import LegBox
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.agentic.run import CancelToken, Run, StepUsage

    from persona_api.services.runtime_factory import RuntimeFactory

__all__ = ["RuntimeFactoryLegRunnerBuilder"]


class _DeferredLoopRunner:
    """An :class:`AgenticRunner` that builds its :class:`AgenticLoop` on first ``run`` (async)."""

    def __init__(self, factory: RuntimeFactory, persona_id: str) -> None:
        self._factory = factory
        self._persona_id = persona_id

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
        loop so the api handler can meter the leg's real per-step cost.
        """
        loop = await self._factory.build_agentic_loop(self._persona_id)
        return await loop.run(
            task, on_event=on_event, cancel_token=cancel_token, on_step_usage=on_step_usage
        )


class RuntimeFactoryLegRunnerBuilder:
    """Build a leg's runner from the shared :class:`RuntimeFactory` (the identical runtime)."""

    def __init__(self, factory: RuntimeFactory) -> None:
        """Inject the app's runtime factory (the same one the chat/run paths use)."""
        self._factory = factory

    def build(self, task_id: str, persona_id: str, box: LegBox) -> _DeferredLoopRunner:  # noqa: ARG002
        """Return the runner for this leg. ``box`` is enforced by the executor, not the loop."""
        return _DeferredLoopRunner(self._factory, persona_id)
