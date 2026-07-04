"""Memory (knowledge-graph UI) read routes (Spec K5, Group A — the read foundation).

GET-only here: the windowed graph, node detail, and search projections over the K0
graph store (via :mod:`memory_service`). RLS-scoped through the per-request engine
(D-08-1) — the owner is the authenticated caller; these are reads (CQS). When no graph
store is wired (community edition, or graph off) the area reads as empty rather than
erroring — the Memory tab simply has nothing yet. The correction (PATCH) and deletion
(DELETE) writes — the trust spine — land in their own tasks.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Query, Request, status
from persona.graph.errors import GraphNodeNotFoundError

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.errors import MemoryNodeNotFoundError
from persona_api.schemas.requests import MemoryCorrectionRequest
from persona_api.schemas.responses import (
    MemoryNodeDetail,
    MemorySearchResponse,
    MemoryWindowResponse,
)
from persona_api.services import memory_service, persona_service

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.graph.protocol import GraphStore
    from sqlalchemy import Engine

router = APIRouter(prefix="/v1/memory", tags=["memory"])


def _graph_store(request: Request) -> GraphStore | None:
    """The wired graph store, or ``None`` when the graph is not composed (edition/off)."""
    store: GraphStore | None = getattr(request.app.state, "graph_store", None)
    return store


def _persona_name_resolver(request: Request) -> Callable[[str], str | None] | None:
    """A ``persona_id → name`` resolver bound to the request's RLS engine (owner-scoped).

    Feeds the node-detail projection so provenance reads "learned by <persona>" instead of
    a bare "system" (R-K5-PROV-PERSONA). ``None`` when no RLS engine is wired; each lookup
    is fail-soft (a miss → ``None`` → the UI's source-based fallback avatar).
    """
    rls_engine: Engine | None = getattr(request.app.state, "rls_engine", None)
    if rls_engine is None:
        return None

    def _resolve(persona_id: str) -> str | None:
        try:
            return persona_service.persona_display_name(
                rls_engine=rls_engine, persona_id=persona_id
            )
        except Exception:  # noqa: BLE001 — attribution is fail-soft on a read path.
            return None

    return _resolve


@router.get("/graph", response_model=MemoryWindowResponse)
async def get_graph_window(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    focus: str | None = Query(
        default=None, description="Centre node id; omit for the first-paint seed window."
    ),
) -> MemoryWindowResponse:
    """A windowed slice of the caller's graph — the seed, or a focus neighbourhood (K5-D-2)."""
    store = _graph_store(request)
    if store is None:
        # No usable graph store (community-on-SQLite / graph off): report
        # ``available=False`` so the UI shows a distinct "Memory isn't available
        # here" state + gates the nav — NOT the "no memories yet" empty invite,
        # which would misrepresent an unavailable graph as an empty one (Spec K5).
        return MemoryWindowResponse(
            available=False,
            focus_id=focus,
            is_seed=focus is None,
            total_nodes=0,
            nodes=[],
            links=[],
        )
    return memory_service.build_window(store, user.id, focus_id=focus)


@router.get("/nodes/{node_id}", response_model=MemoryNodeDetail)
async def get_node_detail(
    request: Request,
    node_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MemoryNodeDetail:
    """A node's full detail: content, provenance, evolution, typed links (criterion 3)."""
    store = _graph_store(request)
    detail = (
        None
        if store is None
        else memory_service.node_detail(
            store,
            user.id,
            node_id,
            persona_name_resolver=_persona_name_resolver(request),
        )
    )
    if detail is None:
        raise MemoryNodeNotFoundError("memory not found", context={"node_id": node_id})
    return detail


@router.patch("/nodes/{node_id}", response_model=MemoryNodeDetail)
async def correct_node(
    request: Request,
    node_id: str,
    body: MemoryCorrectionRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> MemoryNodeDetail:
    """Correct a node's content (criterion 6) — re-embed, re-index, provenance → user-edited.

    The most trustworthy write the graph gets (K5-D-7). Returns the fresh detail so the
    panel shows the user-edited provenance immediately. 404 when the node is not the caller's.
    """
    store = _graph_store(request)
    if store is None:
        raise MemoryNodeNotFoundError("memory not found", context={"node_id": node_id})
    try:
        return memory_service.correct(store, user.id, node_id, body.content)
    except GraphNodeNotFoundError as exc:
        # Re-raise as the API's 404 domain error (catch-at-boundary, eng-std §1).
        raise MemoryNodeNotFoundError("memory not found", context={"node_id": node_id}) from exc


@router.delete("/nodes/{node_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_node(
    request: Request,
    node_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Delete a node — gone from Postgres + index + every persona's retrieval (criterion 7).

    The trust-critical action (K5 §7). 204 on success; 404 when the node is not the
    caller's (existence-disclosure-safe — never reveals another tenant's node exists).
    """
    store = _graph_store(request)
    deleted = store is not None and memory_service.delete(store, user.id, node_id)
    if not deleted:
        raise MemoryNodeNotFoundError("memory not found", context={"node_id": node_id})


@router.get("/search", response_model=MemorySearchResponse)
async def search_memory(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    q: str = Query(min_length=1, description="The search query — exact term or paraphrase."),
) -> MemorySearchResponse:
    """Search-to-navigate over the caller's graph — K1 hybrid retrieval (criterion 5)."""
    store = _graph_store(request)
    if store is None:
        return MemorySearchResponse(query=q, results=[])
    return memory_service.search(store, user.id, q)


__all__ = ["router"]
