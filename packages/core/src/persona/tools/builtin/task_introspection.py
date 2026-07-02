"""The ``task_introspect`` tool — the persona's only window onto its own work (Spec A4, T7).

A model-callable, **read-only** tool that answers "how's it going?" / "what are you working on?"
from typed task state and **nothing else**. With no ``task_id`` it lists the caller's active
tasks (list-then-introspect — the model picks the one the user meant); with a ``task_id`` it
returns the full grounded view. The output is a projection of :class:`TaskStateView` — distilled
conclusions, status, next step, what it's waiting on, spend — and carries **no raw transcript**,
because the underlying :class:`TaskStateReader` exposes none (D-A2-1). So the persona can only
narrate what actually happened: a confabulated progress report has nothing to draw on.

Owner scoping + RLS live in the injected reader (resolved at dispatch via ``reader_provider``);
no reader (no request owner) fails closed. The tool is built once per conversation and stays
correctly scoped because the owner is resolved per call, not per build (the ``record_user_fact``
precedent).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.errors import TaskNotFoundError
from persona.schema.tools import ToolResult
from persona.tasks.reader import (
    IntrospectionStatus,
    TaskStateView,
    project_task_state,
    summarise_task,
)
from persona.tools.protocol import tool

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from persona.tasks.entity import Task
    from persona.tasks.reader import TaskStateReader, TaskSummary
    from persona.tools.protocol import AsyncTool

__all__ = ["make_task_introspection_tool"]

_GUIDANCE = (
    "Report on your standing tasks — your progress, status, and what you're waiting on — using "
    "ONLY this tool's output. Call it with no task_id to list your tasks, or with a task_id for "
    "full detail. Never invent or embellish progress: if the tool says a task just started, say "
    "it just started; if it's stuck or waiting on the user, say so honestly. The tool returns "
    "only typed state, never a transcript — so do not narrate steps it does not report."
)

_STATUS_PHRASE: dict[IntrospectionStatus, str] = {
    IntrospectionStatus.JUST_CREATED: "just started — no work has run yet",
    IntrospectionStatus.PROGRESSING: "in progress",
    IntrospectionStatus.WAITING_ON_USER: "waiting on you",
    IntrospectionStatus.SCHEDULED: "scheduled — waiting for its next run",
    IntrospectionStatus.COMPLETED: "completed",
    IntrospectionStatus.FAILED: "failed",
    IntrospectionStatus.CANCELLED: "cancelled",
    IntrospectionStatus.PAUSED: "paused",
}


def make_task_introspection_tool(
    *,
    reader_provider: Callable[[], TaskStateReader | None],
    persona_id: str | None = None,  # noqa: ARG001 — accepted for parity with the tool factories; provenance only
) -> AsyncTool:
    """Build the ``task_introspect`` tool bound to an owner-scoped reader provider (A4-D-5).

    Args:
        reader_provider: Resolves the current owner's :class:`TaskStateReader` at DISPATCH time
            (the toolbox is built once; the owner is per-request). ``None`` ⇒ fail closed.
        persona_id: The persona introspecting (provenance only).
    """

    @tool(name="task_introspect", description=_GUIDANCE)
    async def task_introspect(task_id: str = "") -> ToolResult:
        reader = reader_provider()
        if reader is None:
            return ToolResult(
                tool_name="task_introspect",
                content="No task context available right now.",
                is_error=True,
            )
        if not task_id.strip():
            summaries = [summarise_task(t) for t in reader.list_active()]
            return ToolResult(
                tool_name="task_introspect",
                content=_render_list(summaries),
                data={"tasks": [s.model_dump(mode="json") for s in summaries]},
            )
        try:
            task: Task = reader.get_task(task_id)
        except TaskNotFoundError:
            return ToolResult(
                tool_name="task_introspect",
                content=f"I don't have a task with id {task_id!r}.",
                is_error=True,
            )
        view = project_task_state(task, reader.get_latest_checkpoint(task_id))
        return ToolResult(
            tool_name="task_introspect",
            content=_render_view(view),
            data=view.model_dump(mode="json"),
        )

    return task_introspect


def _render_list(summaries: Sequence[TaskSummary]) -> str:
    """Render the active-task list (id · goal · status) for the model to pick from."""
    if not summaries:
        return "You have no active tasks right now."
    lines = [f"- {s.task_id}: {s.goal} ({_STATUS_PHRASE[s.status]})" for s in summaries]
    return "Your active tasks:\n" + "\n".join(lines)


def _render_view(view: TaskStateView) -> str:
    """Render one task's grounded state — only what the typed view holds."""
    lines = [f"Task: {view.goal}", f"Status: {_STATUS_PHRASE[view.status]}"]
    if view.progress:
        lines.append("Progress so far:")
        lines.extend(f"  - {p}" for p in view.progress)
    elif view.status is IntrospectionStatus.JUST_CREATED:
        lines.append("Nothing to report yet — the first run hasn't happened.")
    if view.next_step:
        lines.append(f"Next step: {view.next_step}")
    if view.wait_reason:
        lines.append(f"Waiting on: {view.wait_reason}")
    if view.open_questions:
        lines.append("Open questions:")
        lines.extend(f"  - {q}" for q in view.open_questions)
    if view.spent_micros:
        lines.append(f"Spent so far: {view.spent_micros / 10_000:g}kr")
    return "\n".join(lines)
