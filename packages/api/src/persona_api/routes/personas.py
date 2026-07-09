"""Persona CRUD + LLM-assisted authoring routes (spec 08, T07, §5.1).

Every route depends on ``get_current_user`` (which sets the RLS contextvar, so
the service's engine transactions are tenant-scoped — D-08-1) and reads the
RLS engine + embedder from ``app.state`` (attached by the lifespan). The
business logic lives in the services; the routes are thin.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from persona.imagegen import ContentRejectedError, ImageGenError, craft_avatar_prompt
from persona.logging import get_logger
from persona.tools.audit import JSONLToolAuditLogger, ToolAuditEvent
from persona_runtime.routing import tier_for

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.config import Edition
from persona_api.errors import RefinementLimitError
from persona_api.imagegen import service as imagegen_service
from persona_api.jobs.handlers.avatar import enqueue_avatar_generation
from persona_api.middleware.rate_limit import rate_limit
from persona_api.middleware.rls_context import current_user_id
from persona_api.routes._runtime_guard import require_model_backend
from persona_api.schemas import (
    AuthoringDraft,
    AuthorPersonaRequest,
    CreatePersonaRequest,
    GrantToolRequest,
    PersonaCapabilities,
    PersonaDetail,
    PersonaSpecialitySummary,
    PersonaSummary,
    RefinePersonaRequest,
    SetConsentRequest,
    SetSkillConsentRequest,
    ToolRecommendationResponse,
    UpdatePersonaRequest,
)
from persona_api.services import (
    audit_service,
    authoring_service,
    catalog_service,
    consent_service,
    notifications_service,
    persona_service,
    skill_consent_service,
    tool_consent_service,
    voice_assignment_service,
)
from persona_api.services.provenance import avatar_ai_generated_from_source

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.schema.skills import SkillSpec
    from persona_runtime.tier import TierRegistry

# The 3-round refinement cap (D-10-5): the UI owns the counter, the server is
# the backstop. `round` is the count of refinements already applied.
_MAX_REFINE_ROUNDS = 3


def _authoring_sampling(request: Request) -> authoring_service.AuthoringSampling:
    """Build the creative-draft sampling from env-configured API settings.

    Temperature is the primary creativity lever; ``top_p`` / ``top_k`` are
    optional (``None`` ⇒ provider default). The repair retry stays deterministic
    inside the service regardless (D-10-3).
    """
    config = request.app.state.config
    return authoring_service.AuthoringSampling(
        temperature=config.authoring_temperature,
        top_p=config.authoring_top_p,
        top_k=config.authoring_top_k,
    )


#: Fallback avatar-gen wall-clock bound if app.state didn't thread the config
#: value (e.g. a test that builds the app without the Spec-29 lifespan line).
#: The authoritative value is ``APIConfig.avatar_gen_timeout_s`` (D-29-3).
_DEFAULT_AVATAR_GEN_TIMEOUT_S = 25.0

_LOG = get_logger("routes.personas")

router = APIRouter(prefix="/v1/personas", tags=["personas"])


def _tier_registry(request: Request) -> TierRegistry | None:
    """Return the app-scoped :class:`TierRegistry` if the runtime is wired.

    The composition root (``app.py`` lifespan) mounts ``app.state.tier_registry``
    when a runtime backend is configured. Tests that don't wire the runtime
    leave the attribute unset; the persona-detail surface stays usable and
    just omits :attr:`PersonaDetail.capabilities`.
    """
    return getattr(request.app.state, "tier_registry", None)


def _capabilities_from_registry(
    tier_registry: TierRegistry | None,
) -> PersonaCapabilities | None:
    """Hydrate :class:`PersonaCapabilities` from the runtime registry.

    Returns ``None`` if the registry was not wired (test paths / composition
    roots without a runtime). At v0.1 capability is deployment-derived per
    D-F3-X-deployment-vs-persona-capability-framing — the same answer applies
    to every persona under a given deployment because the registry is
    app-scoped. Reads through the public
    :meth:`TierRegistry.supports_vision_for` contract
    (D-F3-X-tier-registry-public-contract) so capability-matrix migrations
    don't ripple here.
    """
    if tier_registry is None:
        return None
    tier_names = tier_registry.configured_tier_names
    vision = any(tier_registry.supports_vision_for(name) for name in tier_names)
    return PersonaCapabilities(vision=vision, configured_tiers=tier_names)


def _tools_from_yaml(yaml_str: str) -> list[str]:
    """Best-effort extraction of the ``tools`` allow-list from a persona YAML string.

    A lightweight ``safe_load`` (not a full schema parse) just to read the allow-list for the
    N2 unavailable-server flag; any malformed/odd shape yields ``[]`` (the flag degrades to
    "nothing to report", never raising on a persona-detail read).
    """
    import yaml

    try:
        data = yaml.safe_load(yaml_str)
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    tools = data.get("tools")
    return [str(t) for t in tools] if isinstance(tools, list) else []


def _persona_detail(
    row: dict[str, object],
    *,
    tier_registry: TierRegistry | None,
    conversation_count: int = 0,
) -> PersonaDetail:
    avatar = row.get("avatar_url")
    consent = row.get("consent_to_auto_dispatch")
    yaml_str = str(row["yaml"])
    # Spec R3 (R3-D-4 / Art. 50): derive the recipient-facing disclosure from the
    # stored provenance signal — never guessed. 'generated' → AI-generated (True),
    # 'uploaded' → not (False), NULL/unknown → None (legacy rows; no claim).
    avatar_source = row.get("avatar_source")
    avatar_src = str(avatar_source) if avatar_source is not None else None
    avatar_ai_generated = avatar_ai_generated_from_source(avatar_src)
    return PersonaDetail(
        id=str(row["id"]),
        yaml=yaml_str,
        schema_version=str(row["schema_version"]),
        avatar_url=str(avatar) if avatar is not None else None,
        avatar_source=avatar_src,
        avatar_ai_generated=avatar_ai_generated,
        capabilities=_capabilities_from_registry(tier_registry),
        consent_to_auto_dispatch=bool(consent) if consent is not None else None,
        consent_updated_at=row.get("consent_updated_at"),  # type: ignore[arg-type]
        created_at=row["created_at"],  # type: ignore[arg-type]
        updated_at=row["updated_at"],  # type: ignore[arg-type]
        conversation_count=conversation_count,
        # N2-D-4 surface c: flag enabled MCP servers no longer in the available catalog.
        unavailable_mcp_servers=catalog_service.unavailable_enabled_mcp_servers(
            _tools_from_yaml(yaml_str)
        ),
    )


def _emit_avatar_build_audit(
    audit: JSONLToolAuditLogger,
    persona_id: str,
    *,
    reason: str,
    detail: str | None = None,
) -> None:
    """Emit the build-hook's own fail-soft audit (backend-absent / timeout / unexpected).

    Covers the two outcomes ``generate_avatar`` cannot reach (no backend
    configured, and the wall-clock timeout that cancels it mid-flight) plus a
    defensive catch-all. The generation-specific outcomes (hard-line / provider
    rejection / provider error) are audited inside ``generate_avatar`` itself.
    Tagged zero-cost system event (D-29-2), JSONL, no migration.
    """
    metadata: dict[str, str] = {
        "outcome": "error",
        "reason": reason,
        "system_initiated": "true",
        "credits_charged": "0",
    }
    if detail is not None:
        metadata["detail"] = detail
    audit.emit(
        ToolAuditEvent(
            timestamp=datetime.now(UTC),
            persona_id=persona_id,
            tool_name="generate_avatar",
            action="execute",
            resource="build_hook",
            is_error=True,
            metadata=metadata,
        )
    )


async def _maybe_generate_avatar(
    request: Request, *, owner_id: str, persona_id: str, yaml_str: str
) -> None:
    """Build-time avatar auto-generation hook (Spec 29 D-29-3, fail-soft).

    Runs after the persona row is committed, only when the builder supplied no
    avatar (the caller guards on ``body.avatar_url is None``). Crafts a
    demographic-safe prompt (D-29-1), generates through the free build-time
    entry bounded by ``avatar_gen_timeout_s`` (D-29-3), and on success points
    ``avatar_url`` at the served uploads path. **Every failure mode fail-softs
    to ``avatar_url=null`` and audits — this coroutine never raises into the
    create path** (D-29-X-fail-soft): a persona must never fail to exist because
    its avatar could not be drawn. F1's default renders until one is set.
    """
    state = request.app.state
    # R5-D-2: the app-selected tool-audit backend (Postgres when multi-worker),
    # falling back to the JSONL default for test overrides that set no logger.
    audit = getattr(state, "tool_audit_logger", None) or JSONLToolAuditLogger(state.audit_root)

    # Backend absent (no PERSONA_IMAGEGEN_API_KEY) → fail-soft + audit.
    backend = getattr(state, "image_backend", None)
    if backend is None:
        _emit_avatar_build_audit(audit, persona_id, reason="backend_not_configured")
        return

    # Re-parse the just-validated YAML to reach identity (cheap; create_persona
    # already proved it validates, so this does not raise in practice).
    persona = persona_service.load_persona_from_yaml(
        yaml_str, persona_id=persona_id, owner_id=owner_id
    )
    prompt = craft_avatar_prompt(persona.identity)
    timeout_s = getattr(state, "avatar_gen_timeout_s", _DEFAULT_AVATAR_GEN_TIMEOUT_S)

    try:
        result = await asyncio.wait_for(
            imagegen_service.generate_avatar(
                file_storage=state.file_storage,
                backend=backend,
                user_id=owner_id,
                persona_id=persona_id,
                prompt=prompt,
                audit_logger=audit,
            ),
            timeout=timeout_s,
        )
    except (ContentRejectedError, ImageGenError):
        # generate_avatar already audited the specific outcome (hard-line /
        # provider rejection / provider error). Fail-soft to null.
        return
    except TimeoutError:
        _emit_avatar_build_audit(audit, persona_id, reason="timeout")
        return
    except Exception as exc:  # noqa: BLE001 — avatar-gen must NEVER break create
        _emit_avatar_build_audit(audit, persona_id, reason="unexpected", detail=str(exc)[:200])
        _LOG.warning("avatar build hook unexpected error", persona_id=persona_id, error=str(exc))
        return

    workspace_path = result.images[0].workspace_path if result.images else None
    if not workspace_path:
        return  # defensive — nothing to point at
    # Store the bare workspace ref (``uploads/<blake2b>.<ext>``), NOT the full
    # route path. The uploads GET route requires Bearer auth + RLS, so the web
    # renders it through the authed-image hook (useAuthedImageBlobUrl), which
    # builds ``{API}/v1/personas/{id}/uploads/{workspace_path}`` itself. Storing
    # the full ``/v1/...`` path made the browser <img> hit the web origin
    # (relative) → 404, and it would 401 even at the API origin (no Bearer).
    # The bare ref is exactly the ``workspacePath`` the authed hook expects.
    avatar_url = workspace_path
    persona_service.set_avatar_url(
        rls_engine=state.rls_engine, persona_id=persona_id, avatar_url=avatar_url
    )


async def _enrich_persona_after_create(
    request: Request,
    *,
    owner_id: str,
    persona_id: str,
    yaml_str: str,
    generate_avatar: bool,
) -> None:
    """Background side-effects of create: voice auto-pick THEN avatar gen (fail-soft).

    Runs as a FastAPI ``BackgroundTasks`` job — **after** the create response is
    sent — so the request no longer blocks on the small-model voice pick and the
    up-to-``avatar_gen_timeout_s`` (25s) avatar generation. The create handler
    returns immediately with ``avatar_url=null`` (F1's default renders) and the
    voice unset (the global default voices it); this task fills both in, and the
    web detail surface bounded-polls ``GET /v1/personas/{id}`` until they appear.

    Order is voice-THEN-avatar, preserved from the synchronous path: the avatar
    hook can block for the full wall-clock budget, and the voice pick forwards
    the caller's short-lived bearer token to the voice service, so it must fire
    first while that token is still fresh. ``generate_avatar`` mirrors the old
    ``if body.avatar_url is None`` guard — a user-supplied avatar skips the
    avatar hook entirely (criterion 6) but the voice pick still runs.

    **RLS re-establishment (load-bearing).** A background task runs OUTSIDE the
    request's RLS scope: the auth dependency's ``current_user_id`` contextvar is
    reset at request teardown, and the pool checkout listener (which sets
    ``app.current_user_id`` from that contextvar — middleware/rls_context.py)
    would otherwise see an empty value → fail-closed → the avatar/voice writes
    would silently touch zero rows. So we re-bind the contextvar to the
    request-time ``owner_id`` here, around the writes, exactly as the request
    path does. **Gated to the CLOUD edition** (Spec 33's edition seam): community
    runs a listener-less single-owner SQLite engine with no RLS, so it needs no
    GUC — the same writes simply run unscoped there. Both editions work; only
    cloud sets the scope.

    Every existing fail-soft contract is preserved verbatim: both hooks swallow
    their own errors (avatar → ``avatar_url=null`` + audit; voice → keep the
    default) and never raise, so a background-task failure can never surface to
    the (already-sent) create response.
    """
    edition = getattr(getattr(request.app.state, "config", None), "edition", None)
    reset_token = None
    if edition is Edition.cloud:
        # Re-bind the RLS scope so the pool checkout listener runs
        # set_config('app.current_user_id', owner_id) on every connection these
        # writes touch (set_voice / set_avatar_url open their own engine.begin()).
        reset_token = current_user_id.set(owner_id)
    try:
        await voice_assignment_service.maybe_assign_voice(
            request, owner_id=owner_id, persona_id=persona_id, yaml_str=yaml_str
        )
        if generate_avatar:
            await _maybe_generate_avatar(
                request, owner_id=owner_id, persona_id=persona_id, yaml_str=yaml_str
            )
    finally:
        if reset_token is not None:
            current_user_id.reset(reset_token)


@router.post("", status_code=status.HTTP_201_CREATED, response_model=PersonaDetail)
async def create_persona(
    body: CreatePersonaRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonaDetail:
    """Create a persona from YAML; populate memory stores; return immediately.

    The row + memory chunks are written synchronously (so the persona exists the
    moment this returns), then the response is sent with ``avatar_url=null`` (F1's
    default renders) and any auto-voice unset (the global default voices it). The
    voice auto-pick and the avatar auto-generation — which together added ~30s to
    the critical path — run in a ``BackgroundTasks`` job AFTER the response
    (:func:`_enrich_persona_after_create`), which re-establishes the owner's RLS
    scope before its writes (cloud) and fills in voice + avatar. The web detail
    surface bounded-polls ``GET /v1/personas/{id}`` until they appear.

    Auto-generation only runs when the builder supplied no avatar (D-29-3); a
    user-supplied ``avatar_url`` always wins (criterion 6) and short-circuits the
    background avatar hook. Everything stays fail-soft (D-29-X-fail-soft).
    """
    persona_id = persona_service.create_persona(
        rls_engine=request.app.state.rls_engine,
        embedder=request.app.state.embedder,
        audit_root=request.app.state.audit_root,
        owner_id=user.id,
        yaml_str=body.yaml,
        avatar_url=body.avatar_url,
        # The edition's typed-memory backend (Chroma for community, Postgres for
        # cloud); a hardcoded PostgresBackend has no memory_chunks table on the
        # community SQLite path (Spec 33 D-33-X-memory-chroma-community).
        memory_backend=getattr(request.app.state, "memory_backend", None),
        # R5-D-2: the app-selected store-mutation audit backend (Postgres when
        # multi-worker). None (test overrides) ⇒ the JSONL default.
        audit_logger=getattr(request.app.state, "audit_logger", None),
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="persona.create",
        target=persona_id,
    )
    # R9-012: post-commit sidebar liveness ping — the owner's OTHER tabs/devices
    # soft-refresh their sidebar (data-only; best-effort, never fails the create).
    notifications_service.publish_sidebar_changed(
        getattr(request.app.state, "event_channel", None),
        owner_id=user.id,
        reason="persona.created",
    )
    # Defer voice auto-pick + avatar generation OFF the create critical path.
    # Voice always runs in-process (BackgroundTasks). Avatar generation routes to
    # the DURABLE queue when the cutover flag is on (A0 T9) — owner_id is the
    # authenticated user (server-side, never the request body) — else it runs
    # in-process as before. Either way the response carries ``avatar_url=null``;
    # the avatar appears on a later GET. Both paths stay fail-soft.
    config = request.app.state.config
    job_queue = getattr(request.app.state, "job_queue", None)
    avatar_via_queue = (
        body.avatar_url is None
        and getattr(config, "avatar_via_queue", False)
        and job_queue is not None
    )
    background_tasks.add_task(
        _enrich_persona_after_create,
        request,
        owner_id=user.id,
        persona_id=persona_id,
        yaml_str=body.yaml,
        generate_avatar=body.avatar_url is None and not avatar_via_queue,
    )
    if avatar_via_queue and job_queue is not None:
        enqueue_avatar_generation(job_queue, persona_id=persona_id, owner_id=user.id)
    row = persona_service.get_persona(
        rls_engine=request.app.state.rls_engine, persona_id=persona_id
    )
    return _persona_detail(row, tier_registry=_tier_registry(request))


def _sse(event: str, data: dict[str, object]) -> bytes:
    """Frame one SSE event (mirrors the chat/runs streaming framing; D-P0-sse-reuse)."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _rate_limit_headers(request: Request) -> dict[str, str]:
    """Copy the rate-limit dependency's headers onto a route-built StreamingResponse.

    FastAPI does not auto-merge a dependency's response headers into a
    route-constructed ``StreamingResponse`` — the chat path does the same copy
    (conversations.py) so ``X-RateLimit-*`` appears on the SSE response too.
    """
    decision = getattr(request.state, "rate_limit_decision", None)
    return decision.headers() if decision is not None else {}


def _authoring_stream_response(
    request: Request,
    user: AuthenticatedUser,
    events: AsyncIterator[authoring_service.AuthoringStreamEvent],
    *,
    action: str,
    reason: str,
) -> StreamingResponse:
    """Frame the service's semantic events as SSE; deduct AFTER the terminal draft.

    ``chunk`` → a forming-text frame; ``retry`` → a visible regenerating frame;
    the terminal ``draft`` triggers the post-success credit deduct
    (D-P0-deduct-after-validate / D-08-6) — deliberately NOT in a ``finally``, so
    an aborted (generator cancelled mid-stream) or failed (provider error,
    propagates before the draft) stream yields no terminal draft and deducts
    nothing — then emits the validated-or-errored ``AuthoringDraft`` payload and
    the ``done`` sentinel (mirrors chat). A validation-exhausted draft is a
    delivered draft and DOES charge (D-10-8), unchanged from the blocking path.
    """

    async def _frames() -> AsyncIterator[bytes]:
        async for kind, payload in events:
            if kind == "chunk":
                yield _sse("chunk", {"delta": payload, "is_final": False})
            elif kind == "retry":
                yield _sse("retry", {"reason": payload})
            else:  # "draft" — the single terminal event
                draft = cast("AuthoringDraft", payload)
                _deduct_and_audit(request, user, action, draft.prompt_version, reason=reason)
                yield _sse("draft", draft.model_dump())
                yield _sse("done", {})

    return StreamingResponse(
        _frames(), media_type="text/event-stream", headers=_rate_limit_headers(request)
    )


@router.post(
    "/author",
    responses={200: {"model": AuthoringDraft, "content": {"text/event-stream": {}}}},
    dependencies=[Depends(rate_limit("author"))],
)
async def author_persona(
    body: AuthorPersonaRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    """SSE-stream a DRAFT persona from a description for review (D-10-2, spec P0).

    Streams the model output as it generates (``chunk`` events), then emits the
    validated ``AuthoringDraft`` as the terminal ``draft`` event followed by
    ``done``. Creates NO persona row — the user reviews/refines, then saves via
    ``POST /v1/personas``. The flat authoring credit is deducted ONLY after a
    successful terminal draft (D-P0-deduct-after-validate / D-08-6); the
    pre-flight 402 (D-11-12) + rate-limit run BEFORE streaming begins.
    """
    # Pre-flight credit guard BEFORE streaming (D-11-12 / D-P0-preflight-preserved).
    request.app.state.credits_policy.require_credits(
        rls_engine=request.app.state.rls_engine, user_id=user.id
    )
    backend = require_model_backend(
        request,
        # Spec P9: authoring is a frontier surface; an unset app.state falls
        # back to the POLICY default, never a silent mid downgrade.
        getattr(request.app.state, "authoring_tier", None) or tier_for("authoring"),
    )
    events = authoring_service.stream_authoring_draft(
        backend,
        body.description,
        [name for name, _ in catalog_service.list_tools()],
        [name for name, _ in catalog_service.list_skills()],
        sampling=_authoring_sampling(request),
    )
    return _authoring_stream_response(
        request, user, events, action="persona.author", reason="persona_authoring"
    )


@router.post(
    "/author/refine",
    responses={200: {"model": AuthoringDraft, "content": {"text/event-stream": {}}}},
    dependencies=[Depends(rate_limit("author"))],
)
async def refine_persona(
    body: RefinePersonaRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> StreamingResponse:
    """SSE-stream a refined draft by answering a clarifying question (§4, D-10-2, spec P0).

    Stateless: the request carries ``round`` (refinements already applied); the
    server rejects ``round >= 3`` as the backstop on the 3-round cap (D-10-5)
    BEFORE streaming begins. Streams the same way as ``/author``; deducts the
    flat authoring credit only after the terminal draft.
    """
    # Round backstop + pre-flight 402 BEFORE streaming (D-P0-preflight-preserved).
    if body.round >= _MAX_REFINE_ROUNDS:
        raise RefinementLimitError(
            "refinement limit reached",
            context={"round": str(body.round), "max_rounds": str(_MAX_REFINE_ROUNDS)},
        )
    request.app.state.credits_policy.require_credits(
        rls_engine=request.app.state.rls_engine, user_id=user.id
    )
    backend = require_model_backend(
        request,
        # Spec P9: authoring is a frontier surface; an unset app.state falls
        # back to the POLICY default, never a silent mid downgrade.
        getattr(request.app.state, "authoring_tier", None) or tier_for("authoring"),
    )
    events = authoring_service.stream_refine_authoring_draft(
        backend,
        body.current_yaml,
        body.question,
        body.answer,
        [name for name, _ in catalog_service.list_tools()],
        [name for name, _ in catalog_service.list_skills()],
        sampling=_authoring_sampling(request),
    )
    return _authoring_stream_response(
        request, user, events, action="persona.author_refine", reason="persona_authoring_refine"
    )


@router.post(
    "/recommend-tools",
    response_model=ToolRecommendationResponse,
    dependencies=[Depends(rate_limit("author"))],
)
async def recommend_tools(
    body: AuthorPersonaRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ToolRecommendationResponse:
    """Recommend a ranked tool subset for a persona description (spec 26 T09).

    Authoring-time assist: given the natural-language description, a single
    mid-tier call (D-26-2) returns up to 10 catalog-valid tool recommendations,
    highest-confidence first. Reuses the description-only ``AuthorPersonaRequest``
    body. Deducts the flat authoring credit (a mid-tier LLM call).
    """
    request.app.state.credits_policy.require_credits(
        rls_engine=request.app.state.rls_engine, user_id=user.id
    )
    # Spec P9 (Phase 2 gate ruling): the recommenders are authoring surface →
    # frontier (negligible volume; quality-first), not a hardcoded mid.
    backend = require_model_backend(
        request, getattr(request.app.state, "authoring_tier", None) or tier_for("authoring")
    )
    recommendations = await authoring_service.recommend_tools_for_persona(backend, body.description)
    _deduct_and_audit(
        request,
        user,
        "persona.recommend_tools",
        authoring_service.RECOMMENDER_PROMPT_VERSION,
        reason="persona_tool_recommend",
    )
    return ToolRecommendationResponse(
        recommendations=recommendations,
        prompt_version=authoring_service.RECOMMENDER_PROMPT_VERSION,
    )


@router.post(
    "/recommend-capabilities",
    response_model=ToolRecommendationResponse,
    dependencies=[Depends(rate_limit("author"))],
)
async def recommend_capabilities(
    body: AuthorPersonaRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> ToolRecommendationResponse:
    """Recommend a unified, provider-tagged capability set (spec 27 T10).

    The D-26-10 generalisation of ``/recommend-tools``: one mid-tier call ranks
    built-in tools, skills, and MCP servers together (each tagged with its
    provider), capped at the combined maximum (D-27-13). Deducts the same flat
    authoring credit (a mid-tier LLM call).
    """
    from persona.skills.catalog import BUILTIN_CATALOG

    request.app.state.credits_policy.require_credits(
        rls_engine=request.app.state.rls_engine, user_id=user.id
    )
    # Spec P9: authoring surface → frontier (same ruling as recommend-tools).
    backend = require_model_backend(
        request, getattr(request.app.state, "authoring_tier", None) or tier_for("authoring")
    )
    recommendations = await authoring_service.recommend_capabilities_for_persona(
        backend,
        body.description,
        available_skills=tuple(BUILTIN_CATALOG.skills),
    )
    _deduct_and_audit(
        request,
        user,
        "persona.recommend_capabilities",
        authoring_service.RECOMMENDER_PROMPT_VERSION,
        reason="persona_capability_recommend",
    )
    return ToolRecommendationResponse(
        recommendations=recommendations,
        prompt_version=authoring_service.RECOMMENDER_PROMPT_VERSION,
    )


@router.post(
    "/{persona_id}/tools",
    response_model=PersonaDetail,
    dependencies=[Depends(rate_limit("default"))],
)
async def grant_tool(
    persona_id: str,
    body: GrantToolRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonaDetail:
    """Enable a tool on the persona's allow-list via runtime consent (spec 26 T11).

    Called when the user accepts a runtime tool-gap offer (T10). Adds the tool to
    the persona's ``tools`` list (persisted in the YAML column — no migration)
    and records the grant as a versioned ``persona_self`` self-fact (force +
    confidence ≥ 0.8 + reason, D-26-X-self-facts-consent-write-contract). Returns
    the updated persona detail. Idempotent: re-granting an already-enabled tool
    is a no-op that still returns 200.
    """
    from datetime import UTC, datetime

    tool_consent_service.grant_tool_consent(
        rls_engine=request.app.state.rls_engine,
        embedder=request.app.state.embedder,
        audit_root=request.app.state.audit_root,
        persona_id=persona_id,
        owner_id=user.id,
        tool_name=body.tool_name,
        written_by=user.id,
        now=datetime.now(UTC),
        turn_index=body.turn_index,
        # Edition's typed-memory backend (Chroma community / Postgres cloud) — the
        # self_facts consent audit must not hardcode PostgresBackend on SQLite.
        memory_backend=getattr(request.app.state, "memory_backend", None),
        # R5-D-2: app-selected store-mutation audit backend (JSONL default).
        audit_logger=getattr(request.app.state, "audit_logger", None),
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="persona.tool_grant",
        target=persona_id,
    )
    row = persona_service.get_persona(
        rls_engine=request.app.state.rls_engine, persona_id=persona_id
    )
    return _persona_detail(row, tier_registry=_tier_registry(request))


def _deduct_and_audit(
    request: Request,
    user: AuthenticatedUser,
    action: str,
    prompt_version: str,
    *,
    reason: str,
) -> None:
    """Deduct the flat authoring credit + record a targetless audit event (D-10-8).

    Author/refine create no persona row, so the audit ``target`` is empty; the
    eventual ``POST /v1/personas`` audits ``persona.create`` against the real id.
    """
    request.app.state.credits_policy.deduct(
        rls_engine=request.app.state.rls_engine,
        user_id=user.id,
        amount=request.app.state.config.authoring_credit_cost,
        reason=reason,
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action=action,
        target="",
        metadata={"prompt_version": prompt_version},
    )


@router.get("", response_model=list[PersonaSummary])
async def list_personas(
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
    limit: int = 50,
    offset: int = 0,
) -> list[PersonaSummary]:
    """List the caller's personas (paginated; RLS-scoped)."""
    rls_engine = request.app.state.rls_engine
    rows = persona_service.list_personas(
        rls_engine=rls_engine, limit=min(limit, 200), offset=offset
    )
    # Spec 35: one GROUP-BY for the whole page feeds every card's chat count.
    counts = persona_service.conversation_counts(rls_engine=rls_engine)
    return [
        persona_service.summary_of(r, conversation_count=counts.get(str(r["id"]), 0)) for r in rows
    ]


@router.get("/{persona_id}", response_model=PersonaDetail)
async def get_persona(
    persona_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
) -> PersonaDetail:
    """Get a persona's YAML + metadata (404 if not the caller's)."""
    rls_engine = request.app.state.rls_engine
    row = persona_service.get_persona(rls_engine=rls_engine, persona_id=persona_id)
    count = persona_service.conversation_count_for(rls_engine=rls_engine, persona_id=persona_id)
    return _persona_detail(row, tier_registry=_tier_registry(request), conversation_count=count)


@router.patch("/{persona_id}", response_model=PersonaDetail)
async def update_persona(
    persona_id: str,
    body: UpdatePersonaRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonaDetail:
    """Replace a persona's YAML (re-validated) and re-index its memory."""
    persona_service.update_persona(
        rls_engine=request.app.state.rls_engine,
        embedder=request.app.state.embedder,
        audit_root=request.app.state.audit_root,
        owner_id=user.id,
        persona_id=persona_id,
        yaml_str=body.yaml,
        avatar_url=body.avatar_url,
        # Edition's typed-memory backend (Chroma community / Postgres cloud) — see
        # create_persona; never a hardcoded PostgresBackend on the SQLite path.
        memory_backend=getattr(request.app.state, "memory_backend", None),
        # R5-D-2: app-selected store-mutation audit backend (JSONL default).
        audit_logger=getattr(request.app.state, "audit_logger", None),
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="persona.update",
        target=persona_id,
    )
    row = persona_service.get_persona(
        rls_engine=request.app.state.rls_engine, persona_id=persona_id
    )
    return _persona_detail(row, tier_registry=_tier_registry(request))


@router.patch("/{persona_id}/consent", response_model=PersonaDetail)
async def set_consent(
    persona_id: str,
    body: SetConsentRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonaDetail:
    """Set the persona's auto-dispatch consent (grant / decline / revoke).

    Spec 21 T09 (D-21-2/7/8): only this ``user``-sourced settings write may
    change consent; ``persona_self`` never can. Each transition stamps
    ``consent_updated_at`` and emits an ``AuditEvent`` naming the transition.
    """
    from datetime import UTC, datetime

    consent_service.set_consent(
        rls_engine=request.app.state.rls_engine,
        persona_id=persona_id,
        granted=body.granted,
        now=datetime.now(UTC),
    )
    transition = (
        "grant" if body.granted is True else "decline" if body.granted is False else "revoke"
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action=f"persona.consent.{transition}",
        target=persona_id,
    )
    row = persona_service.get_persona(
        rls_engine=request.app.state.rls_engine, persona_id=persona_id
    )
    return _persona_detail(row, tier_registry=_tier_registry(request))


def _persona_speciality(spec: SkillSpec, consent_state: str) -> PersonaSpecialitySummary:
    """Map a catalog ``SkillSpec`` + server-computed consent state to the response model."""
    prov = spec.provenance
    return PersonaSpecialitySummary(
        name=spec.name,
        description=spec.description,
        when_to_use=spec.when_to_use,
        trust=spec.trust.value,
        requires_consent=spec.trust.requires_consent,
        content_hash=prov.content_hash if prov else None,
        source=prov.source if prov else None,
        source_uri=prov.source_uri if prov else None,
        source_ref=prov.source_ref if prov else None,
        consent_state=consent_state,
    )


@router.get("/{persona_id}/specialities", response_model=list[PersonaSpecialitySummary])
async def list_persona_specialities(
    persona_id: str,
    request: Request,
    _user: AuthenticatedUser = Depends(get_current_user),
) -> list[PersonaSpecialitySummary]:
    """List the specialities catalog with THIS persona's consent state (Spec S3, S3-D-3).

    The catalog facts (tier + ``content_hash``, T1) enriched with the server-computed
    ``consent_state`` per skill — the one security-authoritative bit the client cannot
    derive (it needs the consent store + the current hash). Enablement (the ``skills:``
    declaration) and ``unavailable`` stay client-derived from the edited draft.
    RLS-scoped: a persona the caller does not own → 404.
    """
    rls_engine = request.app.state.rls_engine
    persona_service.get_persona(rls_engine=rls_engine, persona_id=persona_id)  # 404 if not owned
    out: list[PersonaSpecialitySummary] = []
    for spec in catalog_service.list_specialities():
        content_hash = spec.provenance.content_hash if spec.provenance else None
        state = skill_consent_service.consent_state_for(
            rls_engine=rls_engine,
            persona_id=persona_id,
            skill_name=spec.name,
            current_hash=content_hash,
            requires_consent=spec.trust.requires_consent,
        )
        out.append(_persona_speciality(spec, state))
    return out


@router.post("/{persona_id}/skills/{skill_name}/consent", response_model=PersonaSpecialitySummary)
async def set_skill_consent(
    persona_id: str,
    skill_name: str,
    body: SetSkillConsentRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> PersonaSpecialitySummary:
    """Record consent for a community/third-party speciality (Spec S3, S3-D-2).

    The client sends ONLY ``{granted}``. The ``content_hash`` consent binds to and the
    trust tier are resolved SERVER-SIDE from the catalog on every request — never from
    the client (forge-prevention: a stale/forged hash can't bypass the gate or the
    re-gating, S1-D-5; a claimed ``vetted`` tier can't skip the gate, S1-D-3). The
    request model forbids extra fields, so a client that tries to supply either → 422.
    Append-only consent event + an audit row naming the transition.
    """
    rls_engine = request.app.state.rls_engine
    persona_service.get_persona(rls_engine=rls_engine, persona_id=persona_id)  # 404 if not owned
    spec = {s.name: s for s in catalog_service.list_specialities()}.get(skill_name)
    if spec is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="speciality not found")
    if not spec.trust.requires_consent:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="speciality does not require consent",
        )
    content_hash = spec.provenance.content_hash if spec.provenance else None
    if content_hash is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="speciality has no content hash to bind consent to",
        )
    skill_consent_service.record_consent(
        rls_engine=rls_engine,
        persona_id=persona_id,
        skill_name=skill_name,
        content_hash=content_hash,  # SERVER-derived — never the client's
        granted=body.granted,
        now=datetime.now(UTC),
    )
    audit_service.record(
        engine=rls_engine,
        user_id=user.id,
        action=f"persona.skill_consent.{'grant' if body.granted else 'revoke'}",
        target=persona_id,
        metadata={
            "skill_name": skill_name,
            "content_hash": content_hash,
            "trust": spec.trust.value,
        },
    )
    state = skill_consent_service.consent_state_for(
        rls_engine=rls_engine,
        persona_id=persona_id,
        skill_name=skill_name,
        current_hash=content_hash,
        requires_consent=True,
    )
    return _persona_speciality(spec, state)


@router.delete("/{persona_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_persona(
    persona_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Delete a persona + all its conversations and memory (cascade)."""
    persona_service.delete_persona(
        rls_engine=request.app.state.rls_engine,
        persona_id=persona_id,
        workspace_root=getattr(request.app.state, "workspace_root", None),
        owner_id=user.id,
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="persona.delete",
        target=persona_id,
    )
    # R9-012: post-commit sidebar liveness ping (see create_persona).
    notifications_service.publish_sidebar_changed(
        getattr(request.app.state, "event_channel", None),
        owner_id=user.id,
        reason="persona.deleted",
    )
