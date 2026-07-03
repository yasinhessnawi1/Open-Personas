"""Runtime tool-consent — enable a tool on a persona's allow-list (spec 26 T11).

When a user accepts a runtime tool-gap offer (T10), this service:

1. Adds the tool to the persona's ``tools`` allow-list in the ``personas.yaml``
   TEXT column (the existing storage; NO migration — D-26-X-T12-turnlog-no-migration
   / T11 ruling). The mutation is targeted (re-serialise the YAML with the tool
   appended); it does NOT re-index memory.
2. Records the grant as a ``persona_self`` write in the versioned ``self_facts``
   store — append-only + rollback-capable (Spec 01). Per
   D-26-X-self-facts-consent-write-contract the write MUST pass ``force=True`` +
   ``confidence >= 0.8`` + a meaningful ``reason`` (the store's persona_self
   policy rejects anything weaker).

Mirrors the Spec-21 autonomy-learner persona_self precedent
(``persona.autonomy``). Granting a tool that is not in the known-tool catalog
raises :class:`~persona.errors.ToolNotAllowedError` (we never add a hallucinated
name to an allow-list).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import yaml
from persona.audit import AuditLogger, JSONLAuditLogger
from persona.errors import PersonaNotFoundError, ToolNotAllowedError
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores import SelfFactsStore
from persona.stores.postgres import PostgresBackend
from persona.tools import known_tool_names
from persona.tools.mcp.catalog import known_mcp_server_names
from sqlalchemy import select, update

from persona_api.db.models import personas as personas_t
from persona_api.services.persona_service import load_persona_from_yaml

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from persona.stores.backend import Backend
    from persona.stores.embedder import Embedder
    from sqlalchemy.engine import Engine

__all__ = ["grant_tool_consent"]

#: Confidence stamped on the consent audit chunk. A user-confirmed grant is a
#: high-confidence, well-justified event — clears the persona_self ≥0.8 policy.
_CONSENT_CONFIDENCE = 0.95


def _logical_id(tool_name: str) -> str:
    return f"tool_consent::{tool_name}"


def _is_valid_consent_target(tool_name: str) -> bool:
    """Whether ``tool_name`` may be granted onto a persona's allow-list.

    A built-in tool (catalog name), OR a built-in MCP server in the form
    ``mcp:<server>`` whose ``<server>`` is in the MCP catalog (Spec 30,
    D-30-X-mcp-gap-accept-target — the MCP-gap accept reuses this consent path).
    Never a hallucinated name. Bring-your-own MCP servers are NOT granted here —
    they use the assignment path (D-30-6), not the catalog allow-list.
    """
    if tool_name in known_tool_names():
        return True
    if tool_name.startswith("mcp:"):
        parts = tool_name.split(":")
        server = parts[1] if len(parts) >= 2 else ""
        return bool(server) and server in known_mcp_server_names()
    return False


def grant_tool_consent(
    *,
    rls_engine: Engine,
    embedder: Embedder,
    audit_root: Path,
    persona_id: str,
    owner_id: str,
    tool_name: str,
    written_by: str,
    now: datetime,
    turn_index: int | None = None,
    memory_backend: Backend | None = None,
    audit_logger: AuditLogger | None = None,
) -> bool:
    """Enable ``tool_name`` on the persona's allow-list with a persona_self audit.

    Args:
        rls_engine: RLS-scoped engine (tenant set via the request contextvar).
        embedder: Persona-memory embedder (for the self_facts audit chunk).
        audit_root: JSONL audit root for the self_facts store.
        persona_id: The persona to grant the tool to.
        owner_id: The persona's owner (for YAML re-validation).
        tool_name: The catalog tool to enable, or an ``mcp:<server>`` built-in
            MCP server (Spec 30, D-30-X-mcp-gap-accept-target).
        written_by: The acting user id (recorded on the audit chunk).
        now: tz-aware UTC timestamp.
        turn_index: Optional conversation turn the consent came from (audit).
        memory_backend: The edition's typed-memory backend (Chroma for community,
            Postgres for cloud). When ``None``, defaults to ``PostgresBackend``
            (cloud behavior); the community SQLite path MUST inject Chroma or the
            self_facts audit write fails with ``no such table: memory_chunks``.

    Returns:
        ``True`` if the tool was newly added; ``False`` if the persona already
        had it (idempotent — no write, no audit).

    Raises:
        ToolNotAllowedError: ``tool_name`` is neither a known catalog tool nor a
            catalog-valid ``mcp:<server>`` (never a hallucinated name).
        PersonaNotFoundError: the persona does not exist (under the RLS scope).
    """
    if not _is_valid_consent_target(tool_name):
        raise ToolNotAllowedError(
            "cannot enable an unknown tool",
            context={"tool": tool_name, "persona_id": persona_id},
        )

    # Load the current YAML under the RLS scope.
    with rls_engine.begin() as conn:
        row = (
            conn.execute(select(personas_t.c.yaml).where(personas_t.c.id == persona_id))
            .mappings()
            .first()
        )
    if row is None:
        raise PersonaNotFoundError("persona not found", context={"id": persona_id})

    raw = yaml.safe_load(str(row["yaml"]))
    if not isinstance(raw, dict):
        raise PersonaNotFoundError("persona yaml malformed", context={"id": persona_id})
    tools = list(raw.get("tools") or [])
    if tool_name in tools:
        return False  # idempotent — already granted

    # 1. Targeted allow-list mutation, persisted to the YAML column (no re-index).
    tools.append(tool_name)
    raw["tools"] = tools
    new_yaml = yaml.safe_dump(raw, sort_keys=False, allow_unicode=True)
    # Re-validate before persisting (fail fast on a malformed result).
    load_persona_from_yaml(new_yaml, persona_id=persona_id, owner_id=owner_id)
    with rls_engine.begin() as conn:
        result = conn.execute(
            update(personas_t)
            .where(personas_t.c.id == persona_id)
            .values(yaml=new_yaml)
            .returning(personas_t.c.id)
        )
        if result.first() is None:
            raise PersonaNotFoundError("persona not found", context={"id": persona_id})

    # 2. persona_self audit into the versioned self_facts store (force + ≥0.8 +
    #    reason, per D-26-X-self-facts-consent-write-contract).
    # The edition's typed-memory backend (Chroma for community, Postgres for
    # cloud); a hardcoded PostgresBackend has no memory_chunks table on the
    # community SQLite path (Spec 33 D-33-X-memory-chroma-community). Defaults to
    # PostgresBackend when none is injected (existing cloud callers unaffected).
    backend = memory_backend or PostgresBackend(engine=rls_engine, embedder=embedder)
    store = SelfFactsStore(
        backend=backend,
        # R5-D-2: app-selected audit backend (Postgres when multi-worker), else
        # the JSONL default (byte-unchanged for callers that pass no logger).
        audit_logger=audit_logger or JSONLAuditLogger(audit_root),
    )
    logical_id = _logical_id(tool_name)
    next_version = len(store.history(persona_id, logical_id)) + 1
    reason = f"user granted '{tool_name}' via runtime tool-gap consent" + (
        f", turn {turn_index}" if turn_index is not None else ""
    )
    chunk = PersonaChunk(
        id=f"{persona_id}::self_facts::tool_consent::{tool_name}::{next_version:04d}",
        text=f"Enabled the '{tool_name}' tool via user consent.",
        metadata={"tool": tool_name, "confidence": f"{_CONSENT_CONFIDENCE}"},
        created_at=now,
        provenance=ChunkProvenance(
            source=WriteSource.PERSONA_SELF,
            logical_id=logical_id,
            written_at=now,
            written_by=written_by,
            reason=reason,
        ),
    )
    store.write(
        persona_id,
        [chunk],
        source=WriteSource.PERSONA_SELF,
        written_by=written_by,
        reason=reason,
        force=True,
    )
    return True
