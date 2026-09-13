"""Toolbox — the registry that holds tools and enforces the persona allow-list.

Constructed with a sequence of :class:`AsyncTool` instances plus an
``allow_list`` (literal-only — Phase 1 refinement #4; no wildcards in v0.1).
``None`` means "all registered tools allowed" with a WARNING log
(development convenience per D-03-7).

Dispatch:
- :class:`ToolNotAllowedError` raised when the requested tool name is not in
  the allow-list. ``context["allowed"]`` is a comma-joined string of
  available names (D-03-8) so the runtime can feed the list back to the
  model.
- :class:`ToolExecutionError` raised when the requested name is allowed but
  no tool with that name is registered (configuration error).
- The tool's own ``execute`` is awaited; per D-03-5 the ``@tool`` decorator
  already wraps body exceptions, so the toolbox never sees a raise from a
  decorated tool. We do not re-wrap.

MCP tools register with their full prefixed name (``mcp:server:tool``); the
allow-list contains the same prefix.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from persona.backends.types import ToolSpec, tool_spec_from_tool
from persona.errors import ToolExecutionError, ToolNotAllowedError
from persona.logging import get_logger
from persona.tools.kind import ToolKind, resolve_tool_kind

if TYPE_CHECKING:
    from collections.abc import Iterable

    from persona.schema.tools import ToolCall, ToolResult
    from persona.tools.protocol import AsyncTool

__all__ = ["Toolbox", "ToolboxFactory"]

_logger = get_logger("tools.toolbox")


class ToolboxFactory(Protocol):
    """How a composition root substitutes the toolbox CLASS a persona's tools land in.

    :func:`persona.tools.build_default_toolbox` assembles the tool list and the allow-list,
    then calls this to construct the box. The default is :class:`Toolbox` itself; the
    task-leg runner passes a factory that builds the policy-gated subclass (Spec A3's
    ``PolicyGatedToolbox``), so a leg's toolbox enforces its contract's category policy
    without the factory knowing anything about approvals (Spec W1, D-W1-1).
    """

    def __call__(
        self, tools: Iterable[AsyncTool], *, allow_list: list[str] | None = None
    ) -> Toolbox: ...


class Toolbox:
    """Tool registry + literal-only allow-list + dispatch.

    Args:
        tools: All registered tools (built-in + MCP-discovered).
        allow_list: Persona's declared tool names. ``None`` → all allowed
            with a WARNING log (development; production must pass an
            explicit list per D-03-7).

    Raises:
        ValueError: If two tools share the same name.
    """

    def __init__(
        self,
        tools: Iterable[AsyncTool],
        *,
        allow_list: list[str] | None = None,
    ) -> None:
        registry: dict[str, AsyncTool] = {}
        for t in tools:
            if t.name in registry:
                msg = f"duplicate tool name in Toolbox: {t.name!r}"
                raise ValueError(msg)
            registry[t.name] = t
        self._tools = registry

        if allow_list is None:
            # Permissive default — development convenience (D-03-7).
            self._allow_set: frozenset[str] | None = None
            _logger.warning(
                "Toolbox allow_list is None — ALL tools allowed; "
                "production personas must declare an explicit allow_list",
                registered=len(self._tools),
            )
        else:
            self._allow_set = frozenset(allow_list)

        _logger.info(
            "toolbox constructed",
            registered=len(self._tools),
            allowed=len(self._allow_set) if self._allow_set is not None else "all",
        )

    # Section: query methods

    def is_allowed(self, tool_name: str) -> bool:
        """True if ``tool_name`` is allowed under the active allow-list."""
        if self._allow_set is None:
            return True
        return tool_name in self._allow_set

    def names(self) -> list[str]:
        """Sorted list of allowed tool names that are also registered."""
        if self._allow_set is None:
            return sorted(self._tools)
        return sorted(n for n in self._tools if n in self._allow_set)

    def get_specs(self) -> list[ToolSpec]:
        """Return a :class:`ToolSpec` for every allowed + registered tool."""
        return [tool_spec_from_tool(self._tools[name]) for name in self.names()]

    @staticmethod
    def kind_for(tool_name: str) -> ToolKind:
        """Resolve a dispatched tool name to its capability kind (spec 30 T01, D-30-1).

        Built-in / skill / ``mcp:builtin`` / ``mcp:optional`` — the source badge
        the frontend renders on each in-chat call. Pure + total (unknown name →
        ``"builtin"``); the resolution lives in :func:`persona.tools.kind.resolve_tool_kind`
        so the taxonomy has one authoritative home (DRY). Static because the kind
        derives from the name + the MCP catalog, not from this Toolbox's registry.
        """
        return resolve_tool_kind(tool_name)

    # Section: dispatch path

    async def dispatch(self, tool_call: ToolCall) -> ToolResult:
        """Dispatch a tool call. See class docstring for the error contract."""
        name = tool_call.name

        if not self.is_allowed(name):
            allowed = self.names()
            _logger.warning(
                "tool not allowed",
                called=name,
                allowed_count=len(allowed),
            )
            raise ToolNotAllowedError(
                "tool not allowed",
                context={
                    "called": name,
                    # D-03-8: comma-joined string (PersonaError.context is dict[str, str]).
                    "allowed": ", ".join(allowed),
                },
            )

        tool = self._tools.get(name)
        if tool is None:
            _logger.error(
                "tool allowed but not registered",
                called=name,
            )
            raise ToolExecutionError(
                "tool allowed but not registered",
                context={"name": name, "reason": "not_registered"},
            )

        _logger.debug("dispatching tool", tool=name, call_id=tool_call.call_id)
        result = await tool.execute(**tool_call.args)
        _logger.debug(
            "tool dispatched",
            tool=name,
            call_id=tool_call.call_id,
            is_error=result.is_error,
        )
        return result
