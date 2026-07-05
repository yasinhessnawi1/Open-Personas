"""Append-only audit log of every store mutation.

Separate from the app log (``persona.logging``). The app log is for humans
debugging; the audit log is the durable record of *who did what, when, why*.
Every store mutation (``write``, ``delete``, ``remove_documents``,
``rollback``) emits exactly one :class:`AuditEvent` via an injected
:class:`AuditLogger`. Per D-01-6 the default backend is
:class:`JSONLAuditLogger`, one file per persona at
``<audit_root>/<persona_id>.jsonl``.

The Postgres-backed audit log in spec 07 swaps in behind this protocol.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime  # noqa: TC003 — Pydantic needs runtime access
from enum import StrEnum
from pathlib import Path  # noqa: TC003 — used at runtime by JSONLAuditLogger
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from persona.errors import AuditWriteError
from persona.schema.chunks import WriteSource  # noqa: TC001 — Pydantic needs runtime access

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "AuditAction",
    "AuditEvent",
    "AuditLogger",
    "JSONLAuditLogger",
    "MemoryAuditLogger",
    "StoreKind",
]

# The four typed stores (Spec 01) + the user-scoped knowledge graph (Spec K0 T8 —
# additive: every graph mutation emits exactly one AuditEvent through this same
# port; existing stores are unaffected) + the skill-injection event sentinel
# (Spec S1 T4 — skill injection is not a store mutation, so ``"skill"`` is the
# honest non-store ``store`` value, additive like ``knowledge_graph``).
StoreKind = Literal[
    "identity", "self_facts", "worldview", "episodic", "episodic_gist", "knowledge_graph", "skill"
]


class AuditAction(StrEnum):
    """The mutating actions a store may perform, plus the Spec S1 skill events.

    A rejected store mutation does NOT produce an audit event — rejections are
    surfaced via ``persona.logging`` instead. Skill events (Spec S1, S1-D-7) are
    the exception: a skill *injection* is audited, and a consent *refusal* is
    audited too, because a blocked injection of an untrusted skill is itself a
    security signal worth the durable trail.
    """

    WRITE = "write"
    DELETE = "delete"
    REMOVE_DOCUMENTS = "remove_documents"
    ROLLBACK = "rollback"
    # Spec K8 (K8-D-5): one event per batched reinforcement (the per-turn
    # strength++/clock-reset over the recalled episodic id-set).
    REINFORCE = "reinforce"
    # Spec S1 (S1-D-7): one event per skill injection; one per consent refusal.
    SKILL_INJECTED = "skill_injected"
    SKILL_REFUSED = "skill_refused"


class AuditEvent(BaseModel):
    """One row in the audit log.

    Attributes:
        timestamp: UTC-aware datetime at which the mutation succeeded.
        persona_id: The persona affected.
        action: Which mutation kind.
        store: Which store kind.
        source: Which of the three update sources produced the write
            (``system``/``user``/``persona_self``). For non-write actions
            (delete, rollback), this records who initiated the action.
        written_by: Free-form actor identifier (user id, ``"system"``,
            ``"frontier:claude-sonnet-4-6"``).
        reason: Short free-text rationale. Required for persona_self writes
            (enforced by the store, not here).
        chunk_ids: IDs of the chunks touched by this action. For deletes
            and remove_documents, the ids removed. For writes and rollbacks,
            the new chunk ids appended.
        logical_ids: Logical ids touched by this action. Often empty for
            delete operations.
        metadata: Action-specific extras (e.g., ``{"to_version": "3"}`` on
            rollback). Always stringified for JSONL round-trip.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    timestamp: datetime
    persona_id: str
    action: AuditAction
    store: StoreKind
    source: WriteSource
    written_by: str | None = None
    reason: str | None = None
    chunk_ids: list[str] = Field(default_factory=list)
    logical_ids: list[str] = Field(default_factory=list)
    metadata: dict[str, str] = Field(default_factory=dict)

    @field_validator("timestamp", mode="after")
    @classmethod
    def _timestamp_must_be_tz_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            msg = "naive datetime not allowed on AuditEvent.timestamp"
            raise ValueError(msg)
        return value


@runtime_checkable
class AuditLogger(Protocol):
    """The audit-log port.

    Two implementations ship in spec 01: :class:`JSONLAuditLogger` (the
    production default) and :class:`MemoryAuditLogger` (test fixtures).
    Spec 07 will add a Postgres-backed implementation.
    """

    def emit(self, event: AuditEvent) -> None:
        """Record an event durably. Must be safe to call from multiple threads."""
        ...

    def read(
        self,
        persona_id: str,
        *,
        since: datetime | None = None,
        action: AuditAction | None = None,
        source: WriteSource | None = None,
        store: StoreKind | None = None,
    ) -> list[AuditEvent]:
        """Return events for ``persona_id``, filtered by the optional criteria.

        Filters AND together. Missing persona log → empty list, not an error.
        """
        ...


class JSONLAuditLogger:
    """Default audit logger: one JSONL file per persona.

    Append-only on every emit. ``read`` streams the file and filters in
    memory — fine at persona scale (tens of writes per day). No rotation
    in v0.1; the Postgres backend (spec 07) replaces this for the hosted
    service.

    Args:
        root: Directory containing per-persona JSONL files. Created on first
            write. Per D-01-6 the conventional location is
            ``<PERSONA_CHROMA_PATH>/audit/``.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._lock = threading.Lock()

    def _path(self, persona_id: str) -> Path:
        return self._root / f"{persona_id}.jsonl"

    def emit(self, event: AuditEvent) -> None:
        """Append ``event`` to its persona's log file.

        Concurrent calls are serialised behind a process-local lock; the
        write itself is one ``write()`` call per line, which POSIX makes
        atomic for short payloads. Multi-process safety lands with the
        Postgres backend (spec 07).
        """
        try:
            self._root.mkdir(parents=True, exist_ok=True)
            payload = event.model_dump_json()
            with self._lock, self._path(event.persona_id).open("a", encoding="utf-8") as f:
                f.write(payload)
                f.write("\n")
        except OSError as exc:
            raise AuditWriteError(
                "failed to append audit event",
                context={"persona_id": event.persona_id, "reason": str(exc)[:120]},
            ) from exc

    def read(
        self,
        persona_id: str,
        *,
        since: datetime | None = None,
        action: AuditAction | None = None,
        source: WriteSource | None = None,
        store: StoreKind | None = None,
    ) -> list[AuditEvent]:
        path = self._path(persona_id)
        if not path.exists():
            return []
        events: list[AuditEvent] = []
        # We deliberately do NOT hold the lock during a long read; the lock
        # guards short appends. A concurrent append after we start reading
        # may be missed by *this* read but does not corrupt earlier records
        # (JSONL is line-oriented and appends are atomic for our payload sizes).
        with path.open("r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = AuditEvent.model_validate_json(line)
                except ValidationError:
                    # Corrupt line: skip rather than crash. Logging an
                    # operational warning would create a circular dep with
                    # `persona.logging`; the JSONL file itself is the
                    # forensic record. The line number lets a human find it.
                    _ = line_no
                    continue
                events.append(event)
        return _filter_events(events, since=since, action=action, source=source, store=store)


class MemoryAuditLogger:
    """In-memory audit logger for tests.

    Holds events in a list; the test fixture
    ``packages/core/tests/conftest.py::memory_audit_logger`` exposes it.
    """

    def __init__(self) -> None:
        self._events: list[AuditEvent] = []
        self._lock = threading.Lock()

    @property
    def events(self) -> list[AuditEvent]:
        """A snapshot copy of the recorded events."""
        with self._lock:
            return list(self._events)

    def emit(self, event: AuditEvent) -> None:
        with self._lock:
            self._events.append(event)

    def read(
        self,
        persona_id: str,
        *,
        since: datetime | None = None,
        action: AuditAction | None = None,
        source: WriteSource | None = None,
        store: StoreKind | None = None,
    ) -> list[AuditEvent]:
        with self._lock:
            scoped = [e for e in self._events if e.persona_id == persona_id]
        return _filter_events(scoped, since=since, action=action, source=source, store=store)


def _filter_events(
    events: Iterable[AuditEvent],
    *,
    since: datetime | None,
    action: AuditAction | None,
    source: WriteSource | None,
    store: StoreKind | None,
) -> list[AuditEvent]:
    """Shared filter logic for the two AuditLogger implementations."""
    if since is not None and since.tzinfo is None:
        msg = "since must be tz-aware"
        raise ValueError(msg)
    return [
        e
        for e in events
        if (since is None or e.timestamp >= since)
        and (action is None or e.action == action)
        and (source is None or e.source == source)
        and (store is None or e.store == store)
    ]


# `json` is imported for downstream readers that want to parse the JSONL
# format manually; we don't currently use it directly in this module but
# keeping the import documents the format intent.
_ = json
