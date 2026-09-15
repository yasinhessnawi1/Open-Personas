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

import hashlib
import re
from typing import TYPE_CHECKING, Protocol

from persona.backends.types import ToolSpec, tool_spec_from_tool
from persona.errors import ToolExecutionError, ToolNotAllowedError
from persona.logging import get_logger
from persona.tools.kind import ToolKind, resolve_tool_kind

if TYPE_CHECKING:
    from collections.abc import Iterable

    from persona.schema.tools import ToolCall, ToolResult
    from persona.tools.protocol import AsyncTool

__all__ = ["Toolbox", "ToolboxFactory", "wire_tool_name"]

#: Characters a function name may contain on the model wire.
#:
#: Every OpenAI-compatible provider validates tool names against ``[a-zA-Z0-9_-]`` and 400s
#: the WHOLE request when one fails, so a single bad name takes down the turn rather than
#: that tool. Our MCP tools are named ``mcp:<server>:<tool>``, and the colon is illegal.
_WIRE_SAFE = re.compile(r"[^a-zA-Z0-9_-]")

#: The provider cap on a function name.
_WIRE_MAX = 64


def wire_tool_name(name: str) -> str:
    """The name a provider will accept for ``name``.

    **Identity for every name that is already legal**, which is every built-in and every
    skill; only MCP tools change. That is what makes this safe to apply everywhere rather
    than special-casing MCP at each call site.

    Not reversible on its own, deliberately: the reverse is a lookup against the tools this
    Toolbox actually holds (``_wire_to_real``), so a name the model invents cannot be
    decoded into something dispatchable.
    """
    safe = _WIRE_SAFE.sub("_", name)
    return safe[:_WIRE_MAX] if len(safe) > _WIRE_MAX else safe


_logger = get_logger("tools.toolbox")


def _build_wire_map(registry: dict[str, AsyncTool]) -> dict[str, str]:
    """``{wire name: registered name}`` for every tool, collisions resolved deterministically.

    Two different tools CAN sanitise to the same wire name (``mcp:a:b`` and ``mcp_a_b`` both
    become ``mcp_a_b``), and silently dispatching one when the model asked for the other is
    the kind of bug that is indistinguishable from the model misbehaving. So a collision is
    broken by appending a short stable digest of the real name, which is deterministic across
    processes and restarts: the same toolbox always produces the same wire names, so a
    conversation's history stays valid after a redeploy.

    Names that are already wire-safe are never rewritten, so a collision cannot change the
    name of a built-in tool.
    """
    wire_map: dict[str, str] = {}
    for real in sorted(registry):
        wire = wire_tool_name(real)
        if wire not in wire_map:
            wire_map[wire] = real
            continue
        if wire == real:
            # The already-legal name wins its own spelling; the other one moves.
            displaced = wire_map[wire]
            wire_map[wire] = real
            wire_map[_disambiguated(displaced)] = displaced
            continue
        wire_map[_disambiguated(real)] = real
    return wire_map


def _disambiguated(real: str) -> str:
    """A wire name for ``real`` that cannot collide with another tool's plain wire name."""
    digest = hashlib.sha256(real.encode()).hexdigest()[:8]
    stem = wire_tool_name(real)[: _WIRE_MAX - len(digest) - 1]
    return f"{stem}_{digest}"


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
        self._wire_to_real = _build_wire_map(registry)
        # The reverse view. get_specs must advertise the COLLISION-RESOLVED name, not the
        # plain sanitisation, or two tools would be offered under one name and the model's
        # choice between them would be decided by dict order.
        self._real_to_wire = {real: wire for wire, real in self._wire_to_real.items()}

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

    def real_name(self, tool_name: str) -> str:
        """The registered name behind a name the MODEL used (the wire name).

        Identity unless the model is answering about an MCP tool, whose real name carries
        colons that no provider will accept. An unknown name is returned unchanged so the
        existing "tool not available" path still reports what the model actually said.
        """
        return self._wire_to_real.get(tool_name, tool_name)

    def is_allowed(self, tool_name: str) -> bool:
        """True if ``tool_name`` is allowed under the active allow-list.

        Accepts either the registered name or the wire name the model was shown.
        """
        if self._allow_set is None:
            return True
        return self.real_name(tool_name) in self._allow_set

    def names(self) -> list[str]:
        """Sorted list of allowed tool names that are also registered."""
        if self._allow_set is None:
            return sorted(self._tools)
        return sorted(n for n in self._tools if n in self._allow_set)

    def get_specs(self) -> list[ToolSpec]:
        """Return a :class:`ToolSpec` for every allowed + registered tool.

        Names are WIRE names. Every provider rejects the whole request, not the offending
        tool, when a function name contains anything outside ``[a-zA-Z0-9_-]`` — so one
        ``mcp:calculator:calculate`` in the toolbox 400s every turn the persona takes. The
        registered name is restored on the way back in :meth:`dispatch`, so the rest of the
        system, the audit log and the MCP source badge all still see the real one.
        """
        specs = [tool_spec_from_tool(self._tools[name]) for name in self.names()]
        return [
            spec
            if self._real_to_wire.get(spec.name, spec.name) == spec.name
            else spec.model_copy(update={"name": self._real_to_wire[spec.name]})
            for spec in specs
        ]

    def kind_for(self, tool_name: str) -> ToolKind:
        """Resolve a dispatched tool name to its capability kind (spec 30 T01, D-30-1).

        Built-in / skill / ``mcp:builtin`` / ``mcp:optional`` — the source badge
        the frontend renders on each in-chat call. Pure + total (unknown name →
        ``"builtin"``); the resolution lives in :func:`persona.tools.kind.resolve_tool_kind`
        so the taxonomy has one authoritative home (DRY). Static because the kind
        derives from the name + the MCP catalog, not from this Toolbox's registry.

        Takes the wire name the model used and resolves it first: the taxonomy keys off the
        ``mcp:`` prefix, which wire-safety strips, so without this every MCP call would be
        badged as a built-in.
        """
        return resolve_tool_kind(self.real_name(tool_name))

    # Section: dispatch path

    async def dispatch(self, tool_call: ToolCall) -> ToolResult:
        """Dispatch a tool call. See class docstring for the error contract."""
        name = self.real_name(tool_call.name)

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
