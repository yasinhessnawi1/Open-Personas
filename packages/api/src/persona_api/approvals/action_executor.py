"""The ungated action executor for approved-action replay (Spec A6, T-seam).

The :class:`~persona_api.approvals.resolver.ActionExecutor` the resolver replays an APPROVED
proposal through. The security invariant (A6, verbatim replay): the executor dispatches the
**EXACT recorded** ``(tool_name, arguments)`` against the persona's real, **un-gated** toolbox —
the approval *is* the authorization, so execution never re-gates and the model never re-derives
the call. The only interpretation of the user's intent is the deterministic
:func:`persona.approvals.resolve_reply` floor upstream, which the model cannot bypass.

The guardrail (A6): a toolbox **build** failure or a tool **dispatch** failure is caught here and
returned as an error summary — the resolver folds it into a resolution checkpoint (a *failed*
outcome), the task resumes and the leg adapts. It is **never** raised into the request: an
approval reply must not 500.

The toolbox is built by an injected async builder so the dispatch path is exercised against a
real :class:`~persona.tools.toolbox.Toolbox` in tests, while production wires the real per-persona
build (:meth:`RuntimeFactory.build_action_executor`). The build is deferred to :meth:`execute`
(only an actual APPROVE pays it — listing/denying approvals build no toolbox).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.schema.tools import ToolCall

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Mapping

    from persona.tools.toolbox import Toolbox
    from pydantic import JsonValue

__all__ = ["ToolboxActionExecutor"]

_log = get_logger("api.approvals.action_executor")


class ToolboxActionExecutor:
    """Replay one approved action verbatim against a real, un-gated toolbox (A6 T-seam)."""

    def __init__(self, build_toolbox: Callable[[], Awaitable[Toolbox]]) -> None:
        self._build_toolbox = build_toolbox

    async def execute(self, tool_name: str, arguments: Mapping[str, JsonValue]) -> str:
        """Dispatch the EXACT recorded ``(tool_name, arguments)`` verbatim; never raise (A6).

        Returns the tool's result summary, or an ``"Execution failed: …"`` summary on any build /
        dispatch failure (the resolver records that as a failed outcome and resumes the task).
        """
        try:
            toolbox = await self._build_toolbox()
            result = await toolbox.dispatch(ToolCall(name=tool_name, args=dict(arguments)))
        except Exception as exc:  # noqa: BLE001 — resilience boundary: a reply must never 500
            _log.warning("approval action execution failed", tool_name=tool_name, error=str(exc))
            return f"Execution failed: {exc}"
        if result.is_error:
            _log.info("approval action tool returned an error", tool_name=tool_name)
            return f"Execution failed: {result.content}"
        return result.content
