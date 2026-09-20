"""``persona.imagegen`` — image generation surface (spec 15).

Public surface: the :class:`ImageBackend` Protocol, the boundary types
(:class:`ImageGenOptions`, :class:`GeneratedImage`,
:class:`GenerationResult`), the :class:`ImageBackendConfig` settings,
the domain exceptions, and the :func:`load_image_backend` factory.
Concrete backends (:class:`persona.imagegen.openai_image.OpenAIImageBackend`,
:class:`persona.imagegen.fal_image.FalImageBackend`) are importable for
advanced callers but the recommended entry point is
:func:`load_image_backend` — mirror of the Spec 02
:mod:`persona.backends` public surface.

Subsequent tasks T09 (safety), T11 (_merge), T12 (tool) extend this
surface additively. See ``docs/specs/phase2/spec_15/spec_15_image_generation.md``
for the spec and ``docs/specs/phase2/spec_15/decisions.md`` for D-15-1..5
+ the D-15-X-* micros.
"""

from __future__ import annotations

from persona.imagegen._factory import load_image_backend, load_image_backend_from_env
from persona.imagegen.avatar_prompt import craft_avatar_prompt, wants_generated_portrait
from persona.imagegen.config import ImageBackendConfig, ImageProvider
from persona.imagegen.errors import (
    ContentRejectedError,
    ImageGenError,
    ImageGenUnavailableError,
    ImageProviderError,
    SyntheticPersonaHasNoPortraitError,
)
from persona.imagegen.multi_model_image import (
    AllModelsFailedError,
    AttemptRecord,
    MultiModelImageBackend,
)
from persona.imagegen.nvidia_image import NvidiaImageBackend
from persona.imagegen.protocol import ImageBackend
from persona.imagegen.result import (
    GeneratedImage,
    GenerationResult,
    ImageGenOptions,
    ImageMediaType,
    ImageQuality,
    ImageSize,
)
from persona.imagegen.safety import (
    hash_prompt_for_audit,
    is_hard_line_violation,
)
from persona.imagegen.tool import make_generate_image_tool

__all__ = [
    "AllModelsFailedError",
    "AttemptRecord",
    "ContentRejectedError",
    "GeneratedImage",
    "GenerationResult",
    "ImageBackend",
    "ImageBackendConfig",
    "ImageGenError",
    "ImageGenOptions",
    "ImageGenUnavailableError",
    "ImageMediaType",
    "ImageProvider",
    "ImageProviderError",
    "ImageQuality",
    "ImageSize",
    "MultiModelImageBackend",
    "NvidiaImageBackend",
    "SyntheticPersonaHasNoPortraitError",
    "craft_avatar_prompt",
    "hash_prompt_for_audit",
    "is_hard_line_violation",
    "load_image_backend",
    "load_image_backend_from_env",
    "make_generate_image_tool",
    "wants_generated_portrait",
]
