"""Unit tests for the shared ``mcp:`` entry parsers (Spec N7, D-N7-1).

Two deliberately distinct semantics:

- ``server_grant_name`` — the bare server-grant form ONLY (exactly one colon;
  what the apps toggle writes and what the toolbox-build expansion consumes);
- ``referenced_server_name`` — grant OR tool form (the builtin-launcher scan:
  tool-level declarations still need their server running).
"""

from __future__ import annotations

import pytest
from persona.tools.mcp.naming import referenced_server_name, server_grant_name


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("mcp:time", "time"),
        ("mcp:docker", "docker"),
        ("mcp:google-flights", "google-flights"),
    ],
)
def test_server_grant_name_accepts_the_bare_grant_form(entry: str, expected: str) -> None:
    assert server_grant_name(entry) == expected


@pytest.mark.parametrize(
    "entry",
    [
        "mcp:time:get_current_time",  # tool form — not a grant
        "mcp:docker:search",
        "mcp:a:b:c",
        "mcp:",  # empty server segment
        "mcp::",  # empty server segment + trailing colon
        "mcp::tool",
        "file_read",  # non-MCP builtin
        "use_skill",
        "",  # empty entry
        "xmcp:a",  # prefix must anchor at the start
        "MCP:time",  # case-sensitive by contract
    ],
)
def test_server_grant_name_rejects_everything_else(entry: str) -> None:
    assert server_grant_name(entry) is None


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("mcp:time", "time"),  # grant form
        ("mcp:time:get_current_time", "time"),  # tool form
        ("mcp:docker:search", "docker"),
        ("mcp:a:b:c", "a"),  # first segment only
    ],
)
def test_referenced_server_name_accepts_both_forms(entry: str, expected: str) -> None:
    assert referenced_server_name(entry) == expected


@pytest.mark.parametrize(
    "entry",
    [
        "mcp:",  # empty server segment
        "mcp::",  # empty first segment
        "mcp::tool",
        "file_read",
        "",
        "xmcp:a",
    ],
)
def test_referenced_server_name_rejects_non_mcp_and_empty(entry: str) -> None:
    assert referenced_server_name(entry) is None


@pytest.mark.parametrize(
    "entry",
    ["mcp:time", "mcp:docker", "mcp:google-flights"],
)
def test_grant_implies_reference_and_they_agree(entry: str) -> None:
    """Agreement property: every grant references the SAME server name."""
    grant = server_grant_name(entry)
    assert grant is not None
    assert referenced_server_name(entry) == grant
