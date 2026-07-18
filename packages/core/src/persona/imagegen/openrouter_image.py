"""OpenRouter image-generation backend (Spec 22 T09a).

Concrete :class:`persona.imagegen.protocol.ImageBackend` for OpenRouter
image generation. **OpenRouter has no ``/images/generations`` route** —
image generation rides the chat-completions surface: a
``POST {base_url}/chat/completions`` call with a ``modalities`` param, and
the generated image comes back as a base64 **data URL** embedded inside
the assistant message. The backend uses the ``openai`` SDK's
:class:`openai.AsyncOpenAI` client pointed at OpenRouter's ``base_url``,
calling ``client.chat.completions.create(...)`` with ``extra_body``
(R-22-3).

Mirrors :class:`persona.imagegen.openai_image.OpenAIImageBackend` per the
Spec 15 decisions gate paragraph #1 (Spec 02 mirror discipline): the
per-call coercion tables live co-located near the top of this file,
credentials fail fast at construction, provider SDK exceptions are caught
at the adapter boundary and re-raised as :mod:`persona.imagegen.errors`
domain types so callers depend on our types — not on ``openai``.

The backend NEVER writes bytes to disk. It returns the raw decoded image
bytes in :attr:`persona.imagegen.result.GeneratedImage.image_bytes`; the
hosted service layer (Spec 15 T15) persists them to the per-persona
workspace and rewrites the result.

References:
    docs/specs/phase2/spec_22/research.md §R-22-3 (chat-completions
    image-gen surface); decisions.md D-22-9 (401 → unavailable),
    D-22-16 (403 moderation disambiguation), D-22-19 (options →
    image_config coercion). Spec 15 SURFACE invariant: moderation is
    ContentRejectedError, never ImageProviderError.
"""

from __future__ import annotations

import base64
import time
from typing import TYPE_CHECKING, Any

import openai

from persona.imagegen.errors import (
    ContentRejectedError,
    ImageGenUnavailableError,
    ImageProviderError,
)
from persona.imagegen.protocol import ImageBackend as _ImageBackendProtocol
from persona.imagegen.result import (
    GeneratedImage,
    GenerationResult,
    ImageGenOptions,
    ImageMediaType,
)
from persona.logging import get_logger

if TYPE_CHECKING:
    from persona.imagegen.config import ImageBackendConfig

__all__ = ["OpenRouterImageBackend"]


_LOG = get_logger("imagegen.openrouter")


# OpenRouter's image-gen surface lives on chat-completions, so this file
# owns its own default base URL constant rather than importing from
# ``config.DEFAULT_BASE_URLS`` (the config Literal does not yet carry
# ``"openrouter"`` — that is the separate T09b task).
_OPENROUTER_IMAGE_BASE_URL = "https://openrouter.ai/api/v1/"


# Neutral ``ImageGenOptions.size`` → OpenRouter ``image_config`` coercion
# (D-22-19). OpenRouter's chat-completions image-gen takes an
# ``aspect_ratio`` + ``image_size`` pair rather than a ``WxH`` string;
# the square preset maps to the 1K tier and the two non-square presets to
# the 2K tier (their nearest OpenRouter siblings). The neutral requested
# size is echoed verbatim into ``GeneratedImage.width/height`` via
# ``_parse_size`` so the audit log records what the caller asked for.
_SIZE_TO_IMAGE_CONFIG: dict[str, dict[str, str]] = {
    "1024x1024": {"aspect_ratio": "1:1", "image_size": "1K"},
    "1024x1792": {"aspect_ratio": "9:16", "image_size": "2K"},
    "1792x1024": {"aspect_ratio": "16:9", "image_size": "2K"},
}


# Recognised data-URL media types → neutral IANA media type. Unknown
# variants fall back to ``image/png`` with a warning (mirror of
# ``openai_image._media_type_for_format``).
_DATA_URL_MEDIA_TYPES: dict[str, ImageMediaType] = {
    "image/png": "image/png",
    "image/jpeg": "image/jpeg",
    "image/webp": "image/webp",
}


def _parse_size(size: str) -> tuple[int, int]:
    """Parse a ``"WxH"`` size string into ``(width, height)``."""
    width_str, _, height_str = size.partition("x")
    return int(width_str), int(height_str)


def _media_type_for(raw: str) -> ImageMediaType:
    """Map a data-URL media-type token to the neutral IANA media type."""
    known = _DATA_URL_MEDIA_TYPES.get(raw)
    if known is not None:
        return known
    # Defensive fallback — OpenRouter providers emit png/jpeg/webp but a
    # future provider could surface another type. Treat unknown as png
    # (the dominant default) and log.
    _LOG.warning("unknown data-url media type from openrouter", media_type=raw)
    return "image/png"


def _decode_data_url(url: str, model: str) -> tuple[bytes, ImageMediaType]:
    """Decode a ``data:<media_type>;base64,<b64>`` URL into bytes + media type.

    OpenRouter image-gen always returns the image as a base64 data URL
    (R-22-3). A url that is not a well-formed base64 data URL surfaces as
    ``ImageProviderError(reason="transient")`` — never silently empty
    (fail-loud, mirror of ``openai_image._parse_response``).
    """
    prefix = "data:"
    if not url.startswith(prefix) or ";base64," not in url:
        raise ImageProviderError(
            "openrouter returned a non-data-url image",
            context={
                "provider": "openrouter",
                "model": model,
                "reason": "transient",
            },
        )
    header, _, b64 = url.partition(";base64,")
    raw_media_type = header[len(prefix) :]
    media_type = _media_type_for(raw_media_type)
    if not b64:
        raise ImageProviderError(
            "openrouter returned an empty base64 payload",
            context={
                "provider": "openrouter",
                "model": model,
                "reason": "transient",
            },
        )
    try:
        image_bytes = base64.b64decode(b64, validate=True)
    except (ValueError, TypeError) as exc:
        raise ImageProviderError(
            "openrouter returned malformed base64 image data",
            context={
                "provider": "openrouter",
                "model": model,
                "reason": "transient",
            },
        ) from exc
    return image_bytes, media_type


def _extract_usage(response: Any) -> tuple[int, int, float | None]:  # noqa: ANN401 — SDK type
    """Read ``(prompt_tokens, completion_tokens, cost_usd)`` off the response (Spec M3, T3a).

    OpenRouter image-gen rides chat-completions, so the response carries a
    ``usage`` object exactly like a chat turn: ``prompt_tokens`` /
    ``completion_tokens`` (image tokens land in ``completion_tokens``) and, with
    usage accounting opted in, a ``cost`` extra. FAIL-OPEN (mirrors the M2 chat
    parse): a missing / malformed usage yields ``(0, 0, None)`` — pricing
    telemetry never breaks response parsing.
    """
    from persona.backends.openai_compat import (
        _usage_cost_usd,  # noqa: PLC0415 — reuse the M2 parser
    )

    usage_obj = getattr(response, "usage", None)
    if usage_obj is None:
        return 0, 0, None
    prompt = getattr(usage_obj, "prompt_tokens", 0)
    completion = getattr(usage_obj, "completion_tokens", 0)
    prompt_tokens = prompt if isinstance(prompt, int) and not isinstance(prompt, bool) else 0
    completion_tokens = (
        completion if isinstance(completion, int) and not isinstance(completion, bool) else 0
    )
    return max(0, prompt_tokens), max(0, completion_tokens), _usage_cost_usd(usage_obj)


def _extract_image_url(entry: Any) -> str | None:  # noqa: ANN401 — untyped SDK extra
    """Pull the ``image_url.url`` string out of an ``images[]`` entry.

    The entry is the untyped ``message.images`` extra; the SDK may parse
    its nested ``image_url`` as either a dict or an object, so both access
    shapes are handled defensively (R-22-3).
    """
    if isinstance(entry, dict):
        image_url = entry.get("image_url")
    else:
        image_url = getattr(entry, "image_url", None)
    if image_url is None:
        return None
    url = image_url.get("url") if isinstance(image_url, dict) else getattr(image_url, "url", None)
    return url if isinstance(url, str) else None


def _is_moderation_blocked(exc: openai.APIStatusError) -> bool:
    """Detect an OpenRouter moderation rejection shape (D-22-16).

    Mirror of :func:`persona.imagegen.openai_image._is_moderation_blocked`,
    widened to ``APIStatusError`` so it covers both the 400
    (``BadRequestError``) and 403 (``PermissionDeniedError``) moderation
    surfaces. OpenRouter relays the upstream provider's moderation
    rationale; the ``moderation`` / ``flagged`` markers land in ``exc.code``
    or ``exc.body["error"]["code"]`` (SDK-version dependent) and, as a last
    resort, in the rendered message string. Per the Spec 15 SURFACE
    invariant a moderation 400/403 is :class:`ContentRejectedError`, never
    :class:`ImageProviderError`.
    """
    moderation_codes = {"moderation_blocked", "moderation", "flagged"}
    if getattr(exc, "code", None) in moderation_codes:
        return True
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code") in moderation_codes:
            return True
    message = str(exc).lower()
    return "moderation" in message or "flagged" in message


def _moderation_stage(exc: openai.APIStatusError) -> str:
    """Disambiguate input vs output moderation stage (D-22-16).

    Mirror of :func:`persona.imagegen.openai_image._moderation_stage`:
    "generated image" / "output" hints in the message mean post-generation
    rejection; everything else is input-stage. Lands in
    :attr:`ContentRejectedError.context["stage"]`.
    """
    message = str(exc).lower()
    if "generated image" in message or "output" in message:
        return "output"
    return "input"


class OpenRouterImageBackend:
    """Async image-generation backend for OpenRouter (chat-completions surface).

    Implements :class:`persona.imagegen.protocol.ImageBackend`. OpenRouter
    has no dedicated images route, so :meth:`generate` calls
    ``chat.completions.create`` with ``modalities=["image", "text"]`` and
    unpacks the base64 data URL from the assistant message's untyped
    ``images`` extra (R-22-3). Construction fails fast
    (:class:`ImageGenUnavailableError`) when the API key is missing or
    empty — mirroring the Spec 02 construction-time fail-fast discipline
    (D-02-13).
    """

    def __init__(self, config: ImageBackendConfig) -> None:
        """Construct + validate the backend.

        Args:
            config: Image backend configuration. ``config.model`` is the
                OpenRouter model slug (e.g. ``"google/gemini-2.5-flash-
                image-preview"``); ``config.base_url`` overrides the
                default :data:`_OPENROUTER_IMAGE_BASE_URL` (proxy / mock
                server). ``config.provider`` is not inspected here — the
                ``ImageProvider`` Literal does not yet carry
                ``"openrouter"`` (T09b); the factory owns dispatch.

        Raises:
            ImageGenUnavailableError: ``config.api_key`` is ``None`` or
                empty — the env var was unset. Fail-fast per D-02-13.
        """
        if config.api_key is None or not config.api_key.get_secret_value():
            raise ImageGenUnavailableError(
                "missing OpenRouter API key",
                context={"provider": "openrouter"},
            )

        self._config = config
        self._model = config.model
        self._timeout = config.request_timeout_s
        self._base_url = config.base_url or _OPENROUTER_IMAGE_BASE_URL
        self._client = openai.AsyncOpenAI(
            api_key=config.api_key.get_secret_value(),
            base_url=self._base_url,
            timeout=self._timeout,
        )

        _LOG.debug(
            "constructed",
            provider="openrouter",
            model=self._model,
            base_url=self._base_url,
            timeout_s=self._timeout,
        )

    @property
    def provider_name(self) -> str:
        """Return the stable provider identifier ``"openrouter"``."""
        return "openrouter"

    @property
    def model_name(self) -> str:
        """Return the configured OpenRouter model slug."""
        return self._model

    async def generate(
        self,
        prompt: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        """Single-shot image generation against OpenRouter chat-completions.

        Args:
            prompt: The (already merged with ``visual_style`` by T11) text
                description of the image.
            options: Neutral generation knobs. ``None`` means use a
                default :class:`ImageGenOptions`. ``count`` MUST be 1 —
                OpenRouter chat-completions image-gen returns a single
                image per call.

        Returns:
            :class:`GenerationResult` with exactly one :class:`GeneratedImage`
            carrying raw bytes and ``workspace_path`` ``None`` (the service
            layer in T15 owns disk-write).

        Raises:
            ImageGenUnavailableError: provider returned 401 (D-22-9).
            ImageProviderError: ``count > 1``
                (``reason="unsupported_option"``), rate limit
                (``reason="rate_limit"``), model-not-found
                (``reason="model_not_found"``), timeout
                (``reason="timeout"``), bad request
                (``reason="bad_request"``), or transient failure / empty
                or malformed image (``reason="transient"``). The
                discriminator is ``context["reason"]``.
            ContentRejectedError: provider moderation rejected the prompt
                (input stage) or the generated image (output stage) —
                D-22-16. ``context["reason"] = "provider_moderation"`` and
                ``context["stage"]`` carries ``"input"`` / ``"output"``.
        """
        opts = options if options is not None else ImageGenOptions()

        if opts.count > 1:
            # OpenRouter chat-completions image-gen returns one image per
            # call; multi-image is not supported (D-22-19). Fail closed
            # before the SDK is touched.
            raise ImageProviderError(
                f"count={opts.count} is not supported by openrouter image generation "
                "(one image per call)",
                context={
                    "provider": "openrouter",
                    "model": self._model,
                    "reason": "unsupported_option",
                    "count": str(opts.count),
                },
            )

        image_config = _SIZE_TO_IMAGE_CONFIG.get(opts.size, _SIZE_TO_IMAGE_CONFIG["1024x1024"])
        # ``quality`` has no OpenRouter dial — no-op + debug log (D-22-19).
        _LOG.debug(
            "quality has no openrouter dial; ignored",
            provider="openrouter",
            quality=opts.quality,
        )

        extra_body: dict[str, Any] = {
            "modalities": ["image", "text"],
            "image_config": image_config,
            # Spec M3 (T3a): opt into OpenRouter usage accounting so the final
            # usage carries the routed request's real ``cost`` (the D-M2-3 merge,
            # mirrored from chat) — ``openai/gpt-5.4-image-2`` is token-metered,
            # so the billing path prices the actual (basis ``actual_openrouter``).
            "usage": {"include": True},
        }

        started = time.perf_counter()
        try:
            response = await self._client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                extra_body=extra_body,
            )
        except Exception as exc:  # noqa: BLE001 — adapter boundary; classified below
            self._reraise(exc)

        latency_ms = (time.perf_counter() - started) * 1000.0
        return self._parse_response(response, opts.size, latency_ms)

    async def edit(
        self,
        input_image: GeneratedImage,
        instructions: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        """Reserved for v1.x — delegates to the Protocol default.

        Per D-15-X-edit-protocol-reservation, v1 concrete backends do NOT
        override ``edit``; calling it raises :class:`NotImplementedError`
        via the Protocol default. Declared here so the runtime-checkable
        :class:`persona.imagegen.protocol.ImageBackend` Protocol recognises
        this class as a conforming implementation.
        """
        return await _ImageBackendProtocol.edit(self, input_image, instructions, options=options)

    # ------------------------------------------------------------------
    # Response parsing
    # ------------------------------------------------------------------

    def _parse_response(
        self,
        response: Any,  # noqa: ANN401 — SDK return type
        requested_size: str,
        latency_ms: float,
    ) -> GenerationResult:
        """Unpack OpenRouter's chat-completions response into a :class:`GenerationResult`.

        The image rides the assistant message's untyped ``images`` extra
        (R-22-3): ``message.images == [{"type": "image_url", "image_url":
        {"url": "data:image/png;base64,..."}}]``. The extra is accessed via
        ``msg.model_extra`` (the pydantic catch-all) with a ``getattr``
        fallback. Any text in ``message.content`` is DISCARDED with a debug
        log — ``GenerationResult`` stays image-centric in v0.1
        (decision F-residue). Width / height echo the neutral requested
        size (D-22-19); OpenRouter does not echo per-image dims.

        Args:
            response: The raw SDK chat-completion response object.
            requested_size: The neutral ``"WxH"`` size the caller asked
                for (echoed into the image dims).
            latency_ms: Wall-clock latency in milliseconds.

        Returns:
            :class:`GenerationResult` with exactly one image.
        """
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise ImageProviderError(
                "openrouter returned no choices",
                context={
                    "provider": "openrouter",
                    "model": self._model,
                    "reason": "transient",
                },
            )

        message = getattr(choices[0], "message", None)
        images = self._extract_images(message)
        if not images:
            raise ImageProviderError(
                "openrouter response carried no images",
                context={
                    "provider": "openrouter",
                    "model": self._model,
                    "reason": "transient",
                },
            )

        # Text residue (Gemini-class models) — discard, image-centric v0.1.
        content = getattr(message, "content", None)
        if isinstance(content, str) and content:
            _LOG.debug(
                "discarding text residue from openrouter image response",
                provider="openrouter",
                model=self._model,
                text_len=len(content),
            )

        url = _extract_image_url(images[0])
        if url is None:
            raise ImageProviderError(
                "openrouter image entry missing image_url.url",
                context={
                    "provider": "openrouter",
                    "model": self._model,
                    "reason": "transient",
                },
            )

        image_bytes, media_type = _decode_data_url(url, self._model)
        width, height = _parse_size(requested_size)
        generated = GeneratedImage(
            image_bytes=image_bytes,
            workspace_path=None,
            media_type=media_type,
            width=width,
            height=height,
            revised_prompt=None,
        )

        # Spec M3 (T3a): surface the token usage + the OpenRouter real-cost actual
        # for the billing path. Fail-open (mirrors the M2 chat parse): missing /
        # malformed usage degrades to ``0`` tokens / ``None`` cost, never raises.
        prompt_tokens, completion_tokens, cost_usd = _extract_usage(response)

        return GenerationResult(
            images=[generated],
            provider="openrouter",
            model=self._model,
            latency_ms=latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
        )

    @staticmethod
    def _extract_images(message: Any) -> list[Any]:  # noqa: ANN401 — untyped SDK extra
        """Read the untyped ``images`` extra off the assistant message.

        ``images`` is NOT a typed attribute on the openai SDK's
        ``ChatCompletionMessage``; it lands in the pydantic catch-all
        ``model_extra`` (``dict[str, Any] | None``). A ``getattr`` fallback
        covers SDK shapes that surface it as a plain attribute (R-22-3).
        Returns an empty list when absent or not a list.
        """
        if message is None:
            return []
        model_extra = getattr(message, "model_extra", None)
        images: Any = None
        if isinstance(model_extra, dict):
            images = model_extra.get("images")
        if images is None:
            images = getattr(message, "images", None)
        return images if isinstance(images, list) else []

    # ------------------------------------------------------------------
    # Error mapping
    # ------------------------------------------------------------------

    def _reraise(self, exc: BaseException) -> Any:  # noqa: ANN401 — re-raises
        """Map an ``openai`` SDK exception to a :mod:`persona.imagegen.errors` domain type.

        Mirrors :meth:`persona.imagegen.openai_image.OpenAIImageBackend._reraise`.
        Per the Spec 15 SURFACE invariant a ``moderation`` ``BadRequestError``
        / permission rejection lands as :class:`ContentRejectedError` (not
        :class:`ImageProviderError`) so callers branching on safety vs
        transient failure stay disambiguated (D-22-16). 401 maps to
        :class:`ImageGenUnavailableError` (D-22-9).
        """
        provider = "openrouter"
        model = self._model

        if isinstance(exc, openai.AuthenticationError):
            raise ImageGenUnavailableError(
                str(exc),
                context={"provider": provider},
            ) from exc

        if isinstance(exc, openai.RateLimitError):
            retry_after = _extract_retry_after_s(
                getattr(getattr(exc, "response", None), "headers", None)
            )
            ctx: dict[str, str] = {"provider": provider, "reason": "rate_limit"}
            if retry_after is not None:
                ctx["retry_after_s"] = retry_after
            raise ImageProviderError(str(exc), context=ctx) from exc

        if isinstance(exc, openai.NotFoundError):
            raise ImageProviderError(
                str(exc),
                context={
                    "provider": provider,
                    "model": model,
                    "reason": "model_not_found",
                },
            ) from exc

        if isinstance(exc, openai.PermissionDeniedError):
            # A 403 is moderation when the rationale says so; otherwise it
            # is a credential/permission failure → unavailable (D-22-16 +
            # D-22-9). Moderation 403 must be ContentRejectedError.
            if _is_moderation_blocked(exc):
                stage = _moderation_stage(exc)
                raise ContentRejectedError(
                    str(exc),
                    context={
                        "provider": provider,
                        "reason": "provider_moderation",
                        "stage": stage,
                    },
                ) from exc
            raise ImageGenUnavailableError(
                str(exc),
                context={"provider": provider},
            ) from exc

        if isinstance(exc, openai.BadRequestError):
            if _is_moderation_blocked(exc):
                stage = _moderation_stage(exc)
                raise ContentRejectedError(
                    str(exc),
                    context={
                        "provider": provider,
                        "reason": "provider_moderation",
                        "stage": stage,
                    },
                ) from exc
            raise ImageProviderError(
                str(exc),
                context={
                    "provider": provider,
                    "model": model,
                    "reason": "bad_request",
                },
            ) from exc

        if isinstance(exc, openai.APITimeoutError | openai.APIConnectionError):
            raise ImageProviderError(
                str(exc),
                context={"provider": provider, "reason": "timeout"},
            ) from exc

        # Anything else — unmapped SDK errors land as transient so the
        # caller can decide whether to retry. ``underlying`` carries the
        # original type name for observability.
        raise ImageProviderError(
            str(exc),
            context={
                "provider": provider,
                "model": model,
                "reason": "transient",
                "underlying": type(exc).__name__,
            },
        ) from exc


def _extract_retry_after_s(headers: Any) -> str | None:  # noqa: ANN401 — SDK type
    """Return ``retry-after`` header value as a string, or ``None``.

    Mirror of :func:`persona.imagegen.openai_image._extract_retry_after_s`
    (D-02-8): the header is the only source — we never invent a default.
    """
    if headers is None:
        return None
    try:
        value = headers.get("retry-after") if hasattr(headers, "get") else None
    except (AttributeError, TypeError):
        return None
    if value is None:
        return None
    return str(value)
