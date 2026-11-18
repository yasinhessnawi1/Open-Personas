"""The "fine to bring this up" control, per conversation (D-OWNER-11-18 (16), D-ORCH-11-18-u).

``GET`` reads it and ``PUT`` changes it, both as the authenticated caller: the owner comes from
the token, never from the request, and a conversation that is not the caller's is a 404. This
is the only way in. No chat message, tool or MCP surface can release a conversation, because
consent parsed from chat could be forged by content the persona reads.

A release carries the ``version`` the control was read at; a disclosure recorded since makes it
a 409, and the page reads again. Without a graph (community on SQLite) nothing is ever tagged,
so the control reads ``not_held`` and writes nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, HTTPException, Request, status

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.initiative.conversation_hold import (
    ConversationHold,
    ConversationHoldService,
    HoldChangedError,
    not_held,
)
from persona_api.middleware.rate_limit import rate_limit
from persona_api.schemas.requests import SetInitiativeHoldRequest
from persona_api.schemas.responses import InitiativeHoldResponse

if TYPE_CHECKING:
    from persona.graph.protocol import GraphStore
    from sqlalchemy import Engine

router = APIRouter(prefix="/v1", tags=["conversations"])

_PATH = "/conversations/{conversation_id}/initiative-hold"


def _service(request: Request) -> ConversationHoldService | None:
    """The control over the app's graph and engine, or ``None`` when no graph is composed."""
    store: GraphStore | None = getattr(request.app.state, "graph_store", None)
    engine: Engine | None = getattr(request.app.state, "rls_engine", None)
    if store is None or engine is None:
        return None
    return ConversationHoldService(store, engine)


def _out(hold: ConversationHold) -> InitiativeHoldResponse:
    return InitiativeHoldResponse(
        state=hold.state, released_at=hold.released_at, version=hold.version
    )


@router.get(
    _PATH,
    response_model=InitiativeHoldResponse,
    dependencies=[Depends(rate_limit("default"))],
)
async def get_initiative_hold(
    conversation_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> InitiativeHoldResponse:
    """Whether a disclosure keeps this conversation out of unprompted initiative."""
    service = _service(request)
    if service is None:
        return _out(not_held())
    return _out(service.read(user.id, conversation_id))


@router.put(
    _PATH,
    response_model=InitiativeHoldResponse,
    dependencies=[Depends(rate_limit("default"))],
)
async def set_initiative_hold(
    conversation_id: str,
    body: SetInitiativeHoldRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> InitiativeHoldResponse:
    """Release the conversation for initiative, or hold it back again; the new state."""
    service = _service(request)
    if service is None:
        return _out(not_held())
    try:
        hold = service.change(
            user.id,
            conversation_id,
            released=body.released,
            version=body.version,
            at=datetime.now(UTC),
        )
    except HoldChangedError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "code": "conversation_changed",
                "message": "this conversation changed, read it again",
            },
        ) from exc
    return _out(hold)
