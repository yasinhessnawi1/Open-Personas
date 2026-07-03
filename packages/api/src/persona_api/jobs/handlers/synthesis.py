"""The synthesis job handler — A0's second durable tenant (Spec K2, T8b).

Runs the off-critical-path reflection pass as a durable, idempotent A0 job: window
the interaction (K2-D-5) → run the runtime ``Synthesizer`` → meter the model spend
(Spec-08 visibility) → advance the ``synthesis_markers`` high-water-mark in the same
owner-scoped transaction.

**Idempotency — the high-water-mark marker (declared at registration).** The
``synthesised_up_to`` column is the durable marker: synthesis processes only the
tail past it and advances it monotonically. A re-delivery (retry / lease-expiry
reclaim) re-reads the marker, finds nothing new, and no-ops — the second line
behind A0's ``ON CONFLICT (owner_id, idempotency_key)`` dedup (the key carries the
enqueue-time message count, so a continued conversation re-keys to a new job).

The concrete ``Synthesizer`` (extractor on the small/mid tier, entity registry,
graph store) is composed at the worker root (T8c); this handler depends only on the
``SynthesisRunner`` + ``SynthesisRepository`` ports, so it is unit-testable with fakes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.extraction import InteractionKind
from persona.jobs import (
    MEDIUM_LEASE,
    SYNTHESIS_JOB_TYPE,
    JobTypeSpec,
    RetryPolicy,
    SynthesisJobPayload,
    synthesis_idempotency_key,
)
from persona.jobs.synthesis import CHANNEL_CHAT
from persona.logging import get_logger
from persona_runtime.extraction.windowing import build_window
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from persona_api.db.models import conversations, messages, synthesis_markers

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.extraction import ExtractionInput
    from persona.graph.protocol import MergeOutcome
    from persona.jobs import JobContext, JobRegistry
    from sqlalchemy import Connection

    from persona_api.jobs.queue import JobQueue

# ``SYNTHESIS_JOB_TYPE`` / ``SynthesisJobPayload`` / ``synthesis_idempotency_key`` are
# re-exported from ``persona.jobs`` (V13 D-4-amended: the ONE canonical synthesis
# contract lives in core so the api + voice writers cannot drift). Kept in ``__all__``
# so existing ``from persona_api.jobs.handlers.synthesis import ...`` imports still work.
__all__ = [
    "SYNTHESIS_JOB_TYPE",
    "InteractionData",
    "PgSynthesisRepository",
    "SynthesisHandler",
    "SynthesisJobPayload",
    "SynthesisRepository",
    "SynthesisRunner",
    "enqueue_synthesis",
    "register_synthesis_handler",
    "synthesis_idempotency_key",
]

_logger = get_logger("jobs.synthesis")

# Roles whose text forms the synthesis transcript (the grounding source).
_TRANSCRIPT_ROLES = ("user", "assistant")


class InteractionData(BaseModel):
    """What the repository reads for one interaction (marker + transcript + summary)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    synthesised_up_to: int
    messages: tuple[tuple[str, str], ...]
    compacted_summary: str


@runtime_checkable
class SynthesisRepository(Protocol):
    """The DB port the handler uses (owner-scoped via the job's connection)."""

    def read(
        self, conn: Connection, *, owner_id: str, payload: SynthesisJobPayload
    ) -> InteractionData | None:
        """Read the marker + transcript + summary, or ``None`` if the interaction is gone."""
        ...

    def advance(
        self,
        conn: Connection,
        *,
        owner_id: str,
        payload: SynthesisJobPayload,
        high_water_mark: int,
    ) -> None:
        """Advance the synthesis marker (monotonic compare-and-set)."""
        ...


@runtime_checkable
class SynthesisRunner(Protocol):
    """The runtime synthesis assembly (``persona_runtime.extraction.Synthesizer``)."""

    async def synthesise(
        self, owner_id: str, interaction: ExtractionInput
    ) -> list[MergeOutcome]: ...


class SynthesisHandler:
    """Idempotent synthesis: window → synthesise → meter → advance the marker.

    When the synthesis produced knowledge (the graph was dirtied), it enqueues a
    coalesced ``graph_consolidation`` run at its tail (K7-D-5) — the channel-agnostic
    turn-end/run-end/voice-end trigger carries consolidation for free. The hook is
    optional + off the critical path; ``None`` disables it (the consolidation-off
    config path), keeping synthesis independently testable.
    """

    def __init__(
        self,
        *,
        runner: SynthesisRunner,
        repository: SynthesisRepository,
        enqueue_consolidation: Callable[[str], None] | None = None,
    ) -> None:
        self._runner = runner
        self._repo = repository
        self._enqueue_consolidation = enqueue_consolidation

    async def handle(self, payload: SynthesisJobPayload, context: JobContext) -> None:
        with context.connection() as conn:
            data = self._repo.read(conn, owner_id=context.owner_id, payload=payload)
            if data is None:
                return  # interaction deleted between enqueue and run — nothing to do.
            window = build_window(
                messages=data.messages,
                compacted_summary=data.compacted_summary,
                synthesised_up_to=data.synthesised_up_to,
                interaction_kind=InteractionKind(payload.interaction_kind),
                interaction_id=payload.interaction_id,
                persona_id=payload.persona_id,
                channel=payload.channel,
            )
            if window is None:
                return  # nothing new past the marker — the idempotency no-op.

            outcomes = await self._runner.synthesise(context.owner_id, window.input)

            # Spec-08 cost visibility. amount_micros is attribution-not-deduct here
            # (like avatar's credits_charged=0); precise token-cost metering is a
            # refinement once the extractor surfaces per-call usage.
            context.meter(
                amount_micros=0,
                kind="model",
                detail={
                    "surface": "synthesis",
                    "interaction_id": payload.interaction_id,
                    "candidates": str(len(outcomes)),
                },
            )
            self._repo.advance(
                conn,
                owner_id=context.owner_id,
                payload=payload,
                high_water_mark=window.high_water_mark,
            )

        # Synthesis-tail consolidation trigger (K7-D-5): only when knowledge was
        # written (the graph got dirty). Outside the job transaction — a best-effort
        # enqueue must never fail or roll back the completed synthesis.
        if self._enqueue_consolidation is not None and outcomes:
            try:
                self._enqueue_consolidation(context.owner_id)
            except Exception:  # noqa: BLE001 — the enqueue is best-effort, off the critical path
                _logger.warning("consolidation enqueue failed", owner_id=context.owner_id)


class PgSynthesisRepository:
    """Postgres-backed :class:`SynthesisRepository` (owner-scoped via the job conn).

    v1 supports ``interaction_kind == "conversation"`` (web + voice persist there);
    agentic-run / standalone-voice sources are a documented seam (return ``None``).
    The real SQL is exercised at the T8d integration leg.
    """

    def read(
        self, conn: Connection, *, owner_id: str, payload: SynthesisJobPayload
    ) -> InteractionData | None:
        if payload.interaction_kind != InteractionKind.CONVERSATION.value:
            _logger.debug("synthesis source not yet wired", kind=payload.interaction_kind)
            return None
        convo = conn.execute(
            select(conversations.c.compacted_summary).where(
                conversations.c.id == payload.interaction_id
            )
        ).one_or_none()
        if convo is None:
            return None
        rows = conn.execute(
            select(messages.c.role, messages.c.content)
            .where(messages.c.conversation_id == payload.interaction_id)
            .order_by(messages.c.created_at, messages.c.id)
        ).all()
        transcript = tuple((row.role, row.content) for row in rows if row.role in _TRANSCRIPT_ROLES)
        marker = conn.execute(
            select(synthesis_markers.c.synthesised_up_to).where(
                synthesis_markers.c.owner_id == owner_id,
                synthesis_markers.c.interaction_kind == payload.interaction_kind,
                synthesis_markers.c.interaction_id == payload.interaction_id,
            )
        ).scalar_one_or_none()
        return InteractionData(
            synthesised_up_to=marker or 0,
            messages=transcript,
            compacted_summary=convo.compacted_summary or "",
        )

    def advance(
        self,
        conn: Connection,
        *,
        owner_id: str,
        payload: SynthesisJobPayload,
        high_water_mark: int,
    ) -> None:
        # Monotonic compare-and-set: insert-or-advance, never regress (a concurrent
        # re-delivery that already advanced past this mark is left untouched).
        stmt = (
            pg_insert(synthesis_markers)
            .values(
                owner_id=owner_id,
                interaction_kind=payload.interaction_kind,
                interaction_id=payload.interaction_id,
                synthesised_up_to=high_water_mark,
            )
            .on_conflict_do_update(
                constraint="uq_synthesis_markers_owner_kind_interaction",
                set_={"synthesised_up_to": high_water_mark},
                where=synthesis_markers.c.synthesised_up_to < high_water_mark,
            )
        )
        conn.execute(stmt)


def register_synthesis_handler(
    registry: JobRegistry,
    *,
    runner: SynthesisRunner,
    repository: SynthesisRepository,
    enqueue_consolidation: Callable[[str], None] | None = None,
) -> None:
    """Register the synthesis handler (A0's second tenant) with its declared idempotency.

    ``enqueue_consolidation`` (K7-D-5) is the synthesis-tail trigger the worker root
    wires when consolidation is enabled; ``None`` leaves synthesis unchanged.
    """
    registry.register(
        JobTypeSpec(
            type=SYNTHESIS_JOB_TYPE,
            payload_model=SynthesisJobPayload,
            handler=SynthesisHandler(
                runner=runner,
                repository=repository,
                enqueue_consolidation=enqueue_consolidation,
            ),
            idempotency_key=synthesis_idempotency_key,
            retry=RetryPolicy(max_attempts=3),
            lease=MEDIUM_LEASE,
        )
    )


def enqueue_synthesis(
    queue: JobQueue,
    *,
    owner_id: str,
    interaction_kind: str,
    interaction_id: str,
    persona_id: str,
    message_count: int,
    channel: str = CHANNEL_CHAT,
) -> None:
    """Enqueue a synthesis job at an interaction boundary (turn-end / run-end / voice-end).

    Keyed by the message count so a continued conversation re-keys to a new job; an
    identical re-enqueue is A0's ``ON CONFLICT`` no-op. Off the critical path. This is
    the api-side writer (over ``JobQueue`` — the canonical A0 semantics); the voice
    process has a twin raw-INSERT writer building the SAME core payload (V13 D-4-amended).
    """
    payload = SynthesisJobPayload(
        interaction_kind=interaction_kind,
        interaction_id=interaction_id,
        persona_id=persona_id,
        high_water_mark=message_count,
        channel=channel,
    )
    queue.enqueue(
        type=SYNTHESIS_JOB_TYPE,
        owner_id=owner_id,
        payload=payload.model_dump(),
        idempotency_key=synthesis_idempotency_key(payload),
    )
