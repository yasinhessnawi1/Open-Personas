"""``persona.tools`` — spec 03 surface.

Tool protocols, the ``@tool`` decorator, Toolbox, provider-aware
result formatting, the four built-in tools, MCP client + adapter,
and the tool-audit Protocol.

Engineering standards §1.1 ("smallest public API surface") — only
the names that downstream specs (5/6/8) need are re-exported here.
"""

from __future__ import annotations

from persona.tools._factory import build_default_toolbox
from persona.tools.audit import (
    JSONLToolAuditLogger,
    MemoryToolAuditLogger,
    ToolAuditEvent,
    ToolAuditLogger,
)
from persona.tools.builtin.calculator import make_calculator_tool
from persona.tools.builtin.currency_convert import make_currency_convert_tool
from persona.tools.builtin.datetime import make_datetime_tool
from persona.tools.builtin.file_read import make_file_read_tool
from persona.tools.builtin.file_write import make_file_write_tool
from persona.tools.builtin.json_query import make_json_query_tool
from persona.tools.builtin.regex_match import make_regex_match_tool
from persona.tools.builtin.render_diagram import make_render_diagram_tool
from persona.tools.builtin.text_diff import make_text_diff_tool
from persona.tools.builtin.text_summarize import make_text_summarize_tool
from persona.tools.builtin.web_fetch import make_web_fetch_tool
from persona.tools.builtin.web_search import make_web_search_tool
from persona.tools.catalog import (
    TOOL_CATALOG,
    ToolCatalogEntry,
    catalog_entry,
    known_tool_names,
    warn_unknown_declared_tools,
)
from persona.tools.categories import (
    FREE_CATEGORIES,
    GATED_BY_DEFAULT,
    ActionCategory,
    resolve_action_categories,
    unmapped_catalog_tools,
)
from persona.tools.category_policy import (
    DEFAULT_POLICY,
    CategoryDecision,
    CategoryPolicy,
    CategoryRule,
    default_decision,
)
from persona.tools.errors import (
    MCPConnectionError,
    MCPServerUnavailableError,
    SandboxViolationError,
    ToolExecutionError,
    ToolNotAllowedError,
)
from persona.tools.formatting import format_tool_result
from persona.tools.kind import ToolKind, resolve_tool_kind
from persona.tools.mcp.adapter import MCPToolAdapter
from persona.tools.mcp.client import MCPClient, load_mcp_clients
from persona.tools.protocol import AsyncTool, ToolDescriptor, tool
from persona.tools.toolbox import Toolbox, ToolboxFactory

__all__ = [
    # Action-category taxonomy + policy matrix (A3)
    "ActionCategory",
    "FREE_CATEGORIES",
    "GATED_BY_DEFAULT",
    "resolve_action_categories",
    "unmapped_catalog_tools",
    "CategoryDecision",
    "CategoryPolicy",
    "CategoryRule",
    "DEFAULT_POLICY",
    "default_decision",
    # Protocols + decorator
    "AsyncTool",
    "JSONLToolAuditLogger",
    # MCP
    "MCPClient",
    "MCPConnectionError",
    "MCPServerUnavailableError",
    "MCPToolAdapter",
    "MemoryToolAuditLogger",
    # Known-tool catalog (spec 26 T08)
    "TOOL_CATALOG",
    # Errors
    "SandboxViolationError",
    "ToolAuditEvent",
    "ToolAuditLogger",
    "ToolCatalogEntry",
    "ToolDescriptor",
    "ToolExecutionError",
    "ToolKind",
    "ToolNotAllowedError",
    # Registry
    "Toolbox",
    "ToolboxFactory",
    # Factory + composer
    "build_default_toolbox",
    "catalog_entry",
    "format_tool_result",
    "known_tool_names",
    "load_mcp_clients",
    "resolve_tool_kind",
    "make_calculator_tool",
    "make_currency_convert_tool",
    "make_datetime_tool",
    "make_file_read_tool",
    "make_file_write_tool",
    "make_json_query_tool",
    "make_regex_match_tool",
    "make_render_diagram_tool",
    "make_text_diff_tool",
    "make_text_summarize_tool",
    "make_web_fetch_tool",
    "make_web_search_tool",
    "tool",
    "warn_unknown_declared_tools",
]
