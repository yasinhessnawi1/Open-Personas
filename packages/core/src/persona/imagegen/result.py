"""Boundary types crossing the :class:`ImageBackend` surface (spec 15).

Frozen Pydantic v2 models with ``extra="forbid"`` per
D-15-X-pydantic-boundary-types (six-spec precedent
D-01-12 / D-02-2 / D-03-3 / D-05-9 / D-06-1 / D-13-X-now corrects spec
§4's ``@dataclass`` sketches). These shapes are returned by
:class:`persona.imagegen.protocol.ImageBackend` implementations and
consumed by the persona-api image-gen service (spec 15 T15), the audit
logger (:class:`persona.tools.audit.ToolAuditLogger`), and the HTTP
response payload (spec 15 T16).

The :class:`GeneratedImage.image_bytes` / ``workspace_path`` split is
the Phase-2-surfaced division of labour between the backend layer (which
returns raw bytes in memory) and the API service layer (which persists
the bytes to the per-persona workspace via
``persona.tools.workspace.resolve_sandbox_path`` and rewrites the model
with ``workspace_path`` populated + ``image_bytes`` zeroed for the
response envelope).

References:
    docs/specs/phase2/spec_15/decisions.md (D-15-3, D-15-X-pydantic-
    boundary-types, D-15-X-size-rounding).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = [
    "GeneratedImage",
    "GenerationResult",
    "ImageGenOptions",
    "ImageMediaType",
    "ImageQuality",
    "ImageSize",
]

ImageSize = Literal["1024x1024", "1024x1792", "1792x1024"]
"""Allowed neutral size presets (D-15-3).

The OpenAI backend rounds ``1024x1792`` → ``1024x1536`` and
``1792x1024`` → ``1536x1024`` at the wire (D-15-X-size-rounding); the
fal backend passes them through as ``{width, height}`` pairs.
"""

ImageQuality = Literal["standard", "high"]
"""Allowed neutral quality presets (D-15-3).

The OpenAI backend maps ``standard`` → ``medium``; the fal backend has
no quality dial (Flux 1.1 [pro] is a single-step optimised model) and
treats both values as a no-op + debug log.
"""

ImageMediaType = Literal["image/png", "image/jpeg", "image/webp"]
"""IANA media type carried alongside the generated image bytes."""


class ImageGenOptions(BaseModel):
    """Neutral options the caller passes to :meth:`ImageBackend.generate`.

    The closed set of values keeps the model from accidentally tuning
    provider-specific dials and gives the cost-discipline layer
    (D-15-X-pre-deduct-credits) a small, predictable surface to price
    against.

    Attributes:
        size: Output dimension preset. Defaults to the square 1024×1024
            preset.
        count: Number of images per call. Capped at 2 for v0.1 per
            D-15-3 cost containment (single tool invocation bounded to
            $0.022–$0.084 on OpenAI medium / $0.08 flat on fal).
        quality: Output quality preset. Defaults to ``standard``.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    size: ImageSize = "1024x1024"
    count: int = Field(default=1, ge=1, le=4)
    quality: ImageQuality = "standard"


class GeneratedImage(BaseModel):
    """One generated image as returned through the backend / service split.

    Two-stage population (Phase-2 surface split):

    * The backend layer populates :attr:`image_bytes` with the raw
      decoded bytes (OpenAI returns ``b64_json``; fal returns a CDN URL
      that the adapter downloads in the same call). :attr:`workspace_path`
      is ``None`` at this stage — the backend never touches disk.
    * The API service layer (spec 15 T15) writes the bytes to the
      per-persona workspace via ``resolve_sandbox_path`` + ``O_NOFOLLOW``,
      then ``model_copy(update=...)`` produces a sibling model with
      :attr:`workspace_path` set to the relative path under the persona
      sandbox (e.g. ``"uploads/<blake2b>.<ext>"``) and :attr:`image_bytes`
      zeroed so the response envelope does not double-carry the payload.

    Attributes:
        image_bytes: Raw decoded image bytes. Backend populates;
            service zeroes (``b""``) after persisting.
        workspace_path: Relative path under
            ``{workspace_root}/{owner_id}/{persona_id}/`` once the
            service has persisted the bytes (D-13-4 layout reused per
            D-15-X-workspace-coordination). ``None`` while the bytes are
            still in-memory inside the backend boundary.
        media_type: IANA media type of the bytes (drives extension +
            content-type at the serve endpoint).
        width: Image width in pixels (echo of the provider response).
        height: Image height in pixels.
        revised_prompt: The provider's rewrite of the input prompt where
            available (OpenAI ``revised_prompt`` / fal ``prompt``).
            Preserved for the audit log so the operator can see what the
            provider actually generated against.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    image_bytes: bytes = b""
    workspace_path: str | None = None
    media_type: ImageMediaType
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    revised_prompt: str | None = None


class GenerationResult(BaseModel):
    """Aggregate result of a single :meth:`ImageBackend.generate` call.

    Attributes:
        images: One or more :class:`GeneratedImage` instances; ordering
            matches the provider response.
        provider: Provider identifier (``"openai"`` or ``"fal"`` for
            v0.1; D-15-1).
        model: Echo of the model name the backend used (e.g.
            ``"gpt-image-1"``, ``"fal-ai/flux-pro/v1.1"``).
        latency_ms: Wall-clock time from request send to response
            complete, measured client-side via :func:`time.perf_counter`
            (same convention as :class:`persona.backends.types.ChatResponse`).
        prompt_tokens: Prompt tokens the generation consumed (Spec M3, T3a) —
            token-metered image models (``openai/gpt-5.4-image-2`` via
            OpenRouter, which rides chat-completions) report usage. ``0`` for
            backends that do not surface usage (per-image-priced providers).
        completion_tokens: Completion (image) tokens the generation emitted
            (Spec M3, T3a). ``0`` when the backend reports no usage.
        cost_usd: Response-side ACTUAL cost in USD (Spec M3, T3a) — the
            OpenRouter ``usage.cost`` when usage accounting is opted in
            (``usage: {"include": true}``), else ``None``. Mirrors
            :attr:`persona.backends.types.TokenUsage.cost_usd` (D-M2-3); the
            billing path prefers it (basis ``actual_openrouter``) and falls back
            to a resolver estimate over the token counts when absent.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    images: list[GeneratedImage] = Field(min_length=1)
    provider: str
    model: str
    latency_ms: float = Field(ge=0.0)
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None, ge=0.0)
