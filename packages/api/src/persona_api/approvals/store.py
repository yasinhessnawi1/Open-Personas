"""The durable approval store — RLS-scoped, audited, at-most-once (Spec A3, T6).

:class:`ApprovalStore` owns the ``approval_proposals`` + ``approval_decisions`` rows: it
records the gated proposal, runs the **atomic status CAS** that makes approval execution
at-most-once, appends the verbatim decisions (the audit trail), and reads the one pending
proposal a reply resolves. Like :class:`persona_api.tasks.TaskStore` it runs **owner-scoped
through RLS** (every operation re-binds ``app.current_user_id`` via ``rls_connection``), so a
cross-tenant reach hits zero rows.

Discipline held here (mirrors ``TaskStore``):

* **CQS** — reads only read; mutators write and return the persisted entity as confirmation.
* **One ``AuditEvent`` per mutation** — exactly one ``audit_log`` row per create / transition /
  decision (best-effort, never breaks the op).
* **The status CAS is the at-most-once enforcement (A3-D-X-approved-execution).**
  :meth:`transition_proposal` is a single ``UPDATE ... WHERE id=:id AND status=:expected
  RETURNING`` — the DB serialises two concurrent transitions on one proposal (a racing
  double-approve, or approve-vs-expire) so exactly one wins; the loser gets ``None``. This is
  the same effectively-once shape as ``CheckpointStore.append``'s head CAS — a read-check-then-
  update would reopen the double-execution hole A3 exists to close.
* **One-pending-aware create.** A second ``pending`` proposal on a task trips the partial-unique
  index; the store surfaces that as the *existing* pending proposal (idempotent — the task's
  single referent), never a raw ``IntegrityError`` (criterion 4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from persona.approvals import ActionProposal, ApprovalDecision, ProposalStatus
from persona.errors import ApprovalNotFoundError
from persona.logging import get_logger
from sqlalchemy import insert, select, update
from sqlalchemy.exc import IntegrityError

from persona_api.approvals.serde import (
    decision_values,
    proposal_values,
    row_to_decision,
    row_to_proposal,
)
from persona_api.db.engine import rls_connection
from persona_api.db.models import approval_decisions as decisions_t
from persona_api.db.models import approval_proposals as proposals_t
from persona_api.db.models import tasks as tasks_t
from persona_api.services import audit_service

if TYPE_CHECKING:
    from datetime import datetime

    from sqlalchemy import Engine

__all__ = ["ApprovalStore"]

_log = get_logger("api.approvals.store")

#: The partial-unique index that enforces one pending proposal per task (criterion 4).
_ONE_PENDING_CONSTRAINT = "uq_one_pending_proposal_per_task"


class ApprovalStore:
    """Owner-scoped, audited store over ``approval_proposals`` + ``approval_decisions``.

    Construct with the ``persona_app`` RLS engine — every operation re-binds the owner's GUC,
    so the store can never reach another tenant's rows.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    # --- reads (CQS: no writes) --------------------------------------------

    def get_proposal(self, owner_id: str, proposal_id: str) -> ActionProposal:
        """Fetch one proposal. Raises :class:`ApprovalNotFoundError` on a miss (no oracle)."""
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(select(proposals_t).where(proposals_t.c.id == proposal_id))
                .mappings()
                .first()
            )
        if row is None:
            raise ApprovalNotFoundError("proposal not found", context={"proposal_id": proposal_id})
        return row_to_proposal(row)

    def get_pending_for_task(self, owner_id: str, task_id: str) -> ActionProposal | None:
        """The task's single pending proposal (the one referent a reply resolves), or ``None``."""
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(
                    select(proposals_t).where(
                        proposals_t.c.task_id == task_id,
                        proposals_t.c.status == ProposalStatus.PENDING.value,
                    )
                )
                .mappings()
                .first()
            )
        return row_to_proposal(row) if row is not None else None

    def list_pending_for_owner(self, owner_id: str) -> list[ActionProposal]:
        """Every pending proposal across the owner's tasks (the A6 inbox), oldest first.

        RLS-scoped to the owner. Ordered by ``created_at`` so the longest-waiting (nearest to the
        expiry sweep) sits first — the inbox's triage order.
        """
        with rls_connection(self._engine, owner_id) as conn:
            rows = (
                conn.execute(
                    select(proposals_t)
                    .where(proposals_t.c.status == ProposalStatus.PENDING.value)
                    .order_by(proposals_t.c.created_at.asc())
                )
                .mappings()
                .all()
            )
        return [row_to_proposal(r) for r in rows]

    def get_pending_for_conversation(
        self, owner_id: str, conversation_id: str
    ) -> ActionProposal | None:
        """The pending proposal for a conversation's waiting-on-user task (the A6 chat-twin reader).

        Joins ``approval_proposals`` → ``tasks`` on ``task_id`` and scopes to the task tied to
        ``conversation_id`` that is parked ``waiting(on_user)`` with a pending proposal (the shape a
        gated leg leaves). RLS-scoped through both tables. No schema invariant forbids >1 task per
        conversation, so the most recent proposal wins — the one the user is most plausibly replying
        to (``state='waiting'`` / ``wait_kind='on_user'`` mirror ``TaskState.WAITING`` /
        ``WaitKind.ON_USER``).
        """
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(
                    select(proposals_t)
                    .select_from(
                        proposals_t.join(tasks_t, proposals_t.c.task_id == tasks_t.c.id)
                    )
                    .where(
                        tasks_t.c.conversation_id == conversation_id,
                        proposals_t.c.status == ProposalStatus.PENDING.value,
                        tasks_t.c.state == "waiting",
                        tasks_t.c.wait_kind == "on_user",
                    )
                    .order_by(proposals_t.c.created_at.desc())
                )
                .mappings()
                .first()
            )
        return row_to_proposal(row) if row is not None else None

    def list_decisions(self, owner_id: str, proposal_id: str) -> list[ApprovalDecision]:
        """The append-only decisions for a proposal, oldest first (the audit trail)."""
        with rls_connection(self._engine, owner_id) as conn:
            rows = (
                conn.execute(
                    select(decisions_t)
                    .where(decisions_t.c.proposal_id == proposal_id)
                    .order_by(decisions_t.c.decided_at.asc())
                )
                .mappings()
                .all()
            )
        return [row_to_decision(r) for r in rows]

    # --- mutations (CQS: return the persisted entity as confirmation) -------

    def create_proposal(self, proposal: ActionProposal) -> ActionProposal:
        """Record a gated proposal; idempotent against the one-pending index (criterion 4).

        On a one-pending conflict (a second open proposal on the task — a re-delivered gated
        leg) the existing pending proposal is returned as the task's single referent, never a
        raw ``IntegrityError``. Any other integrity error (a bad FK) propagates.
        """
        try:
            with rls_connection(self._engine, proposal.owner_id) as conn:
                conn.execute(insert(proposals_t).values(**proposal_values(proposal)))
        except IntegrityError as exc:
            if self._is_one_pending_conflict(exc):
                existing = self.get_pending_for_task(proposal.owner_id, proposal.task_id)
                if existing is not None:
                    _log.info(
                        "proposal create no-op (one pending per task)",
                        task_id=proposal.task_id,
                        existing_id=existing.proposal_id,
                    )
                    return existing
            raise
        self._audit(
            proposal.owner_id,
            "approval.proposal.create",
            proposal.proposal_id,
            {"task_id": proposal.task_id, "status": proposal.status.value},
        )
        return proposal

    def transition_proposal(
        self,
        owner_id: str,
        proposal_id: str,
        *,
        expected: ProposalStatus,
        new: ProposalStatus,
        now: datetime,
    ) -> ActionProposal | None:
        """Atomic status CAS (A3-D-X-approved-execution): the at-most-once enforcement point.

        ``UPDATE ... SET status=:new WHERE id=:id AND status=:expected RETURNING`` — exactly one
        of two concurrent transitions from ``expected`` wins (the DB serialises the row); the
        loser returns ``None``. Only the transition that wins should run the side effect
        (e.g. replay the approved payload), so a racing double-approve cannot double-execute.

        Returns the post-transition :class:`ActionProposal`, or ``None`` if no row was in the
        ``expected`` state (already transitioned / lost the race).
        """
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(
                    update(proposals_t)
                    .where(
                        proposals_t.c.id == proposal_id,
                        proposals_t.c.status == expected.value,
                    )
                    .values(status=new.value, updated_at=now)
                    .returning(*proposals_t.c)
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        proposal = row_to_proposal(row)
        self._audit(
            owner_id,
            f"approval.proposal.{new.value}",
            proposal_id,
            {"from": expected.value, "to": new.value},
        )
        return proposal

    def revise_pending_payload(
        self,
        owner_id: str,
        proposal_id: str,
        *,
        arguments: dict[str, Any],
        description: str,
        now: datetime,
    ) -> ActionProposal | None:
        """Update a *pending* proposal's payload in place (the material-modify re-confirm path).

        A material edit revises the recorded payload + description and keeps the proposal
        ``pending`` (the single open state) for re-confirmation — it never opens a second
        proposal. Returns the revised proposal, or ``None`` if it was no longer pending.
        """
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(
                    update(proposals_t)
                    .where(
                        proposals_t.c.id == proposal_id,
                        proposals_t.c.status == ProposalStatus.PENDING.value,
                    )
                    .values(arguments_json=arguments, description=description, updated_at=now)
                    .returning(*proposals_t.c)
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        proposal = row_to_proposal(row)
        self._audit(
            owner_id, "approval.proposal.revised", proposal_id, {"task_id": proposal.task_id}
        )
        return proposal

    def mark_reminded(
        self, owner_id: str, proposal_id: str, *, now: datetime
    ) -> ActionProposal | None:
        """CAS the remind-once marker (A3-D-2, T9): fire a reminder exactly once.

        ``UPDATE ... SET reminded_at=:now WHERE id=:id AND status='pending' AND reminded_at IS
        NULL RETURNING`` — the ``reminded_at IS NULL`` guard makes a double-sweep a no-op (only
        the first sweep wins). Status stays ``pending`` (the one-pending index is undisturbed).

        Returns the proposal if this sweep claimed the reminder, else ``None`` (already
        reminded / no longer pending).
        """
        with rls_connection(self._engine, owner_id) as conn:
            row = (
                conn.execute(
                    update(proposals_t)
                    .where(
                        proposals_t.c.id == proposal_id,
                        proposals_t.c.status == ProposalStatus.PENDING.value,
                        proposals_t.c.reminded_at.is_(None),
                    )
                    .values(reminded_at=now, updated_at=now)
                    .returning(*proposals_t.c)
                )
                .mappings()
                .first()
            )
        if row is None:
            return None
        proposal = row_to_proposal(row)
        self._audit(
            owner_id, "approval.proposal.reminded", proposal_id, {"task_id": proposal.task_id}
        )
        return proposal

    def record_decision(self, owner_id: str, decision: ApprovalDecision) -> ApprovalDecision:
        """Append a decision (verbatim reply + channel) — the audit trail (criterion 9).

        Append-only: a clarify-then-approve keeps both rows. The composite FK pins this row's
        ``owner_id`` to its proposal's owner (a cross-owner decision is impossible).
        """
        with rls_connection(self._engine, owner_id) as conn:
            conn.execute(insert(decisions_t).values(**decision_values(decision, owner_id)))
        self._audit(
            owner_id,
            f"approval.decision.{decision.type.value}",
            decision.proposal_id,
            {"channel": decision.channel},
        )
        return decision

    # --- internals ----------------------------------------------------------

    @staticmethod
    def _is_one_pending_conflict(exc: IntegrityError) -> bool:
        """True iff the integrity error is the one-pending partial-unique index violation."""
        constraint = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        return constraint == _ONE_PENDING_CONSTRAINT

    def _audit(self, owner_id: str, action: str, target: str, metadata: dict[str, str]) -> None:
        audit_service.record(
            engine=self._engine,
            user_id=owner_id,
            action=action,
            target=target,
            metadata=metadata,
        )
