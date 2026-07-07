"""The shared, request-scoped approval-resolution service (Spec A6, T-seam).

Completes A3's approval loop: A3 shipped the resolver + stores + floor, but nothing constructed
the resolver in any live path, so a parked proposal was never resolved (only the expiry sweep
transitioned it). This service is the ONE place the real :class:`ApprovalResolver` is assembled —
with the real un-gated executor (verbatim replay), the real Originator-backed C0 notifier, the
real continuation, and the deterministic Lexicon floor interpreter.

**Both** surfaces consume this one service — the approvals inbox (a structured decision) and the
chat reply path (a natural-language reply) — so there is one floor, one CAS, and one durable
record. A concurrent chat+inbox double-resolve therefore has exactly one winner (the proposal
status CAS), the loser reflects "already handled" (idempotency is structural in the resolver:
:meth:`ApprovalResolver.resolve` no-ops on a non-pending proposal). Resolved state is anchored on
the durable A3 decision record, never conversation metadata (A6-D-3, criterion 5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.approvals import LexiconReplyInterpreter
from persona.audit import JSONLAuditLogger
from persona.stores.episodic import EpisodicStore

from persona_api.approvals.resolver import ApprovalResolver, InboxDecision
from persona_api.approvals.store import ApprovalStore
from persona_api.jobs.queue import JobQueue
from persona_api.schedules.store import ScheduleStore
from persona_api.services.origination_adapters import OriginatorApprovalNotifier
from persona_api.tasks.continuation import TaskContinuation
from persona_api.tasks.store import CheckpointStore, TaskStore

if TYPE_CHECKING:
    from collections.abc import Mapping
    from datetime import datetime
    from pathlib import Path

    from persona.audit import AuditLogger
    from persona.stores.backend import Backend
    from pydantic import JsonValue
    from sqlalchemy import Engine

    from persona_api.approvals.resolver import ResolutionOutcome
    from persona_api.config import Edition
    from persona_api.services.runtime_factory import RuntimeFactory

__all__ = ["ApprovalResolutionService"]


class ApprovalResolutionService:
    """Assemble the real :class:`ApprovalResolver` for a proposal and resolve a reply through it."""

    def __init__(
        self,
        *,
        engine: Engine,
        factory: RuntimeFactory,
        edition: Edition,
        memory_backend: Backend,
        audit_root: Path,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self._engine = engine
        self._factory = factory
        self._edition = edition
        # The C0 episodic recorder for the notifier — the same construction as
        # OriginatorFailureNotifier (backend-selected audit when supplied; JSONL fallback).
        self._episodic = EpisodicStore(
            backend=memory_backend, audit_logger=audit_logger or JSONLAuditLogger(audit_root)
        )

    def build_resolver(self, persona_id: str) -> ApprovalResolver:
        """Assemble the resolver for ``persona_id`` with its real production dependencies.

        The executor is lazy (the persona's toolbox builds only on an actual APPROVE), so building
        the resolver for a list/deny path is cheap. RLS scope is the caller's (a request binds the
        owner GUC via middleware); every store here is owner-scoped through that engine.
        """
        engine = self._engine
        tasks = TaskStore(engine)
        checkpoints = CheckpointStore(engine)
        continuation = TaskContinuation(
            task_store=tasks,
            queue=JobQueue(engine),
            checkpoint_store=checkpoints,
            schedule_store=ScheduleStore(engine),
        )
        return ApprovalResolver(
            approvals=ApprovalStore(engine),
            tasks=tasks,
            checkpoints=checkpoints,
            continuation=continuation,
            # v1: the deterministic NO/EN floor interpreter (approve/deny parity; A6 T-seam
            # decision 1). A model-backed ReplyInterpreter for NL modify-with-edits is a named
            # follow-up — the inbox handles modify via structured edited_arguments.
            interpreter=LexiconReplyInterpreter(),
            executor=self._factory.build_action_executor(persona_id),
            notifier=OriginatorApprovalNotifier(
                rls_engine=engine,
                episodic=self._episodic,
                edition=self._edition,
                tasks=tasks,
            ),
        )

    async def resolve(
        self, owner_id: str, proposal_id: str, reply: str, channel: str, *, now: datetime
    ) -> ResolutionOutcome:
        """Resolve a natural-language reply against the proposal (the chat path; idempotent).

        Reads the proposal first (for the executor's ``persona_id``); the resolver then applies the
        floor + CAS. A reply for an already-resolved proposal is a clean no-op
        (``ResolutionOutcome(outcome=None, note="not_pending")``) — the second surface reflects,
        never errors.
        """
        proposal = ApprovalStore(self._engine).get_proposal(owner_id, proposal_id)
        resolver = self.build_resolver(proposal.persona_id)
        return await resolver.resolve(owner_id, proposal_id, reply, channel, now=now)

    async def resolve_structured(
        self,
        owner_id: str,
        proposal_id: str,
        *,
        decision: InboxDecision,
        edited_arguments: Mapping[str, JsonValue] | None = None,
        verbatim_reply: str,
        channel: str,
        now: datetime,
    ) -> ResolutionOutcome:
        """Resolve an explicit inbox decision through the SAME floor/CAS/record as the chat twin.

        The A6 inbox path: a structured approve/deny/modify becomes a deterministic confidence-1.0
        interpretation (no model), so a chat+inbox double-resolve has exactly one winner and the
        loser reflects ``not_pending`` — read from the durable record, never a cached/pushed state.
        """
        proposal = ApprovalStore(self._engine).get_proposal(owner_id, proposal_id)
        resolver = self.build_resolver(proposal.persona_id)
        return await resolver.resolve_structured(
            owner_id,
            proposal_id,
            decision=decision,
            edited_arguments=edited_arguments,
            verbatim_reply=verbatim_reply,
            channel=channel,
            now=now,
        )
