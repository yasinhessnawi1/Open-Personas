"""Image-generation route (spec 15 T16, §5).

Thin HTTP route over :mod:`persona_api.imagegen.service` (T15 owns the
cap → pre-deduct → backend → persist → refund-on-fail composition). The
route layer's job is the four standard concerns the other Phase-2 routes
already model:

1. **Authentication** — :func:`get_current_user` sets the RLS contextvar
   (D-08-1) so every DB access is structurally tenant-scoped.
2. **Pre-flight RLS persona check** — mirrors :mod:`persona_api.routes.uploads`'
   :func:`_ensure_persona_visible`: a SELECT against the RLS-scoped engine
   returns 0 rows for any cross-tenant persona id, surfaces as
   :class:`PersonaNotFoundError` (→ 404). Blocks cross-tenant generation
   at the route boundary before any credits movement or workspace I/O.
3. **Credits pre-flight gate** — :func:`credits_service.require_credits`
   raises :class:`CreditsExhaustedError` (→ 402) BEFORE the service-layer
   pre-deduct fires, so an exhausted user never burns a deduct + refund
   round trip (the deduct/refund pair is the cost-discipline lock for the
   in-flight failure path, NOT for "user is broke"; D-15-X-pre-deduct-credits
   gate paragraph #3).
4. **Audit (API-layer, second deliberate emission)** — every successful
   generation records ``imagegen.create`` in the API ``audit_log`` (spec-08
   §8.2). T12 already emitted the persona-layer :class:`ToolAuditEvent`
   ("what did this persona do?"); T16 emits the HTTP-layer event ("what
   hit this endpoint?"). Both shapes are deliberate — same pattern as
   Spec 13 uploads where the image_service emits storage-layer audit and
   the route emits API-layer audit.

**Error → HTTP mapping** (gate paragraph #4 + spec §5):

* :class:`ConcurrencyCappedError` → 429 + ``Retry-After`` (via the
  app-level handler in :mod:`persona_api.errors`).
* :class:`ImageGenUnavailableError` → 503 (missing/invalid creds at
  startup or 401/403 from the provider — distinct from "provider rejected
  us" which is 502).
* :class:`ContentRejectedError` → 422 with structured body carrying
  ``reason`` and ``stage`` from the exception context (model-facing
  surface is a graceful refusal; the HTTP client gets a structured
  validation error).
* :class:`ImageProviderError` → 502 (provider failed for non-credential,
  non-moderation reasons — rate limit, transient 5xx, model_not_found,
  timeout, unsupported_option).

**Response payload** (ImageRef shape, Spec 13 contract reused):

::

    {
        "images": [
            {"workspace_path", "media_type", "width", "height", "revised_prompt"},
            ...
        ],
        "provider": "openai",
        "model": "gpt-image-1",
        "latency_ms": 12.5
    }

The client constructs ``GET /v1/personas/:id/uploads/:ref`` from
``workspace_path`` to fetch the bytes (D-15-X-workspace-coordination —
generated images and uploaded images share ONE storage-and-serve path).

References:
    docs/specs/phase2/spec_15/decisions.md gate paragraph; D-15-X-pre-deduct-credits;
    D-15-X-concurrency-cap; D-15-X-workspace-coordination;
    D-15-X-size-rounding (audit captures REQUESTED size, not rounded);
    docs/specs/phase2/spec_15/tasks.md §T16.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, status
from persona.imagegen import (
    ContentRejectedError,
    ImageGenOptions,
    ImageGenUnavailableError,
    ImageProviderError,
)
from persona.imagegen.result import ImageQuality, ImageSize
from persona.schema.persona import Persona
from pydantic import BaseModel, ConfigDict, Field

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.imagegen import service as imagegen_service
from persona_api.middleware.rate_limit import rate_limit
from persona_api.services import audit_service, persona_service

if TYPE_CHECKING:
    from persona.imagegen.protocol import ImageBackend

router = APIRouter(prefix="/v1/personas", tags=["imagegen"])

__all__ = ["ImageGenRequest", "router"]


class ImageGenRequest(BaseModel):
    """Body of ``POST /v1/personas/:id/imagegen``.

    The closed Literal surface on ``size`` and ``quality`` ensures invalid
    values land as 422 Pydantic validation errors before any service-
    layer work happens; ``count`` is bounded by D-15-3 (``le=2``).

    Attributes:
        prompt: The user-supplied text prompt. Required, min length 1.
            The visual_style merge runs at the service layer
            (:func:`persona_api.imagegen.service.generate`) so the prompt
            here is the raw user input — NOT yet merged.
        size: One of the three closed presets per D-15-3. Defaults to
            ``"1024x1024"``. The OpenAI backend rounds non-square presets
            per D-15-X-size-rounding; the audit captures the REQUESTED
            value (this field's literal), not the rounded one.
        count: Number of images to generate. ``Field(ge=1, le=2)`` enforces
            the D-15-3 cap. Defaults to 1.
        quality: One of the two closed presets per D-15-3. Defaults to
            ``"standard"``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt: str = Field(min_length=1)
    size: ImageSize = "1024x1024"
    count: int = Field(default=1, ge=1, le=2)
    quality: ImageQuality = "standard"


def _ensure_persona_visible(request: Request, persona_id: str) -> dict[str, object]:
    """Pre-flight RLS check: persona must be visible under the caller's scope.

    Mirrors :func:`persona_api.routes.uploads._ensure_persona_visible`.
    The RLS-scoped engine returns 0 rows for any cross-tenant persona id,
    surfaces as :class:`PersonaNotFoundError` (→ 404 via the existing
    handler). Returns the persona row so the caller can extract
    ``identity.visual_style`` without a second DB round-trip.
    """
    return persona_service.get_persona(
        rls_engine=request.app.state.rls_engine, persona_id=persona_id
    )


def _extract_visual_style(persona_row: dict[str, object]) -> str | None:
    """Extract ``identity.visual_style`` from a persona row's YAML.

    The DB stores the persona as a YAML string (the canonical authoring
    surface, D-08-8). We parse only to read the optional ``visual_style``
    field — when absent or empty, the service layer's
    :func:`merge_visual_style` falls through to the identity branch (no
    merge) so this returns ``None`` cleanly in that case.

    A malformed YAML row would be a stored-data integrity issue, not a
    client error — surface the original :exc:`ValidationError` rather
    than masking it (Pydantic's handler maps it to 422 which is the right
    HTTP shape for "the data we have for this persona is invalid"; better
    than silently dropping the style).
    """
    yaml_str = str(persona_row.get("yaml", ""))
    if not yaml_str.strip():
        return None
    raw = yaml.safe_load(yaml_str)
    if not isinstance(raw, dict):
        return None
    persona = Persona.model_validate(raw)
    style = persona.identity.visual_style
    if style is None or not style.strip():
        return None
    return style


@router.post(
    "/{persona_id}/imagegen",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("default"))],
)
async def post_imagegen(
    persona_id: str,
    body: ImageGenRequest,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> dict[str, Any]:
    """Generate one or more images for a persona; persist to workspace; audit.

    Returns 201 + an ImageRef-shape payload on success. The bytes land at
    ``{workspace_root}/{user_id}/{persona_id}/uploads/{blake2b}.{ext}``
    (D-13-4 layout reused per D-15-X-workspace-coordination) and are
    fetched via the existing ``GET /v1/personas/:id/uploads/:ref`` route.

    Args:
        persona_id: Persona id from the path; pre-flight RLS-checked so
            cross-tenant ids surface as 404.
        body: Validated :class:`ImageGenRequest` (Pydantic 422 on shape
            violations before we get here).
        request: FastAPI request — used for ``app.state`` access to the
            RLS engine, the workspace root, and the composed image backend.
        user: Authenticated principal from the bearer token.

    Returns:
        ImageRef-shape JSON payload (mirrors the Spec 13 upload response
        but as a list since ``count`` can be 2).

    Raises:
        ImageGenUnavailableError: The provider is not configured at all
            (no ``PERSONA_IMAGEGEN_API_KEY`` at startup) → 503 via the
            app exception handler.
        HTTPException: 502 for upstream provider failures; 422 for
            content rejection (provider moderation or hard-line filter);
            403 for ``ToolNotAllowedError``; 404 for cross-tenant; 402 for
            credits exhaustion; 429 for concurrency cap or rate limit.
    """
    # 1. Pre-flight persona visibility (cross-tenant → 404). Returns the
    #    row so we can pull visual_style without a second DB round-trip.
    persona_row = _ensure_persona_visible(request, persona_id)

    # 2. Provider must be wired at startup. None means the deployment did
    #    not set PERSONA_IMAGEGEN_API_KEY; surface 503 (distinct from
    #    "provider rejected us" 502 — the operator needs to know the
    #    deployment configuration is incomplete, not that the provider
    #    is down).
    backend: ImageBackend | None = getattr(request.app.state, "image_backend", None)
    if backend is None:
        raise ImageGenUnavailableError(
            "image generation backend is not configured",
            context={
                "reason": "backend_not_configured",
                "hint": "set PERSONA_IMAGEGEN_API_KEY at deployment time",
            },
        )

    # 3. Credits pre-flight (402 if exhausted). Distinct from the service-
    #    layer pre-deduct: this gate catches the "user is broke" case
    #    BEFORE any deduct/refund pair is written to the ledger.
    request.app.state.credits_policy.require_credits(
        rls_engine=request.app.state.rls_engine, user_id=user.id
    )

    # 4. Extract the persona's visual_style (optional Pydantic field, T10).
    persona_visual_style = _extract_visual_style(persona_row)

    # 5. Build ImageGenOptions from the validated body. The Literal closed
    #    sets + the count cap already passed at body-validation time so
    #    this constructor never raises ValidationError here in practice.
    options = ImageGenOptions(size=body.size, count=body.count, quality=body.quality)

    # 6. Compose the cap → deduct → backend → persist → refund-on-fail
    #    flow. The service layer owns the cost-discipline locks
    #    (D-15-X-pre-deduct-credits, D-15-X-concurrency-cap,
    #    D-15-X-credit-flow-semantics). Errors propagate through the
    #    HTTP funnel below; ConcurrencyCappedError uses the app handler
    #    (429 + Retry-After).
    try:
        result = await imagegen_service.generate(
            rls_engine=request.app.state.rls_engine,
            credits_policy=request.app.state.credits_policy,
            file_storage=request.app.state.file_storage,
            backend=backend,
            user_id=user.id,
            persona_id=persona_id,
            persona_visual_style=persona_visual_style,
            prompt=body.prompt,
            options=options,
        )
    except ContentRejectedError as exc:
        # 422 with structured body so the client can present a
        # human-friendly refusal. The model-facing shape (when this fires
        # inside the tool factory) is a ToolResult(is_error=True); the
        # HTTP-facing shape is a 422 so the client knows the request was
        # well-formed but the content was refused.
        payload: dict[str, Any] = {
            "error": "content_rejected",
            "detail": exc.message or "content rejected",
        }
        if exc.context:
            payload["context"] = dict(exc.context)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=payload,
        ) from exc
    except ImageGenUnavailableError:
        # Bubble up to the app handler (which maps to 503 — same shape as
        # the "backend not configured" branch above). The provider
        # rejected our credentials at call time (401/403 from the SDK).
        raise
    except ImageProviderError as exc:
        # 502 — upstream failure that is neither credential-related nor
        # moderation-related (rate limit, transient 5xx, model_not_found,
        # timeout, unsupported_option). The client may retry.
        provider_payload: dict[str, Any] = {
            "error": "provider_error",
            "detail": exc.message or "image provider failed",
        }
        if exc.context:
            provider_payload["context"] = dict(exc.context)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=provider_payload,
        ) from exc

    # 7. API-layer audit (SECOND deliberate emission; T12 emitted the
    #    persona-layer ToolAuditEvent inside the tool factory — but the
    #    tool factory is invoked through the runtime, not through this
    #    direct HTTP path, so the persona-layer emission may not fire on
    #    this code path; the API-layer record is the one this route
    #    owns regardless). The audit metadata captures the REQUESTED
    #    size, not the (potentially rounded) provider-mapped size, per
    #    D-15-X-size-rounding — the operator needs to see what the user
    #    asked for, not what the adapter sent over the wire.
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="imagegen.create",
        target=persona_id,
        metadata={
            "provider": result.provider,
            "model": result.model,
            "requested_size": body.size,
            "count": str(len(result.images)),
            "latency_ms": str(result.latency_ms),
        },
    )

    # 8. ImageRef-shape payload (Spec 13 contract reused). The client
    #    constructs GET /v1/personas/:id/uploads/:ref from
    #    ``workspace_path`` to fetch the bytes.
    return {
        "images": [
            {
                "workspace_path": img.workspace_path,
                "media_type": img.media_type,
                "width": img.width,
                "height": img.height,
                "revised_prompt": img.revised_prompt,
            }
            for img in result.images
        ],
        "provider": result.provider,
        "model": result.model,
        "latency_ms": result.latency_ms,
    }
