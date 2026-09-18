"""The approvals inbox — pending decisions across a user's tasks (Spec A6, B3).

Three thin, RLS-scoped endpoints over the shared approval machinery:

- ``GET /v1/approvals`` — every pending proposal, rendered FAITHFULLY (the exact ``arguments`` +
  ``description``, verbatim — a safety surface, not a summary; the web renders them as text).
- ``GET /v1/approvals/handled``: the most recently DECIDED proposals, newest first, so a
  reopened inbox can still say what happened (and, for an edited one, whose version went out).
- ``GET /v1/approvals/{id}``: one proposal, faithfully, whatever its status.
- ``POST /v1/approvals/{id}/decision`` — approve / deny / modify through the shared
  :class:`ApprovalResolutionService`, the SAME floor + CAS + durable record as the chat twin. A
  chat-vs-inbox race resolves ONCE: the loser gets ``not_pending`` and the durable status shows
  what won — the surface reflects 'already handled' (A6-D-3, criterion 5).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request
from persona.errors import ApprovalNotFoundError

from persona_api.approvals import EXPIRE_AFTER_DEFAULT, ApprovalStore
from persona_api.approvals.resolver import InboxDecision
from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.schemas.requests import ApprovalDecisionRequest
from persona_api.schemas.responses import ApprovalDecisionResult, ApprovalOut

if TYPE_CHECKING:
    from persona.approvals import ActionProposal

router = APIRouter(prefix="/v1/approvals", tags=["approvals"])


def _to_out(proposal: ActionProposal, *, edited: bool) -> ApprovalOut:
    """Render a proposal FAITHFULLY — verbatim arguments + description + the expiry countdown."""
    return ApprovalOut(
        proposal_id=proposal.proposal_id,
        task_id=proposal.task_id,
        persona_id=proposal.persona_id,
        tool_name=proposal.tool_name,
        arguments=dict(proposal.arguments),  # the EXACT recorded payload — never paraphrased
        description=proposal.description,
        categories=sorted(c.value for c in proposal.categories),
        created_at=proposal.created_at,
        expires_at=proposal.created_at + EXPIRE_AFTER_DEFAULT,
        status=proposal.status.value,
        edited=edited,
    )


def _render(
    store: ApprovalStore, owner_id: str, proposals: list[ActionProposal]
) -> list[ApprovalOut]:
    """Render a page of proposals, asking the decision trail ONCE which of them carry edits."""
    edited = store.list_edited_proposal_ids(owner_id, [p.proposal_id for p in proposals])
    return [_to_out(p, edited=p.proposal_id in edited) for p in proposals]


@router.get("", response_model=list[ApprovalOut])
async def list_approvals(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[ApprovalOut]:
    """Every pending approval across the caller's tasks, oldest-first (RLS-scoped, faithful)."""
    store = ApprovalStore(request.app.state.rls_engine)
    return _render(store, user.id, store.list_pending_for_owner(user.id))


@router.get("/handled", response_model=list[ApprovalOut])
async def list_handled_approvals(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    limit: int = 10,
) -> list[ApprovalOut]:
    """The caller's most recently DECIDED approvals, newest first: the reopenable half.

    Declared before ``/{proposal_id}`` so the literal path wins the match. A decision used to
    leave nothing a reopened inbox could read: the list endpoint returns pending proposals only,
    so "Approved with your edits" existed for as long as the tab stayed open and not a moment
    longer. This is the same durable rows, read back.
    """
    store = ApprovalStore(request.app.state.rls_engine)
    return _render(
        store,
        user.id,
        store.list_recently_resolved_for_owner(user.id, limit=max(1, min(limit, 50))),
    )


@router.get("/{proposal_id}", response_model=ApprovalOut)
async def get_approval(
    proposal_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalOut:
    """One approval, faithfully (any status). 404 when it isn't the caller's / doesn't exist."""
    store = ApprovalStore(request.app.state.rls_engine)
    try:
        proposal = store.get_proposal(user.id, proposal_id)
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="approval not found") from exc
    return _render(store, user.id, [proposal])[0]


@router.post("/{proposal_id}/decision", response_model=ApprovalDecisionResult)
async def decide_approval(
    proposal_id: str,
    body: ApprovalDecisionRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ApprovalDecisionResult:
    """Approve / deny / modify a pending approval — the inbox twin of a chat reply (A6-D-3).

    Goes through the shared :class:`ApprovalResolutionService` (same floor, CAS, durable record). A
    race with the chat path resolves ONCE; the loser gets ``outcome=None`` / ``not_pending`` and the
    durable ``status`` shows what actually won — the surface reflects 'already handled'.
    """
    build = getattr(request.app.state, "build_approval_resolver", None)
    if build is None:  # keyless / community-without-C0 boot — the loop is inert
        raise HTTPException(status_code=503, detail="approval resolution is not available")
    if body.decision == "modify" and body.edited_arguments is None:
        raise HTTPException(status_code=422, detail="a modify decision requires edited_arguments")

    store = ApprovalStore(request.app.state.rls_engine)
    # Confirm ownership/existence first so a missing/foreign proposal is a clean 404, not a 500.
    try:
        store.get_proposal(user.id, proposal_id)
    except ApprovalNotFoundError as exc:
        raise HTTPException(status_code=404, detail="approval not found") from exc

    outcome = await build().resolve_structured(
        user.id,
        proposal_id,
        decision=InboxDecision(body.decision),
        edited_arguments=body.edited_arguments,
        verbatim_reply=body.note or body.decision,
        channel="web",
        now=datetime.now(UTC),
    )
    # Read the DURABLE post-state for the honest reflection — never the pushed/cached outcome.
    status = store.get_proposal(user.id, proposal_id).status
    return ApprovalDecisionResult(
        outcome=outcome.outcome.value if outcome.outcome is not None else None,
        executed=outcome.executed,
        note=outcome.note,
        status=status.value,
    )


__all__ = ["router"]
