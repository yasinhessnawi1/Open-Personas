"""API adapters for the pipeline's seams (Spec A5, T7; A5-D-X-layer-split).

The runtime :class:`~persona_runtime.initiative.pipeline.InitiativePipeline`
is DB-free (Protocols in); these adapters bind it to the real stores. Every
adapter is a thin, owner-scoped translation — the policy lives in runtime/core,
the SQL lives in the T3 stores and the graph/users reads.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.schedules.quiet_hours import QuietHours
from persona.timezone import resolve_timezone
from sqlalchemy import select, text

from persona_api.db.engine import rls_connection
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import messages as messages_t
from persona_api.db.models import users as users_t
from persona_api.initiative.store import InitiativeLedger, NoticeDisposition
from persona_api.services import audit_service

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from persona.graph.protocol import GraphStore
    from persona.initiative import InitiativeCandidate
    from persona.initiative.restraint import CadenceCounts
    from sqlalchemy import Engine

    from persona_api.initiative.store import NoticeRecord
    from persona_api.tasks.store import CheckpointStore, TaskStore

__all__ = [
    "ApiGroundingSource",
    "ApiPipelineAuditor",
    "ApiProvenanceReader",
    "ApiUserContextReader",
    "ApiWellbeingSubjectCheck",
    "LedgerAdapter",
    "ResolvedUserContext",
]


class ApiWellbeingSubjectCheck:
    """The subject-exclusion read over the graph store (all five categories)."""

    def __init__(self, store: GraphStore) -> None:
        self._store = store

    def tagged_refs(self, owner_id: str, node_refs: Sequence[str]) -> set[str]:
        """Cited node refs whose CURRENT node carries any wellbeing category."""
        wanted = set(node_refs)
        return {n.id for n in self._store.flagged_nodes(owner_id) if n.id in wanted}


class LedgerAdapter:
    """The runtime ``NoticeLedger`` protocol over T3's :class:`InitiativeLedger`."""

    def __init__(self, ledger: InitiativeLedger) -> None:
        self._ledger = ledger

    def try_claim_held(
        self,
        candidate: InitiativeCandidate,
        *,
        voicer_persona_id: str,
        held_until: datetime | None,
        now: datetime,
    ) -> NoticeRecord | None:
        return self._ledger.try_claim(
            candidate,
            voicer_persona_id=voicer_persona_id,
            disposition=NoticeDisposition.HELD,
            envelope_action=None,
            held_until=held_until,
            now=now,
        )

    def mark_delivered(
        self, owner_id: str, notice_id: str, *, envelope_action: str, now: datetime
    ) -> bool:
        return self._ledger.mark_delivered(
            owner_id, notice_id, envelope_action=envelope_action, now=now
        )

    def expire_stale(self, owner_id: str, notice_id: str, *, reason: str, now: datetime) -> bool:
        return self._ledger.expire_stale(owner_id, notice_id, reason=reason, now=now)

    def held_for_owner(self, owner_id: str) -> Sequence[NoticeRecord]:
        return self._ledger.held_for_owner(owner_id)

    def delivered_counts(self, owner_id: str, *, persona_id: str, now: datetime) -> CadenceCounts:
        return self._ledger.delivered_counts(owner_id, persona_id=persona_id, now=now)


class ResolvedUserContext:
    """The user's resolved tz + quiet window (A8's shared definitions)."""

    def __init__(self, timezone: str, quiet_hours: QuietHours | None) -> None:
        self._timezone = timezone
        self._quiet_hours = quiet_hours

    @property
    def timezone(self) -> str:
        return self._timezone

    @property
    def quiet_hours(self) -> QuietHours | None:
        return self._quiet_hours


class ApiUserContextReader:
    """Reads ``users.timezone`` + the quiet-hours pair (off-until-set, A8-D-6)."""

    def __init__(self, engine: Engine, *, default_timezone: str) -> None:
        self._engine = engine
        self._default_timezone = default_timezone

    def user_context(self, owner_id: str) -> ResolvedUserContext:
        with rls_connection(self._engine, owner_id) as conn:
            row = conn.execute(
                select(
                    users_t.c.timezone,
                    users_t.c.quiet_hours_start,
                    users_t.c.quiet_hours_end,
                ).where(users_t.c.id == owner_id)
            ).first()
        stored_tz = row[0] if row is not None else None
        timezone = resolve_timezone(stored_tz, default=self._default_timezone)
        quiet: QuietHours | None = None
        if row is not None and row[1] is not None and row[2] is not None and row[1] != row[2]:
            quiet = QuietHours(start_minute=int(row[1]), end_minute=int(row[2]))
        return ResolvedUserContext(timezone, quiet)


class ApiProvenanceReader:
    """The A5-D-6 arbitration signals over the graph + conversations reads."""

    _ACTIVITY_LIMIT = 200  # recent conversations sampled for the tie-break

    def __init__(self, store: GraphStore, engine: Engine) -> None:
        self._store = store
        self._engine = engine

    def citation_personas(self, owner_id: str, node_refs: Sequence[str]) -> list[str]:
        """One entry per provenance contribution carrying a persona_id."""
        personas: list[str] = []
        for ref in node_refs:
            node = self._store.get_node(owner_id, ref)
            if node is None:
                continue
            personas.extend(p.persona_id for p in node.provenance if p.persona_id)
        return personas

    def activity_rank(self, owner_id: str) -> Mapping[str, int]:
        """Persona → recent-conversation count (the most-active tie-break)."""
        stmt = (
            select(conversations_t.c.persona_id)
            .order_by(conversations_t.c.updated_at.desc())
            .limit(self._ACTIVITY_LIMIT)
        )
        with rls_connection(self._engine, owner_id) as conn:
            rows = conn.execute(stmt).all()
        rank: dict[str, int] = {}
        for (persona_id,) in rows:
            rank[persona_id] = rank.get(persona_id, 0) + 1
        return rank


class ApiPipelineAuditor:
    """No-silent-skips: every pre-claim discard lands one ``audit_log`` row."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, owner_id: str, event: str, opportunity_key: str, detail: str) -> None:
        audit_service.record(
            engine=self._engine,
            user_id=owner_id,
            action=event,
            target=opportunity_key,
            metadata={"detail": detail},
        )


class ApiGroundingSource:
    """The T4 resolution surface over the real stores (A5-D-2's mechanics).

    ``None`` is definitive-unresolvable at every method:

    - a node that is absent OR MERGED (``merged_into`` set — K7 moved on; the
      canonical merged-exclusion applied to citation resolution). ``get_node``
      hydrates merged rows too, so the read here filters in SQL (the api's
      raw-read precedent, V13's ``_load_persona`` shape) — content only, no
      model hydration needed for a resolution check;
    - an unknown conversation, or one with no stored MESSAGES — per A5-D-2 a
      conversation citation resolves against stored message content, never the
      compacted summary (D-K2-5 re-applied: summaries can compress inference);
    - an unknown task (goal + latest conclusions are the citable content).
    """

    _MESSAGE_WINDOW = 50  # the citable tail; bounded like every read here

    def __init__(self, engine: Engine, tasks: TaskStore, checkpoints: CheckpointStore) -> None:
        self._engine = engine
        self._tasks = tasks
        self._checkpoints = checkpoints

    def node_content(self, owner_id: str, node_id: str) -> str | None:
        with rls_connection(self._engine, owner_id) as conn:
            row = conn.execute(
                text(
                    "SELECT content FROM graph_nodes "
                    "WHERE id = :i AND owner_id = :o AND merged_into IS NULL"
                ),
                {"i": node_id, "o": owner_id},
            ).first()
        return None if row is None else str(row[0])

    def conversation_content(self, owner_id: str, conversation_id: str) -> str | None:
        with rls_connection(self._engine, owner_id) as conn:
            rows = conn.execute(
                select(messages_t.c.content)
                .where(messages_t.c.conversation_id == conversation_id)
                .order_by(messages_t.c.created_at.desc())
                .limit(self._MESSAGE_WINDOW)
            ).all()
        if not rows:
            return None
        return "\n".join(str(r[0]) for r in reversed(rows))

    def task_content(self, owner_id: str, task_id: str) -> str | None:
        from persona.errors import TaskNotFoundError

        try:
            task = self._tasks.get(owner_id, task_id)
        except TaskNotFoundError:
            return None
        parts = [f"Task goal: {task.contract.goal} (status: {task.state.value})"]
        checkpoint = self._checkpoints.get_latest(owner_id, task_id)
        if checkpoint is not None and checkpoint.progress_conclusions:
            parts.extend(checkpoint.progress_conclusions)
        return "\n".join(parts)
