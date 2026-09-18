"""The approval resolver — reply → floor → replay → resolution checkpoint → resume (Spec A3, T8).

The orchestrator that closes the approval loop (A3-D-X-approved-execution). A reply arrives on
some channel; the resolver:

1. **reads** the task's one pending proposal (idempotent — a reply for an already-resolved
   proposal is a no-op);
2. **interprets** the reply through the model :class:`ReplyInterpreter`, then **the floor**
   (:func:`persona.approvals.resolve_reply`) — the reply NEVER bypasses the floor's
   default-to-deny / clarify-once / material-modify-re-confirm bound;
3. **records** the verbatim decision (the audit trail);
4. **resolves**:
   - **approve** (or an immaterial **modify**) → the **at-most-once execution gate** (the
     proposal CAS ``pending → approved``, or ``pending → modified`` when what is about to run
     is the user's edited payload). Only the winner **replays the EXACT recorded payload
     verbatim** (the model never re-derives), folds the result into a **resolution
     checkpoint** (the A2 ``CheckpointStore.append`` CAS), marks the proposal ``consumed``
     (admitted from either green light via :meth:`ProposalStatus.executable`), and resumes
     the task;
   - **deny** → the CAS ``pending → denied`` gates a one-time denial checkpoint + resume (the
     leg adapts gracefully — denial is information, not an error);
   - **material modify** → revise the pending payload + re-confirm (stays pending);
   - **clarify** → ask once more (stays pending).

The two-layer at-most-once (the proposal CAS gating execution + the checkpoint CAS gating the
durable progress) guarantees an approved action runs **exactly once** under a duplicated reply
or a re-delivered resume. Execution, interpretation, and the C0 ask are injected seams
(:class:`ActionExecutor` / :class:`ReplyInterpreter` / :class:`ApprovalNotifier`) so the
orchestration is testable against real stores with fakes for the edges.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from persona.approvals import (
    ActionProposal,
    ApprovalDecision,
    DecisionType,
    InterpretedIntent,
    Materiality,
    ProposalStatus,
    RawInterpretation,
    resolve_reply,
)
from persona.logging import get_logger
from persona.tasks import (
    TaskCheckpoint,
    UserReply,
    merge_artifact_pointers,
    pointers_from_artifacts,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from datetime import datetime

    from persona.approvals import ReplyInterpreter, ResolvedReply
    from persona.schema.tools import PersistedArtifact
    from persona.tasks import ArtifactPointer
    from pydantic import JsonValue

    from persona_api.approvals.store import ApprovalStore
    from persona_api.tasks.continuation import TaskContinuation
    from persona_api.tasks.store import CheckpointStore, TaskStore

__all__ = [
    "ActionExecutor",
    "ApprovalNotifier",
    "ApprovalResolver",
    "announce_parked_proposal",
    "ExecutedAction",
    "InboxDecision",
    "ResolutionOutcome",
]


class InboxDecision(StrEnum):
    """An explicit, structured approval decision from the A6 inbox (no NL, no model).

    Maps to a deterministic :class:`RawInterpretation` at confidence 1.0 — the click IS the
    interpretation — which runs through the SAME :func:`resolve_reply` floor as a chat reply.
    (No ``clarify``: the inbox never asks a question back at itself.)
    """

    APPROVE = "approve"
    DENY = "deny"
    MODIFY = "modify"


_log = get_logger("api.approvals.resolver")


@dataclass(frozen=True)
class ExecutedAction:
    """What replaying one approved action produced: a summary, and any files it persisted.

    ``artifacts`` is the Spec-28 channel (R9-162). An approved ``file_write`` or
    ``generate_image`` writes real bytes into the workspace, and before this the resolution
    checkpoint recorded only a sentence about it: the file existed and the task's pointer
    list did not know. Empty for the tools that produce only text, which is most of them.
    """

    summary: str
    artifacts: tuple[PersistedArtifact, ...] = ()


class ActionExecutor(Protocol):
    """Executes the EXACT recorded payload verbatim and returns what the replay produced.

    The api wires a plain (un-gated) ``Toolbox`` dispatch — the approval *is* the
    authorisation, so execution does not re-gate. The model never re-derives the call.
    """

    async def execute(
        self, tool_name: str, arguments: Mapping[str, JsonValue]
    ) -> ExecutedAction: ...


class ApprovalNotifier(Protocol):
    """The C0 persona-voiced messages of the approval loop (Originator-backed in the api)."""

    async def ask(self, proposal: ActionProposal) -> None: ...

    async def reconfirm(self, proposal: ActionProposal) -> None: ...

    async def clarify(self, proposal: ActionProposal) -> None: ...

    async def remind(self, proposal: ActionProposal) -> None: ...

    async def expired(self, proposal: ActionProposal) -> None: ...


@dataclass(frozen=True)
class ResolutionOutcome:
    """What a resolve did — enough for tests + observability without over-building."""

    outcome: DecisionType | None  # the floor verdict; None on an idempotent no-op
    executed: bool = False
    executed_arguments: Mapping[str, JsonValue] | None = None
    resumed: bool = False
    note: str = ""


async def announce_parked_proposal(
    approvals: ApprovalStore,
    notifier: ApprovalNotifier,
    *,
    owner_id: str,
    proposal_id: str,
) -> None:
    """Voice the C0 "may I do X?" for a proposal that is STILL pending.

    One implementation, two callers: :meth:`ApprovalResolver.announce` and the leg handler's
    notify-on-park hook in ``worker_root``. It is a module function rather than a method
    because the hook has a store and a notifier and none of the resolver's other six
    collaborators, and building a whole resolver to reach one guard is how the second copy
    came to exist in the first place.

    **The guard is the point.** Until 2026-09-15 the wired path was a near-duplicate of this
    that omitted the ``PENDING`` check, so a re-delivered leg job could voice "may I do X?"
    for a proposal the user had already answered. The resolver's copy had the check and no
    callers; the hook had callers and no check (part1 F6, which the sweep recorded as an
    unwired feature and which was really a wired duplicate of one).
    """
    proposal = approvals.get_proposal(owner_id, proposal_id)
    if proposal.status is ProposalStatus.PENDING:
        await notifier.ask(proposal)


class ApprovalResolver:
    """Closes the approval loop: reply → floor → verbatim replay → resolution checkpoint."""

    def __init__(
        self,
        *,
        approvals: ApprovalStore,
        tasks: TaskStore,
        checkpoints: CheckpointStore,
        continuation: TaskContinuation,
        interpreter: ReplyInterpreter,
        executor: ActionExecutor,
        notifier: ApprovalNotifier,
    ) -> None:
        self._approvals = approvals
        self._tasks = tasks
        self._checkpoints = checkpoints
        self._continuation = continuation
        self._interpreter = interpreter
        self._executor = executor
        self._notifier = notifier

    async def announce(self, owner_id: str, proposal_id: str) -> None:
        """Originate the persona-voiced C0 ask for a freshly-parked proposal (the leg gated)."""
        await announce_parked_proposal(
            self._approvals, self._notifier, owner_id=owner_id, proposal_id=proposal_id
        )

    async def resolve(
        self, owner_id: str, proposal_id: str, reply: str, channel: str, *, now: datetime
    ) -> ResolutionOutcome:
        """Resolve a natural-language reply against the pending proposal (the chat twin).

        Idempotent; the floor is never bypassed. The model interpretation is the ONLY difference
        from the inbox path — both converge on :meth:`_apply_resolved` (same floor, CAS, record).
        """
        proposal = self._approvals.get_proposal(owner_id, proposal_id)
        if proposal.status is not ProposalStatus.PENDING:
            # A reply for an already-resolved proposal (a re-delivered C1 webhook) — no-op.
            _log.info(
                "resolve no-op (not pending)", proposal_id=proposal_id, status=proposal.status
            )
            return ResolutionOutcome(outcome=None, note="not_pending")
        raw = await self._interpreter.interpret(reply, proposal)
        return await self._apply_resolved(
            owner_id, proposal, raw, verbatim_reply=reply, channel=channel, now=now
        )

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
        """Resolve an explicit, structured decision from the A6 inbox (the twin of :meth:`resolve`).

        The click IS the interpretation: a deterministic :class:`RawInterpretation` at confidence
        1.0, fed through the SAME :func:`resolve_reply` floor — genuine twin-parity, not a divergent
        path (a material modify still re-confirms; the model cannot manufacture an approval). No
        model call. Idempotent + exactly-one-winner identically to :meth:`resolve` (the status
        pre-check + the ``transition_proposal`` CAS): a concurrent chat+inbox double-resolve has one
        winner; the loser is a clean ``not_pending`` no-op (the surface reflects 'already handled'
        from the durable record).
        """
        proposal = self._approvals.get_proposal(owner_id, proposal_id)
        if proposal.status is not ProposalStatus.PENDING:
            _log.info(
                "resolve_structured no-op (not pending)",
                proposal_id=proposal_id,
                status=proposal.status,
            )
            return ResolutionOutcome(outcome=None, note="not_pending")
        raw = _raw_from_decision(decision, edited_arguments)
        return await self._apply_resolved(
            owner_id, proposal, raw, verbatim_reply=verbatim_reply, channel=channel, now=now
        )

    async def _apply_resolved(
        self,
        owner_id: str,
        proposal: ActionProposal,
        raw: RawInterpretation,
        *,
        verbatim_reply: str,
        channel: str,
        now: datetime,
    ) -> ResolutionOutcome:
        """Apply an interpretation through the floor → record → dispatch (shared by both twins).

        The one path both the NL reply and the structured inbox decision converge on: the floor
        (never bypassed), the durable verbatim decision record, and the outcome dispatch.
        """
        prior_decisions = self._approvals.list_decisions(owner_id, proposal.proposal_id)
        clarifications_used = sum(1 for d in prior_decisions if d.type is DecisionType.CLARIFY)
        # A material edit revised this payload earlier and is now being re-confirmed, so the
        # plain "approve" arriving here is an approval OF THE USER'S OWN VERSION of the action.
        # The durable record has to say so; the decision trail is where that is already written.
        already_edited = any(d.type is DecisionType.MODIFY for d in prior_decisions)
        resolved = resolve_reply(
            raw, original_arguments=proposal.arguments, clarifications_used=clarifications_used
        )
        self._approvals.record_decision(
            owner_id,
            ApprovalDecision(
                decision_id=f"dec_{uuid.uuid4().hex}",
                proposal_id=proposal.proposal_id,
                type=resolved.outcome,
                verbatim_reply=verbatim_reply,
                channel=channel,
                edited_arguments=resolved.edited_arguments,
                decided_at=now,
            ),
        )
        if resolved.outcome is DecisionType.APPROVE:
            return await self._execute_and_resume(
                owner_id,
                proposal,
                proposal.arguments,
                verbatim_reply,
                now,
                carries_edit=already_edited,
            )
        if resolved.outcome is DecisionType.MODIFY:
            return await self._handle_modify(owner_id, proposal, resolved, verbatim_reply, now)
        if resolved.outcome is DecisionType.DENY:
            return self._deny_and_resume(owner_id, proposal, verbatim_reply, now)
        # CLARIFY — ask once more; the proposal stays pending (the floor caps this at one).
        await self._notifier.clarify(proposal)
        return ResolutionOutcome(outcome=DecisionType.CLARIFY, note="clarified")

    # --- the resolution paths ----------------------------------------------

    async def _handle_modify(
        self,
        owner_id: str,
        proposal: ActionProposal,
        resolved: ResolvedReply,
        reply: str,
        now: datetime,
    ) -> ResolutionOutcome:
        edited = dict(resolved.edited_arguments or {})
        if resolved.materiality is Materiality.MATERIAL:
            # Revise the recorded payload in place + re-confirm (stays pending — never executes
            # a materially-changed action without a fresh confirmation).
            revised = self._approvals.revise_pending_payload(
                owner_id,
                proposal.proposal_id,
                arguments=edited,
                description=f"Run `{proposal.tool_name}` with {edited}",
                now=now,
            )
            if revised is not None:
                await self._notifier.reconfirm(revised)
            return ResolutionOutcome(outcome=DecisionType.MODIFY, note="reconfirm")
        # Immaterial edit → execute the edited payload directly (phrasing change). It is still
        # the user's own wording that goes out, so the record lands MODIFIED, not APPROVED.
        return await self._execute_and_resume(
            owner_id, proposal, edited, reply, now, carries_edit=True
        )

    async def _execute_and_resume(
        self,
        owner_id: str,
        proposal: ActionProposal,
        exec_arguments: Mapping[str, JsonValue],
        reply: str,
        now: datetime,
        *,
        carries_edit: bool,
    ) -> ResolutionOutcome:
        """The at-most-once execution gate: CAS-win → verbatim replay → resolution checkpoint.

        ``carries_edit`` picks which green light the record keeps: ``modified`` when the payload
        about to run is the user's edited version (an immaterial edit executing straight away, or
        the re-confirmation of a material one), ``approved`` when it is the action exactly as the
        persona proposed it. Both execute identically and both consume; the difference is only
        what the record can tell you afterwards, which until now it could not.
        """
        decided = ProposalStatus.MODIFIED if carries_edit else ProposalStatus.APPROVED
        approved = self._approvals.transition_proposal(
            owner_id,
            proposal.proposal_id,
            expected=ProposalStatus.PENDING,
            new=decided,
            now=now,
        )
        if approved is None:
            # Lost the CAS race (a concurrent/duplicated approve already won) — do NOT execute.
            _log.info("execute skipped (lost approve CAS)", proposal_id=proposal.proposal_id)
            return ResolutionOutcome(outcome=DecisionType.APPROVE, executed=False, note="race_lost")

        # Verbatim replay — the EXACT recorded payload, never re-derived by the model.
        result = await self._executor.execute(proposal.tool_name, exec_arguments)
        headline = "Approved with your edits" if carries_edit else "Approved"
        self._write_resolution_checkpoint(
            owner_id,
            proposal.task_id,
            conclusion=f"{headline} + executed: {proposal.description} → {result.summary}",
            produced=pointers_from_artifacts(result.artifacts),
            now=now,
        )
        self._approvals.transition_proposal(
            owner_id,
            proposal.proposal_id,
            # The one admission predicate: whichever green light the decision wrote, it
            # consumes. Naming the set here is what keeps `modified` from being a status that
            # executes and then never reaches the terminal it is supposed to reach.
            expected=ProposalStatus.executable(),
            new=ProposalStatus.CONSUMED,
            now=now,
        )
        self._continuation.resume(owner_id, proposal.task_id, UserReply(reply=reply), now=now)
        _log.info(
            "approved action executed + resumed",
            proposal_id=proposal.proposal_id,
            task_id=proposal.task_id,
        )
        return ResolutionOutcome(
            outcome=DecisionType.APPROVE,
            executed=True,
            executed_arguments=dict(exec_arguments),
            resumed=True,
        )

    def _deny_and_resume(
        self, owner_id: str, proposal: ActionProposal, reply: str, now: datetime
    ) -> ResolutionOutcome:
        """Denial is first-class: CAS-gate a one-time denial checkpoint + resume (leg adapts)."""
        denied = self._approvals.transition_proposal(
            owner_id,
            proposal.proposal_id,
            expected=ProposalStatus.PENDING,
            new=ProposalStatus.DENIED,
            now=now,
        )
        if denied is None:
            return ResolutionOutcome(outcome=DecisionType.DENY, resumed=False, note="race_lost")
        self._write_resolution_checkpoint(
            owner_id,
            proposal.task_id,
            conclusion=f"User denied: {proposal.description}. Adapt the plan or report.",
            now=now,
        )
        self._continuation.resume(owner_id, proposal.task_id, UserReply(reply=reply), now=now)
        return ResolutionOutcome(outcome=DecisionType.DENY, resumed=True)

    def _write_resolution_checkpoint(
        self,
        owner_id: str,
        task_id: str,
        *,
        conclusion: str,
        now: datetime,
        produced: Sequence[ArtifactPointer] = (),
    ) -> None:
        """Fold the resolution into a new checkpoint (the A2 CAS gates the durable write)."""
        task = self._tasks.get(owner_id, task_id)
        prior = self._checkpoints.get_latest(owner_id, task_id)
        seq = task.next_checkpoint_seq
        checkpoint = TaskCheckpoint(
            task_id=task_id,
            leg_id=f"{task_id}:approval:{seq}",
            checkpoint_seq=seq,
            progress_conclusions=(
                *(prior.progress_conclusions if prior is not None else ()),
                conclusion,
            ),
            next_step=prior.next_step if prior is not None else "",
            open_questions=prior.open_questions if prior is not None else (),
            # R9-162: an approved action that wrote a file leaves the file in the pointer
            # list, not only in a sentence. A denial produces nothing, so it carries the
            # prior pointers through unchanged (the merge with an empty tuple).
            artifact_pointers=merge_artifact_pointers(
                prior.artifact_pointers if prior is not None else (), produced
            ),
            # Spec W1 (D-W1-16): an approval decision is not a leg and asks the world
            # nothing, so it carries both ledgers forward untouched. Dropping them here
            # would make every approved task re-run every search it had already run.
            queries_run=prior.queries_run if prior is not None else (),
            sources_seen=prior.sources_seen if prior is not None else (),
            blocked_on=None,
            updated_at=now,
        )
        self._checkpoints.append(task, checkpoint, spend={}, now=now)


def _raw_from_decision(
    decision: InboxDecision, edited_arguments: Mapping[str, JsonValue] | None
) -> RawInterpretation:
    """Build the deterministic confidence-1.0 interpretation for a structured inbox decision.

    The inbox click is unambiguous, so it enters the floor at full confidence — but the floor still
    governs (a material modify re-confirms; there is no way to skip it). A ``modify`` without edits
    is a caller error (the inbox must supply the edited payload).
    """
    if decision is InboxDecision.APPROVE:
        return RawInterpretation(intent=InterpretedIntent.APPROVE, confidence=1.0)
    if decision is InboxDecision.DENY:
        return RawInterpretation(intent=InterpretedIntent.DENY, confidence=1.0)
    if edited_arguments is None:
        msg = "a MODIFY inbox decision requires edited_arguments"
        raise ValueError(msg)
    return RawInterpretation(
        intent=InterpretedIntent.MODIFY, confidence=1.0, edited_arguments=edited_arguments
    )
