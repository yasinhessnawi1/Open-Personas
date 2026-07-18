"""Memory (knowledge-graph UI) routes (Spec K5, Group A — the read foundation).

The windowed graph, node detail, and search projections over the K0 graph store (via
:mod:`memory_service`). RLS-scoped through the per-request engine (D-08-1) — the owner
is the authenticated caller; the GET routes are reads (CQS). When no graph store is
wired (community edition, or graph off) the area reads as empty rather than erroring —
the Memory tab simply has nothing yet. The correction (PATCH) and plain deletion
(DELETE) writes are the K5 trust spine; ``forget-preview``/``forget`` (Spec K11) are
the cross-layer forget that also reaches the episodic evidence a concept node was
distilled from — a plain ``DELETE`` alone is resurrection-prone (D-K11-4).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Query, Request, status
from persona.graph.errors import GraphNodeNotFoundError

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.errors import MemoryNodeNotFoundError
from persona_api.schemas.requests import ForgetRequest, MemoryCorrectionRequest
from persona_api.schemas.responses import (
    EpisodicMembersResponse,
    EpisodicWindowResponse,
    ForgetPreviewResponse,
    MemoryNodeDetail,
    MemorySearchResponse,
    MemoryWindowResponse,
)
from persona_api.services import memory_service, persona_service

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.audit import AuditLogger
    from persona.graph.protocol import GraphStore
    from persona.stores.backend import Backend
    from sqlalchemy import Engine

router = APIRouter(prefix="/v1/memory", tags=["memory"])


def _graph_store(request: Request) -> GraphStore | None:
    """The wired graph store, or ``None`` when the graph is not composed (edition/off)."""
    store: GraphStore | None = getattr(request.app.state, "graph_store", None)
    return store


def _memory_backend(request: Request) -> Backend | None:
    """The wired episodic transport, or ``None`` when no memory backend is composed.

    The SAME app-level, embedder-wired backend the runtime writes/retrieves episodic
    through (Spec K11) — reused, never rebuilt per request, so a forget-preview never
    pays an ML model reload (D-K11-8 makes the REQUEST's latency a non-constraint, not
    reloading weights from disk on every click).
    """
    backend: Backend | None = getattr(request.app.state, "memory_backend", None)
    return backend


def _audit_logger(request: Request) -> AuditLogger | None:
    """The app's configured audit logger (JSONL or Postgres, R5-D-2) — reused, not rebuilt."""
    logger: AuditLogger | None = getattr(request.app.state, "audit_logger", None)
    return logger


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


def _forget_composition(
    request: Request, user: AuthenticatedUser, node_id: str
) -> tuple[GraphStore, Backend, AuditLogger, Engine]:
    """The four pieces a forget needs, or a 404 (node absent/not-owned, or composition
    incomplete — no graph, no episodic backend, or no RLS engine wired, e.g. the
    community/no-DB edition). Existence-disclosure-safe: both reasons read identically
    to the caller (mirrors the K5 ``delete_node`` pattern)."""
    store = _graph_store(request)
    detail = None if store is None else memory_service.node_detail(store, user.id, node_id)
    backend = _memory_backend(request)
    audit_logger = _audit_logger(request)
    rls_engine: Engine | None = getattr(request.app.state, "rls_engine", None)
    if (
        detail is None
        or store is None
        or backend is None
        or audit_logger is None
        or rls_engine is None
    ):
        raise MemoryNodeNotFoundError("memory not found", context={"node_id": node_id})
    return store, backend, audit_logger, rls_engine


@router.post("/nodes/{node_id}/forget-preview", response_model=ForgetPreviewResponse)
async def forget_preview_node(
    request: Request,
    node_id: str,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ForgetPreviewResponse:
    """Cross-persona episodic evidence a forget would also delete (Spec K11, D-K11-1).

    A pure preview (CQS) — nothing is deleted. 404 when the node is not the caller's.
    """
    store, backend, audit_logger, rls_engine = _forget_composition(request, user, node_id)
    candidates = memory_service.forget_preview(
        graph_store=store,
        memory_backend=backend,
        audit_logger=audit_logger,
        rls_engine=rls_engine,
        owner_id=user.id,
        node_id=node_id,
        floor=memory_service.ForgetSettings().similarity_floor,
    )
    return ForgetPreviewResponse(candidates=candidates)


@router.post("/nodes/{node_id}/forget", status_code=status.HTTP_204_NO_CONTENT)
async def forget_node(
    request: Request,
    node_id: str,
    body: ForgetRequest,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Commit a cross-layer forget (Spec K11, D-K11-2): confirmed episodic evidence
    (cascading its covering gists, K8-D-14) AND the concept node, one operation — the
    resurrection guard, since a sleep-time consolidation pass can only re-distill from
    evidence that still exists. 404 when the node is not the caller's.
    """
    store, backend, audit_logger, rls_engine = _forget_composition(request, user, node_id)
    memory_service.forget(
        graph_store=store,
        memory_backend=backend,
        audit_logger=audit_logger,
        rls_engine=rls_engine,
        owner_id=user.id,
        node_id=node_id,
        episodic=[(e.persona_id, e.chunk_id) for e in body.episodic],
    )


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


@router.get("/episodic", response_model=EpisodicWindowResponse)
async def get_episodic_window(
    request: Request,
    persona_id: str = Query(min_length=1),
    q: str | None = Query(default=None, min_length=1, description="Semantic search, gist-scoped."),
    cursor: str | None = Query(  # noqa: ARG001 — reserved for future paging (K11-T2 v1: full window)
        default=None, description="Reserved for pagination; not yet implemented."
    ),
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> EpisodicWindowResponse:
    """The episodic browser's gist-layer window for one persona (Spec K11, D-K11-5).

    A standalone graph, separate from the concept graph — gist cluster-nodes,
    drillable to raw members via ``GET .../episodic/{id}/members``. ``available=False``
    mirrors the K5 graph route when no episodic backend is composed (community/off
    edition). Ownership is enforced by the real per-request RLS engine the shared
    ``memory_backend`` rides — a foreign ``persona_id`` reads as empty, never another
    tenant's rows (the K5-proven pattern; see ``memory_chunks``' RLS policy).
    """
    backend = _memory_backend(request)
    audit_logger = _audit_logger(request)
    if backend is None or audit_logger is None:
        return EpisodicWindowResponse(available=False, gists=[])
    return memory_service.episodic_window(backend, audit_logger, persona_id, q=q)


@router.get("/episodic/{gist_id}/members", response_model=EpisodicMembersResponse)
async def get_episodic_members(
    request: Request,
    gist_id: str,
    persona_id: str = Query(min_length=1),
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> EpisodicMembersResponse:
    """A gist's raw members, in gist order — the browser's drill-down (Spec K11, D-K11-5)."""
    backend = _memory_backend(request)
    audit_logger = _audit_logger(request)
    if backend is None or audit_logger is None:
        return EpisodicMembersResponse(members=[])
    return memory_service.episodic_members(backend, audit_logger, persona_id, gist_id)


@router.delete("/episodic/{chunk_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_episodic_node(
    request: Request,
    chunk_id: str,
    persona_id: str = Query(min_length=1),
    is_gist: bool = Query(default=False),
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> None:
    """Delete a raw chunk or a gist cluster in the episodic browser (Spec K11, D-K11-6).

    A raw chunk deletes itself (cascades its covering gist, K8-D-14); a gist deletes
    its cluster's raw members (the cascade then removes the gist too). Never reaches
    the concept graph (D-K11-6 — facts stay managed in the concept view). 404 when the
    id is not found on the (RLS-scoped) persona's episodic store —
    existence-disclosure-safe, mirroring the K5 ``delete_node`` pattern.
    """
    backend = _memory_backend(request)
    audit_logger = _audit_logger(request)
    deleted = (
        backend is not None
        and audit_logger is not None
        and memory_service.episodic_delete(
            backend, audit_logger, persona_id, chunk_id, is_gist=is_gist
        )
    )
    if not deleted:
        raise MemoryNodeNotFoundError("memory not found", context={"chunk_id": chunk_id})


__all__ = ["router"]
