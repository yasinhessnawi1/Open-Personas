"""Runtime MCP-gap detection (spec 27 T11, D-27-7).

The MCP-layer mirror of :mod:`persona_runtime.proactive_tool_gap`: when the model
says it *can't* do something and a **catalog MCP server** would enable it, but the
persona has no ``mcp:<server>:`` tool available, this offers one-tap consent via a
Spec-21 :class:`ProactiveQuestion` (the 3+1 shape).

Detection is **keyword/heuristic tier-1** (D-27-7, identical to D-26-4): a
capability-gap phrase ("I can't", "I don't have …") must co-occur with an MCP
catalog server's keyword. The catalog (``persona.tools.mcp.catalog``) owns the
phrase→server vocabulary, so adding a server extends detection automatically. A
semantic tier-2 is a named fast-follow, not built here.

Pure functions; no I/O. The loop calls :func:`detect_mcp_gap` post-generation
(mutually exclusive with the tool-gap offer — one offer per turn) and, when it
fires, surfaces :func:`build_mcp_gap_question` and records the gap on the TurnLog.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from persona.tools.mcp.catalog import BUILTIN_MCP_CATALOG, recommender_provider_tag
from pydantic import BaseModel, ConfigDict

from persona_runtime.questions import ProactiveProposal, ProactiveQuestion, QuestionOption

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["MCPGapSignal", "build_mcp_gap_question", "detect_mcp_gap"]

#: Capability-gap phrases — identical set to the tool-gap detector (D-27-7).
_GAP_PHRASES: tuple[str, ...] = (
    "i don't have",
    "i do not have",
    "i can't",
    "i cannot",
    "i'm not able to",
    "i am not able to",
    "i'm unable to",
    "i am unable to",
    "i don't have access",
    "i do not have access",
    "no access to",
    "don't have a tool",
    "don't have the tool",
    "i lack the",
    "i don't have the ability",
)
_GAP_RE = re.compile("|".join(re.escape(p) for p in _GAP_PHRASES))

#: The persona allow-list prefix an MCP server tool carries: ``mcp:<server>:``.
_MCP_PREFIX = "mcp:"


class MCPGapSignal(BaseModel):
    """A detected capability gap a persona could close by enabling an MCP server.

    Attributes:
        server_name: The catalog MCP server that would close the gap (no
            ``mcp:<server>:`` tool is available to the persona).
        provider: ``"mcp:builtin"`` (default-enabled) or ``"mcp:optional"``
            (opt-in built-in / BYO external) — shapes the consent copy.
        capability: A first-person VERB phrase for the consent line (slots into
            ``"…which would let me {capability}."``). Sourced from the catalog
            entry's ``capability``, falling back to a lower-cased ``description``.
        matched_keyword: The catalog keyword that fired (for telemetry/debug).
        required_env: Env vars the operator must set to enable the server (e.g.
            ``GITHUB_TOKEN``); surfaced in the consent question.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    server_name: str
    provider: str
    capability: str
    matched_keyword: str
    required_env: tuple[str, ...] = ()


def _available_servers(available_tools: Iterable[str]) -> set[str]:
    """The MCP server names a persona already has tools for (``mcp:<name>:...``)."""
    servers: set[str] = set()
    for name in available_tools:
        if name.startswith(_MCP_PREFIX):
            servers.add(name[len(_MCP_PREFIX) :].split(":", 1)[0])
    return servers


def detect_mcp_gap(model_output: str, available_tools: Iterable[str]) -> MCPGapSignal | None:
    """Detect a closeable MCP gap in the model's output (D-27-7, tier-1).

    Returns an :class:`MCPGapSignal` when (a) a capability-gap phrase is present
    AND (b) a catalog MCP server's keyword appears AND (c) no ``mcp:<server>:``
    tool for that server is available. Returns ``None`` otherwise. The first
    catalog match (in catalog order) wins — at most one offer per turn. Pure +
    deterministic.

    Args:
        model_output: The assistant's final turn text. May be empty.
        available_tools: The tools currently available to the persona (e.g.
            ``Toolbox.names()``).

    Returns:
        The gap signal, or ``None`` if no closeable gap was detected.
    """
    if not model_output:
        return None
    lowered = model_output.lower()
    if not _GAP_RE.search(lowered):
        return None
    available = _available_servers(available_tools)
    for name, entry in BUILTIN_MCP_CATALOG.servers.items():
        if name in available or not entry.keywords:
            continue
        for keyword in entry.keywords:
            if keyword in lowered:
                # Prefer the catalog's verb-phrase capability; fall back to a
                # lower-cased description so the consent line stays grammatical.
                capability = entry.capability or (
                    (entry.description[0].lower() + entry.description[1:]).rstrip(".")
                    if entry.description
                    else ""
                )
                return MCPGapSignal(
                    server_name=name,
                    provider=recommender_provider_tag(entry),
                    capability=capability,
                    matched_keyword=keyword,
                    required_env=entry.required_env,
                )
    return None


def build_mcp_gap_question(signal: MCPGapSignal) -> ProactiveQuestion:
    """Build the Spec-21 3+1 consent question for a detected MCP gap (D-27-7)."""
    capability = signal.capability.rstrip(".")
    if signal.required_env:
        enable_detail = (
            f"Configure the `{signal.server_name}` MCP server (requires "
            f"{', '.join(signal.required_env)} in the environment) and try again."
        )
    else:
        enable_detail = (
            f"Enable the `{signal.server_name}` MCP server for this persona and try again."
        )
    return ProactiveQuestion(
        question=(
            f"I don't have the `{signal.server_name}` MCP server, which would let me "
            f"{capability}. Want me to enable it for this persona?"
        ),
        options=(
            QuestionOption(label="Enable it and retry", description=enable_detail),
            QuestionOption(
                label="Find another way",
                description="Answer using the tools I already have.",
            ),
            QuestionOption(
                label="Just explain the server",
                description="Tell me what it does without enabling it.",
            ),
        ),
        allow_free_form=True,
        # Spec 30 (D-30-2 / D-30-X-mcp-gap-accept-target): the rail's accept
        # target — grant the built-in MCP server as the `mcp:<server>` allow-list
        # entry via POST /personas/{id}/tools (the consent path, extended to
        # admit catalog-valid mcp:<server> names). `provider` carries the
        # mcp:builtin / mcp:optional badge for display.
        proposal=ProactiveProposal(
            kind="mcp",
            name=f"mcp:{signal.server_name}",
            action="grant_tool",
            provider=signal.provider,
        ),
    )
