"""The ``task_pickup`` tool — the persona acting on its own stalled work (Spec W1, T9).

``task_introspect`` let a persona SEE that one of its tasks had stalled; nothing let it do
anything about it. So the honest answer to "what happened to the GitHub research?" was a
description of a dead end. This is the other half: having seen it, the persona can carry it on.

Deliberately narrow. It takes a task id and nothing else: no goal, no instructions, no new
work. Everything about WHICH tasks may be picked up lives behind the injected port, which the
composition root binds to this persona and resolves the caller at dispatch — so another
tenant's task, another persona's task, and a task whose owner has paused autonomy are all
refused there, not here (D-W1-10). A tool that could be talked into a wider scope by a clever
argument is not a narrow tool, so the scope is not an argument.

It reports what actually happened rather than what it hoped: picking up a task that has
already finished, or whose owner is paused, is a calm no-op with the reason, never an error and
never a claim that work resumed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from persona.schema.tools import ToolResult
from persona.tools.protocol import tool

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.tools.protocol import AsyncTool

__all__ = ["TASK_PICKUP_TOOL_NAME", "PickupOutcome", "TaskPickupPort", "make_task_pickup_tool"]

TASK_PICKUP_TOOL_NAME = "task_pickup"

_GUIDANCE = (
    "Carry on a task of yours that has stalled: one that `task_introspect` shows as waiting "
    "or stuck. Give the task_id exactly as that tool reported it. Use this only when the user "
    "asks you to get back to it, or when you have just told them something of yours is "
    "stuck and they want it continued. It resumes work that already exists; it cannot start "
    "anything new, take on another persona's task, or change what a task is for. Report the "
    "result as given: if it says the task already finished, say that, and do not claim you "
    "have picked anything up."
)


class PickupOutcome(BaseModel):
    """What the durable seam did, in the persona's terms.

    ``changed`` false is not a failure: a finished task, a paused owner and a suspended persona
    are all honest no-ops, and ``note`` is the sentence the seam gave for it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    changed: bool
    note: str = ""
    goal: str = ""


@runtime_checkable
class TaskPickupPort(Protocol):
    """The durable pickup seam, bound by the composition root to ONE persona's own tasks.

    The implementation owns every scope question: the caller's owner (resolved per dispatch,
    never per build), that the task belongs to this persona, and the pause gates. It raises
    :class:`~persona.errors.TaskNotFoundError` for anything outside that scope, so a task the
    persona may not touch is indistinguishable from one that does not exist.
    """

    def pick_up(self, task_id: str) -> PickupOutcome: ...


def make_task_pickup_tool(
    *,
    port_provider: Callable[[], TaskPickupPort | None],
    persona_id: str | None = None,  # noqa: ARG001 — provenance only, like the reader tools
) -> AsyncTool:
    """Build the ``task_pickup`` tool over an owner-scoped port provider (Spec W1, D-W1-10).

    Args:
        port_provider: Resolves the caller's port at DISPATCH time (the toolbox is built once;
            the owner is per-request). ``None`` fails closed, as off-request it must.
        persona_id: The persona picking up (provenance only; the port carries the binding).
    """

    @tool(name=TASK_PICKUP_TOOL_NAME, description=_GUIDANCE)
    async def task_pickup(task_id: str) -> ToolResult:
        from persona.errors import TaskNotFoundError

        port = port_provider()
        if port is None:
            return ToolResult(
                tool_name=TASK_PICKUP_TOOL_NAME,
                content="No task context available right now.",
                is_error=True,
            )
        wanted = task_id.strip()
        if not wanted:
            return ToolResult(
                tool_name=TASK_PICKUP_TOOL_NAME,
                content="Tell me which task: give the task_id from task_introspect.",
                is_error=True,
            )
        try:
            outcome = port.pick_up(wanted)
        except TaskNotFoundError:
            # Out of scope reads exactly like absent: another tenant's task, another persona's
            # task and a made-up id are one answer, so nothing here confirms what exists.
            return ToolResult(
                tool_name=TASK_PICKUP_TOOL_NAME,
                content=f"I don't have a task with id {wanted!r}.",
                is_error=True,
            )
        return ToolResult(
            tool_name=TASK_PICKUP_TOOL_NAME,
            content=_render(outcome),
            data=outcome.model_dump(mode="json"),
        )

    return task_pickup


def _render(outcome: PickupOutcome) -> str:
    """The one line the model reports from — the seam's own words when nothing moved."""
    subject = f"“{outcome.goal}”" if outcome.goal else "that task"
    if outcome.changed:
        return f"Picked {subject} back up. I'm on it again."
    return outcome.note or f"Nothing to pick up on {subject}."
