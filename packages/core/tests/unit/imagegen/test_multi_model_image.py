"""Tests for ``persona.imagegen.multi_model_image`` (Spec 20 T16).

Asserts the D-20-9 / D-20-10 / D-20-12 / D-20-14 / D-20-15 invariants
adapted for the Spec 15 :class:`ImageBackend` Protocol and its error
hierarchy. The critical Spec-15-specific invariant — ``ContentRejectedError``
SURFACES and never cross-provider laundered — has its own block of tests
(:class:`TestContentRejectedSurface`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest
from loguru import logger as _loguru_logger
from persona.errors import AuthenticationError, PersonaError
from persona.imagegen.errors import (
    ContentRejectedError,
    ImageGenUnavailableError,
    ImageProviderError,
)
from persona.imagegen.multi_model_image import (
    AllModelsFailedError,
    AttemptRecord,
    MultiModelImageBackend,
)
from persona.imagegen.result import (
    GeneratedImage,
    GenerationResult,
    ImageGenOptions,
)

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence


# ---------------------------------------------------------------------
# Helpers — scripted ImageBackend doubles
# ---------------------------------------------------------------------


def _make_result(provider: str = "test", model: str = "m") -> GenerationResult:
    return GenerationResult(
        images=[
            GeneratedImage(
                image_bytes=b"\x89PNG",
                media_type="image/png",
                width=1024,
                height=1024,
            )
        ],
        provider=provider,
        model=model,
        latency_ms=1.0,
    )


class _ScriptedBackend:
    """Minimal :class:`ImageBackend` double driven by a script of outcomes.

    Each call pops the next outcome. ``Exception`` instances are raised;
    :class:`GenerationResult` instances are returned. The doubles record
    every call for inspection.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        script: Sequence[Exception | GenerationResult],
    ) -> None:
        self._provider = provider
        self._model = model
        self._script: list[Exception | GenerationResult] = list(script)
        self.calls: list[tuple[str, ImageGenOptions | None]] = []

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def model_name(self) -> str:
        return self._model

    async def generate(
        self,
        prompt: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        self.calls.append((prompt, options))
        if not self._script:
            raise AssertionError(
                f"_ScriptedBackend({self._provider}/{self._model}) called too often"
            )
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def edit(self, *args: Any, **kwargs: Any) -> GenerationResult:  # noqa: ANN401
        raise NotImplementedError("edit not supported in v1")


# Shorthand factories.
def _rate_limit(retry_after_s: str | None = None) -> ImageProviderError:
    ctx: dict[str, str] = {"provider": "p", "reason": "rate_limit"}
    if retry_after_s is not None:
        ctx["retry_after_s"] = retry_after_s
    return ImageProviderError("rate limited", context=ctx)


def _credits_expired() -> ImageProviderError:
    return ImageProviderError(
        "402 paid trial expired",
        context={"provider": "nvidia", "reason": "credits_expired"},
    )


def _model_not_found() -> ImageProviderError:
    return ImageProviderError(
        "no such model",
        context={"provider": "p", "model": "m", "reason": "model_not_found"},
    )


def _transient() -> ImageProviderError:
    return ImageProviderError(
        "5xx",
        context={"provider": "p", "reason": "transient"},
    )


def _content_rejected(stage: str = "input") -> ContentRejectedError:
    return ContentRejectedError(
        "moderation",
        context={"provider": "p", "reason": "provider_moderation", "stage": stage},
    )


def _provider_credential_missing() -> PersonaError:
    """Synthesize a :class:`ProviderCredentialMissingError` shape (T11 class not yet imported)."""

    class ProviderCredentialMissingError(PersonaError):
        pass

    return ProviderCredentialMissingError(
        "missing key",
        context={"provider": "openai", "env_var": "PERSONA_OPENAI_API_KEY"},
    )


# ---------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------


class TestConstruction:
    def test_empty_backends_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="at least one backend"):
            MultiModelImageBackend([])

    def test_length_one_works(self) -> None:
        backend = _ScriptedBackend(provider="openai", model="gpt-image-1", script=[])
        wrapper = MultiModelImageBackend([backend], tier_name="imagegen")
        assert wrapper.provider_name == "openai"
        assert wrapper.model_name == "gpt-image-1"

    def test_properties_echo_primary_backend(self) -> None:
        primary = _ScriptedBackend(provider="nvidia", model="flux", script=[])
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[])
        wrapper = MultiModelImageBackend([primary, secondary])
        # Primary's identity is what the wrapper reports — concrete
        # backend's GenerationResult.provider tells which actually fired.
        assert wrapper.provider_name == "nvidia"
        assert wrapper.model_name == "flux"


# ---------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------


class TestHappyPaths:
    @pytest.mark.asyncio
    async def test_single_backend_success(self) -> None:
        result = _make_result(provider="nvidia", model="flux")
        backend = _ScriptedBackend(provider="nvidia", model="flux", script=[result])
        wrapper = MultiModelImageBackend([backend])

        out = await wrapper.generate("a cat")
        assert out is result
        assert len(backend.calls) == 1

    @pytest.mark.asyncio
    async def test_primary_succeeds_no_fallback(self) -> None:
        primary_result = _make_result(provider="nvidia", model="flux")
        primary = _ScriptedBackend(provider="nvidia", model="flux", script=[primary_result])
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[])
        wrapper = MultiModelImageBackend([primary, secondary])

        out = await wrapper.generate("a cat")
        assert out is primary_result
        assert len(primary.calls) == 1
        # Secondary never touched.
        assert secondary.calls == []


# ---------------------------------------------------------------------
# Fallback paths
# ---------------------------------------------------------------------


class TestFallback:
    @pytest.mark.asyncio
    async def test_rate_limit_retries_then_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Both attempts on primary fail; secondary succeeds.
        monkeypatch.setattr("persona.imagegen.multi_model_image.asyncio.sleep", _no_sleep)
        primary = _ScriptedBackend(
            provider="nvidia",
            model="flux",
            script=[_rate_limit(), _rate_limit()],
        )
        secondary_result = _make_result(provider="fal", model="flux-pro")
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[secondary_result])
        wrapper = MultiModelImageBackend([primary, secondary])

        out = await wrapper.generate("a cat")
        assert out is secondary_result
        # D-20-10 N=1 — primary called twice (initial + 1 retry).
        assert len(primary.calls) == 2
        assert len(secondary.calls) == 1

    @pytest.mark.asyncio
    async def test_credits_expired_immediate_fallback(self) -> None:
        # NVIDIA 402 mapped reason="credits_expired" per R-20-3 → FALLBACK-NO-RETRY.
        primary = _ScriptedBackend(
            provider="nvidia",
            model="flux",
            script=[_credits_expired()],
        )
        secondary_result = _make_result(provider="fal", model="flux-pro")
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[secondary_result])
        wrapper = MultiModelImageBackend([primary, secondary])

        out = await wrapper.generate("a cat")
        assert out is secondary_result
        # No retry — primary called exactly once.
        assert len(primary.calls) == 1

    @pytest.mark.asyncio
    async def test_authentication_error_fallback_no_retry(self) -> None:
        # D-20-12: cross-provider AuthenticationError → SKIP-AND-FALLBACK.
        primary = _ScriptedBackend(
            provider="openai",
            model="gpt-image-1",
            script=[AuthenticationError("bad key", context={"provider": "openai"})],
        )
        secondary_result = _make_result(provider="fal", model="flux-pro")
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[secondary_result])
        wrapper = MultiModelImageBackend([primary, secondary])

        out = await wrapper.generate("a cat")
        assert out is secondary_result
        assert len(primary.calls) == 1

    @pytest.mark.asyncio
    async def test_image_gen_unavailable_fallback_no_retry(self) -> None:
        # OpenAI/Spec 15 backends map 401/403 → ImageGenUnavailableError.
        primary = _ScriptedBackend(
            provider="openai",
            model="gpt-image-1",
            script=[
                ImageGenUnavailableError("401", context={"provider": "openai"}),
            ],
        )
        secondary_result = _make_result(provider="fal", model="flux-pro")
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[secondary_result])
        wrapper = MultiModelImageBackend([primary, secondary])

        out = await wrapper.generate("a cat")
        assert out is secondary_result
        assert len(primary.calls) == 1

    @pytest.mark.asyncio
    async def test_provider_credential_missing_fallback_no_retry(self) -> None:
        # D-20-15: ProviderCredentialMissingError at call time → FALLBACK-NO-RETRY.
        primary = _ScriptedBackend(
            provider="nvidia",
            model="flux",
            script=[_provider_credential_missing()],
        )
        secondary_result = _make_result(provider="fal", model="flux-pro")
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[secondary_result])
        wrapper = MultiModelImageBackend([primary, secondary])

        out = await wrapper.generate("a cat")
        assert out is secondary_result
        assert len(primary.calls) == 1

    @pytest.mark.asyncio
    async def test_model_not_found_fallback_no_retry(self) -> None:
        primary = _ScriptedBackend(
            provider="openai", model="gpt-image-1", script=[_model_not_found()]
        )
        secondary_result = _make_result(provider="fal", model="flux-pro")
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[secondary_result])
        wrapper = MultiModelImageBackend([primary, secondary])

        out = await wrapper.generate("a cat")
        assert out is secondary_result
        assert len(primary.calls) == 1

    @pytest.mark.asyncio
    async def test_rate_limit_long_retry_after_skips_retry(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # D-20-9 row 3: Retry-After > 2s → FALLBACK-NO-RETRY (no N=1).
        monkeypatch.setattr("persona.imagegen.multi_model_image.asyncio.sleep", _no_sleep)
        primary = _ScriptedBackend(
            provider="nvidia",
            model="flux",
            script=[_rate_limit(retry_after_s="10")],
        )
        secondary_result = _make_result(provider="fal", model="flux-pro")
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[secondary_result])
        wrapper = MultiModelImageBackend([primary, secondary])

        out = await wrapper.generate("a cat")
        assert out is secondary_result
        # Skipped retry — primary called exactly once.
        assert len(primary.calls) == 1

    @pytest.mark.asyncio
    async def test_transient_error_uses_retry(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr("persona.imagegen.multi_model_image.asyncio.sleep", _no_sleep)
        primary_result = _make_result(provider="nvidia", model="flux")
        primary = _ScriptedBackend(
            provider="nvidia",
            model="flux",
            script=[_transient(), primary_result],
        )
        wrapper = MultiModelImageBackend([primary])

        out = await wrapper.generate("a cat")
        assert out is primary_result
        # First call failed; second (retry) succeeded.
        assert len(primary.calls) == 2


# ---------------------------------------------------------------------
# ContentRejectedError SURFACE invariant (Spec 15 critical)
# ---------------------------------------------------------------------


class TestContentRejectedSurface:
    """The Spec 15 invariant — ContentRejectedError ALWAYS surfaces."""

    @pytest.mark.asyncio
    async def test_primary_content_rejected_surfaces(self) -> None:
        rejected = _content_rejected()
        primary = _ScriptedBackend(provider="openai", model="gpt-image-1", script=[rejected])
        secondary_result = _make_result(provider="fal", model="flux-pro")
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[secondary_result])
        wrapper = MultiModelImageBackend([primary, secondary])

        with pytest.raises(ContentRejectedError) as excinfo:
            await wrapper.generate("an unsafe prompt")
        assert excinfo.value is rejected
        # CRITICAL — secondary NEVER tried.
        assert secondary.calls == []

    @pytest.mark.asyncio
    async def test_content_rejected_never_falls_back_even_if_secondary_would_succeed(
        self,
    ) -> None:
        # The whole point: even though secondary WOULD have served, the
        # refusal is about the prompt — laundering it across vendors is
        # the antipattern this test guards.
        primary = _ScriptedBackend(
            provider="openai", model="gpt-image-1", script=[_content_rejected()]
        )
        secondary_result = _make_result(provider="fal", model="flux-pro")
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[secondary_result])
        tertiary_result = _make_result(provider="nvidia", model="flux")
        tertiary = _ScriptedBackend(provider="nvidia", model="flux", script=[tertiary_result])
        wrapper = MultiModelImageBackend([primary, secondary, tertiary])

        with pytest.raises(ContentRejectedError):
            await wrapper.generate("an unsafe prompt")
        assert secondary.calls == []
        assert tertiary.calls == []

    @pytest.mark.asyncio
    async def test_content_rejected_on_output_stage_also_surfaces(self) -> None:
        # fal.ai output-moderation rejection — same surface discipline
        # as input rejection.
        primary = _ScriptedBackend(
            provider="fal",
            model="flux-pro",
            script=[_content_rejected(stage="output")],
        )
        secondary = _ScriptedBackend(provider="openai", model="gpt-image-1", script=[])
        wrapper = MultiModelImageBackend([primary, secondary])

        with pytest.raises(ContentRejectedError) as excinfo:
            await wrapper.generate("borderline prompt")
        assert excinfo.value.context["stage"] == "output"
        assert secondary.calls == []

    @pytest.mark.asyncio
    async def test_content_rejected_after_fallback_still_surfaces(self) -> None:
        # Primary transient-fails, secondary content-rejects: surface.
        # Tertiary must never be tried.
        primary = _ScriptedBackend(provider="nvidia", model="flux", script=[_credits_expired()])
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[_content_rejected()])
        tertiary_result = _make_result(provider="openai", model="gpt-image-1")
        tertiary = _ScriptedBackend(
            provider="openai", model="gpt-image-1", script=[tertiary_result]
        )
        wrapper = MultiModelImageBackend([primary, secondary, tertiary])

        with pytest.raises(ContentRejectedError):
            await wrapper.generate("borderline prompt")
        assert tertiary.calls == []


# ---------------------------------------------------------------------
# Exhaustion path
# ---------------------------------------------------------------------


class TestExhaustion:
    @pytest.mark.asyncio
    async def test_all_rate_limit_raises_all_models_failed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr("persona.imagegen.multi_model_image.asyncio.sleep", _no_sleep)
        primary = _ScriptedBackend(
            provider="nvidia", model="flux", script=[_rate_limit(), _rate_limit()]
        )
        secondary = _ScriptedBackend(
            provider="fal", model="flux-pro", script=[_rate_limit(), _rate_limit()]
        )
        tertiary = _ScriptedBackend(
            provider="openai",
            model="gpt-image-1",
            script=[_rate_limit(), _rate_limit()],
        )
        wrapper = MultiModelImageBackend([primary, secondary, tertiary], tier_name="imagegen")

        with pytest.raises(AllModelsFailedError) as excinfo:
            await wrapper.generate("a cat")
        ctx = excinfo.value.context
        assert ctx["tier"] == "imagegen"
        assert ctx["attempt_count"] == "3"
        # ImageProviderError class name appears in the attempts dump.
        assert "ImageProviderError" in ctx["attempts"]
        assert ctx["final_error_class"] == "ImageProviderError"

    @pytest.mark.asyncio
    async def test_mixed_error_classes_recorded_in_attempts(self) -> None:
        primary = _ScriptedBackend(
            provider="nvidia",
            model="flux",
            script=[_credits_expired()],
        )
        secondary = _ScriptedBackend(
            provider="fal",
            model="flux-pro",
            script=[AuthenticationError("bad key", context={"provider": "fal"})],
        )
        tertiary = _ScriptedBackend(
            provider="openai",
            model="gpt-image-1",
            script=[_model_not_found()],
        )
        wrapper = MultiModelImageBackend([primary, secondary, tertiary])

        with pytest.raises(AllModelsFailedError) as excinfo:
            await wrapper.generate("a cat")
        attempts_str = excinfo.value.context["attempts"]
        # All three error class names present.
        assert "ImageProviderError" in attempts_str
        assert "AuthenticationError" in attempts_str

    @pytest.mark.asyncio
    async def test_tier_default_when_omitted(self) -> None:
        primary = _ScriptedBackend(provider="nvidia", model="flux", script=[_credits_expired()])
        wrapper = MultiModelImageBackend([primary])
        with pytest.raises(AllModelsFailedError) as excinfo:
            await wrapper.generate("a cat")
        assert excinfo.value.context["tier"] == "imagegen"


# ---------------------------------------------------------------------
# D-20-14 atomic generate / DISCARD + RESTART
# ---------------------------------------------------------------------


class TestAtomicGenerate:
    """Per D-20-14: generate() is atomic — complete result OR raise."""

    @pytest.mark.asyncio
    async def test_fallback_restarts_clean_no_partial_state(self) -> None:
        # Primary "starts producing" then raises — fal/NIM/OpenAI image
        # APIs are one-shot HTTP, so this models a backend that touched
        # external state before raising. The wrapper MUST still return
        # the secondary's complete result and never expose primary's
        # half-state to the caller.
        partial_marker: dict[str, Any] = {"primary_partial_emitted": False}

        class _PartialThenFailBackend:
            provider_name = "nvidia"
            model_name = "flux"

            async def generate(
                self,
                prompt: str,  # noqa: ARG002
                *,
                options: ImageGenOptions | None = None,  # noqa: ARG002
            ) -> GenerationResult:
                partial_marker["primary_partial_emitted"] = True
                raise _transient()

            async def edit(self, *args: Any, **kwargs: Any) -> GenerationResult:  # noqa: ANN401
                raise NotImplementedError

        secondary_result = _make_result(provider="fal", model="flux-pro")
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[secondary_result])
        wrapper = MultiModelImageBackend(
            [_PartialThenFailBackend(), secondary], max_retries_per_backend=0
        )

        out = await wrapper.generate("a cat")
        # Primary's partial state is discarded; secondary's complete
        # result is returned untouched.
        assert out is secondary_result
        assert partial_marker["primary_partial_emitted"] is True
        # Only one image (from secondary) — no splicing.
        assert len(out.images) == 1
        assert out.provider == "fal"

    @pytest.mark.asyncio
    async def test_generate_returns_complete_result(self) -> None:
        result = GenerationResult(
            images=[
                GeneratedImage(
                    image_bytes=b"complete",
                    media_type="image/png",
                    width=1024,
                    height=1024,
                ),
                GeneratedImage(
                    image_bytes=b"complete2",
                    media_type="image/png",
                    width=1024,
                    height=1024,
                ),
            ],
            provider="fal",
            model="flux-pro",
            latency_ms=10.0,
        )
        backend = _ScriptedBackend(provider="fal", model="flux-pro", script=[result])
        wrapper = MultiModelImageBackend([backend])
        out = await wrapper.generate("a cat")
        assert out is result
        assert len(out.images) == 2


# ---------------------------------------------------------------------
# Classifier surface — non-PersonaError programmer bug → SURFACE
# ---------------------------------------------------------------------


class TestProgrammerBugSurface:
    @pytest.mark.asyncio
    async def test_value_error_surfaces(self) -> None:
        # A bare ValueError from below the boundary is a programmer
        # bug — fallback would mask it.
        primary = _ScriptedBackend(
            provider="nvidia",
            model="flux",
            script=[ValueError("bug in adapter")],
        )
        secondary_result = _make_result()
        secondary = _ScriptedBackend(provider="fal", model="flux-pro", script=[secondary_result])
        wrapper = MultiModelImageBackend([primary, secondary])
        with pytest.raises(ValueError, match="bug in adapter"):
            await wrapper.generate("a cat")
        assert secondary.calls == []


# ---------------------------------------------------------------------
# AttemptRecord shape
# ---------------------------------------------------------------------


class TestAttemptRecord:
    def test_dataclass_is_frozen(self) -> None:
        record = AttemptRecord(
            provider="p",
            model="m",
            last_error_class="ImageProviderError",
            last_error_reason="rate_limit",
            last_error_message="rate limited",
            retried_same_model=True,
        )
        with pytest.raises((AttributeError, TypeError)):
            record.provider = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------
# R9-015 — provider error surfacing on fallback + exhaustion
# ---------------------------------------------------------------------


@pytest.fixture
def log_records() -> Iterator[list[dict[str, Any]]]:
    """Loguru sink capturing full records (message + structured ``extra``)."""
    records: list[dict[str, Any]] = []
    sink_id = _loguru_logger.add(lambda m: records.append(dict(m.record)), level="DEBUG")
    try:
        yield records
    finally:
        _loguru_logger.remove(sink_id)


def _auth_with_secret() -> AuthenticationError:
    """An auth error whose message leaks a bearer token + sk- key (worst case)."""
    return AuthenticationError(
        "401 Unauthorized: Authorization: Bearer "
        "sk-or-v1-abcdef0123456789abcdef0123456789 rejected",
        context={"provider": "openrouter"},
    )


class TestProviderErrorSurfacing:
    @pytest.mark.asyncio
    async def test_log_fallback_emits_redacted_error_message(
        self, log_records: list[dict[str, Any]]
    ) -> None:
        # A backend fails with a distinctive message → the fallback WARNING
        # carries an ``error=`` field so a bad model id is distinguishable
        # from an auth failure (the whole R9-015 point).
        primary = _ScriptedBackend(
            provider="nvidia",
            model="flux",
            script=[_model_not_found()],
        )
        secondary = _ScriptedBackend(
            provider="fal", model="flux-pro", script=[_make_result(provider="fal")]
        )
        wrapper = MultiModelImageBackend([primary, secondary], tier_name="imagegen")

        await wrapper.generate("a cat")

        fallback = next(r for r in log_records if r["message"] == "multi_model_image fallback")
        # PersonaError.str() appends its context; the provider phrasing is present.
        assert "no such model" in fallback["extra"]["error"]
        assert fallback["extra"]["error_class"] == "ImageProviderError"

    @pytest.mark.asyncio
    async def test_log_fallback_scrubs_secret_from_message(
        self, log_records: list[dict[str, Any]]
    ) -> None:
        # A provider error echoing a Bearer token + sk- key must be scrubbed
        # before it reaches the sink.
        primary = _ScriptedBackend(provider="openrouter", model="x", script=[_auth_with_secret()])
        secondary = _ScriptedBackend(
            provider="fal", model="flux-pro", script=[_make_result(provider="fal")]
        )
        wrapper = MultiModelImageBackend([primary, secondary])

        await wrapper.generate("a cat")

        fallback = next(r for r in log_records if r["message"] == "multi_model_image fallback")
        logged = fallback["extra"]["error"]
        assert "sk-or-v1-abcdef0123456789abcdef0123456789" not in logged
        assert "Bearer sk-or-v1" not in logged
        assert "<redacted>" in logged
        # The non-secret signal ("401 Unauthorized") is preserved.
        assert "401 Unauthorized" in logged

    @pytest.mark.asyncio
    async def test_exhaustion_error_carries_per_backend_causes(self) -> None:
        # When every backend fails, the aggregated AllModelsFailedError must
        # carry each backend's redacted message so the dead-letter last_error
        # is diagnosable — not just "everything failed".
        primary = _ScriptedBackend(provider="nvidia", model="flux", script=[_credits_expired()])
        secondary = _ScriptedBackend(provider="openrouter", model="x", script=[_auth_with_secret()])
        wrapper = MultiModelImageBackend([primary, secondary])

        with pytest.raises(AllModelsFailedError) as excinfo:
            await wrapper.generate("a cat")
        ctx = excinfo.value.context
        attempts_str = ctx["attempts"]
        # Per-backend messages present (redacted) in the attempts dump.
        assert "402 paid trial expired" in attempts_str
        assert "401 Unauthorized" in attempts_str
        # The final (terminal) cause is hoisted for the dead-letter.
        assert "401 Unauthorized" in ctx["final_error"]
        # And the secret never leaks into the aggregated error.
        assert "sk-or-v1-abcdef0123456789abcdef0123456789" not in attempts_str
        assert "sk-or-v1-abcdef0123456789abcdef0123456789" not in ctx["final_error"]


# ---------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------


async def _no_sleep(_s: float) -> None:
    """Replace ``asyncio.sleep`` to keep retry-path tests fast."""
    return
