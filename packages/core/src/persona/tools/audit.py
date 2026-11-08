"""Tool audit log shape and Protocol (D-03-25).

A separate audit port from spec-01's :mod:`persona.audit`. Tool events
have different semantics from store mutations (no ``store`` field, no
fixed ``action`` enum that matches mutation kinds) and forcing them
through the same :class:`AuditEvent` would lie about the schema.

In v0.1, two events emit:
- ``action="write"`` from ``file_write`` (per D-03-21 — security-relevant).
- ``action="connect"`` / ``"disconnect"`` / ``"server_unavailable"`` from
  the MCP client lifecycle (T11; D-03-21).

Read-only operations (``file_read``, ``web_search``, ``web_fetch``, per-call
MCP dispatch) do NOT emit — that's the runtime's logging concern, not a
trust-relevant audit event.
"""

from __future__ import annotations

import threading
from datetime import datetime  # noqa: TC003 — Pydantic needs runtime ref
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "JSONLToolAuditLogger",
    "MemoryToolAuditLogger",
    "ToolAuditEvent",
    "ToolAuditLogger",
]


ToolAuditAction = Literal["write", "connect", "disconnect", "execute", "server_unavailable"]
# ``execute`` added per D-12-8 (spec 12 code-execution audit). The decision
# specified additive extension at T03; the Literal was missed at the time
# and the call sites at ``persona/sandbox/tool.py:329,369`` already pass
# ``action="execute"`` — surfaced as mypy errors during T09c verification.


class ToolAuditEvent(BaseModel):
    """One row in the tool audit log.

    Attributes:
        timestamp: UTC-aware datetime at which the action completed.
        persona_id: The persona that owns this tool call. ``None`` for CLI
            development sessions where no persona is loaded.
        tool_name: The dispatched tool's name (e.g., ``file_write``,
            ``mcp:husleietvistutvalget:search_cases``).
        action: One of ``write`` / ``connect`` / ``disconnect`` /
            ``server_unavailable`` (D-03-21).
        resource: The mutated resource — a sandbox-relative path for
            ``write``, a server name or URL for the MCP lifecycle events.
        is_error: True if the action failed.
        metadata: Stringified extras (byte count, transport name, error class).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime
    persona_id: str | None = None
    tool_name: str
    action: ToolAuditAction
    resource: str
    is_error: bool = False
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("timestamp", mode="after")
    @classmethod
    def _timestamp_must_be_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "naive datetime not allowed on ToolAuditEvent.timestamp"
            raise ValueError(msg)
        return value


@runtime_checkable
class ToolAuditLogger(Protocol):
    """Tool audit-log port. Two implementations ship in spec 03.

    Production wiring uses :class:`JSONLToolAuditLogger`; tests use
    :class:`MemoryToolAuditLogger`.
    """

    def emit(self, event: ToolAuditEvent) -> None:
        """Record an event durably. Must be safe to call from multiple threads."""
        ...


class JSONLToolAuditLogger:
    """JSONL-backed tool audit logger.

    Writes one line per event to a per-persona file at
    ``<root>/<persona_id>.tools.jsonl`` (or ``<root>/_cli.tools.jsonl`` for
    events with ``persona_id is None``). Append-only; the Postgres backend
    in a later spec will replace this for the hosted service.

    Thread safety: a :class:`threading.Lock` serializes emits within one
    process. **Single-process only** — two processes writing to the same
    JSONL file may interleave partial writes at the OS level. v0.1 ships as
    a single-process CLI; the hosted path (spec 08) replaces this logger
    with Postgres-backed writes that are multi-process safe.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()

    def _path(self, persona_id: str | None) -> Path:
        if not persona_id:
            return self._root / "_cli.tools.jsonl"
        # One name, or the log lands outside its folder (see ``JSONLAuditLogger._path``).
        from persona.tools._sandbox import require_path_segment  # noqa: PLC0415

        return self._root / f"{require_path_segment(persona_id, what='persona id')}.tools.jsonl"

    def emit(self, event: ToolAuditEvent) -> None:
        path = self._path(event.persona_id)
        with self._lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            line = event.model_dump_json()
            with path.open("a", encoding="utf-8") as f:
                f.write(line + "\n")


class MemoryToolAuditLogger:
    """In-memory tool audit logger for tests.

    Events accumulate in :attr:`events`. Caller-readable list, in emit order.
    """

    def __init__(self) -> None:
        self.events: list[ToolAuditEvent] = []
        self._lock = threading.Lock()

    def emit(self, event: ToolAuditEvent) -> None:
        with self._lock:
            self.events.append(event)
