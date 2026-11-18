"""Spec 20 acceptance criterion 9a — cross-provider multi-model fallback.

Exercises :class:`MultiModelChatBackend` + :class:`MultiModelImageBackend`
end-to-end with scripted backends from MULTIPLE providers (openai /
anthropic / deepseek / fal). Verifies D-20-9 / D-20-10 / D-20-12 /
D-20-14 / D-20-15 / D-20-16 / D-20-17 / D-20-18 locks, the Spec 15
:class:`ContentRejectedError` SURFACE invariant, TurnLog 5+1 fallback
fields, and the backward-compat single-model env-var path (5d).

``@pytest.mark.integration`` — default-skipped; CI runs with
``pytest -m integration``. Transport scripted via Protocol doubles —
no SDK reach, no live key. T20 verified the Persona→SDK transport;
T21 verifies the wrapper composition itself.

Location rationale: under ``packages/runtime/tests/integration/`` —
the file exercises BOTH the wrapper (core) and the TurnLog
``_compute_fallback_fields`` projection plus :func:`tier_registry_from_env`
(both runtime modules).
"""

# ruff: noqa: SLF001, N802, PT012, PT019
# SLF001: integration test asserts on the wrapper's private last_attempts
# ledger via the public ``last_attempts`` property surface (documentation
# value of the underscore is implicit here).
# N802: test names embed acceptance-criterion identifiers (e.g.
# ``AllModelsFailedError``) — the camel-case is load-bearing and matches
# the symbol the test exercises.
# PT012: the streaming post-first-chunk test must consume the generator
# inside ``pytest.raises`` to observe the partial-output invariant.
# PT019: ``_isolated_env`` fixture name is intentionally underscored
# (private helper that yields ``monkeypatch`` after pre-cleaning env).

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.backends.errors import (
    AllModelsFailedError,
    AuthenticationError,
    BackendTimeoutError,
    IncompleteTierConfigError,
    LocalProviderInModelsListError,
    MalformedTierModelsError,
    ModelNotFoundError,
    ProviderCredentialMissingError,
    ProviderError,
    RateLimitError,
)
from persona.backends.multi_model import (
    MultiModelChatBackend,
)
from persona.backends.types import (
    BackendAttempt,
    ChatResponse,
    ServedCall,
    StreamChunk,
    TokenUsage,
)
from persona.errors import PersonaError
from persona.imagegen.errors import ContentRejectedError, ImageProviderError
from persona.imagegen.multi_model_image import MultiModelImageBackend
from persona.imagegen.result import GeneratedImage, GenerationResult, ImageGenOptions
from persona.schema.conversation import ConversationMessage
from persona_runtime.loop import _compute_fallback_fields
from persona_runtime.tier import tier_registry_from_env

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Sequence

    from persona.backends.types import ToolSpec


pytestmark = pytest.mark.integration


# ---------------------------------------------------------------------- #
# Scripted ChatBackend double — implements the ChatBackend Protocol.
# ---------------------------------------------------------------------- #


class _ScriptedChatBackend:
    """ChatBackend Protocol double; popped-FIFO script of outcomes."""

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        script: Sequence[object],
        supports_native_tools: bool = True,
        supports_vision: bool = False,
    ) -> None:
        self._provider = provider
        self._model = model
        self._script: list[object] = list(script)
        self._supports_native_tools = supports_native_tools
        self._supports_vision = supports_vision
        self.call_count = 0
        self.stream_call_count = 0

    @property
    def provider_name(self) -> str:
        return self._provider

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def supports_native_tools(self) -> bool:
        return self._supports_native_tools

    @property
    def supports_vision(self) -> bool:
        return self._supports_vision

    async def chat(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002
        *,
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
        temperature: float = 0.0,  # noqa: ARG002
        max_tokens: int = 4096,  # noqa: ARG002
        stop: list[str] | None = None,  # noqa: ARG002
        top_p: float | None = None,  # noqa: ARG002
        top_k: int | None = None,  # noqa: ARG002
        **_kwargs: object,  # forward-compat: accept any further sampling knobs
    ) -> ChatResponse:
        self.call_count += 1
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, ChatResponse)
        return outcome

    async def chat_stream(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002
        *,
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
        temperature: float = 0.0,  # noqa: ARG002
        max_tokens: int = 4096,  # noqa: ARG002
        stop: list[str] | None = None,  # noqa: ARG002
        top_p: float | None = None,  # noqa: ARG002
        top_k: int | None = None,  # noqa: ARG002
        **_kwargs: object,  # forward-compat: accept any further sampling knobs
    ) -> AsyncIterator[StreamChunk]:
        self.stream_call_count += 1
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, list)
        for item in outcome:
            if isinstance(item, Exception):
                raise item
            assert isinstance(item, StreamChunk)
            yield item


# ---------------------------------------------------------------------- #
# Scripted ImageBackend double — implements the ImageBackend Protocol
# (plus the reserved ``edit`` method per D-15-X-edit-protocol-reservation).
# ---------------------------------------------------------------------- #


class _ScriptedImageBackend:
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
        self.calls: list[str] = []

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
        options: ImageGenOptions | None = None,  # noqa: ARG002
    ) -> GenerationResult:
        self.calls.append(prompt)
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def edit(
        self,
        input_image: GeneratedImage,  # noqa: ARG002
        instructions: str,  # noqa: ARG002
        *,
        options: ImageGenOptions | None = None,  # noqa: ARG002
    ) -> GenerationResult:
        raise NotImplementedError("edit reserved per D-15-X-edit-protocol-reservation")


# ---------------------------------------------------------------------- #
# Factory helpers.
# ---------------------------------------------------------------------- #


def _user(text: str = "ping") -> ConversationMessage:
    return ConversationMessage(role="user", content=text, created_at=datetime.now(UTC))


def _ok_chat(provider: str, model: str, content: str = "ok") -> ChatResponse:
    return ChatResponse(
        content=content,
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model=model,
        provider=provider,
        latency_ms=1.0,
    )


def _ok_image(provider: str, model: str) -> GenerationResult:
    return GenerationResult(
        images=[
            GeneratedImage(
                image_bytes=b"\x89PNG-OK",
                media_type="image/png",
                width=1024,
                height=1024,
            )
        ],
        provider=provider,
        model=model,
        latency_ms=1.0,
    )


def _stream_chunk(delta: str = "", *, is_final: bool = False) -> StreamChunk:
    return StreamChunk(
        delta=delta,
        is_final=is_final,
        usage=(
            TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2) if is_final else None
        ),
    )


# ====================================================================== #
# TestCrossProviderChatFallback
# D-20-9 three-bucket classifier + D-20-10 retry + D-20-12 auth.
# ====================================================================== #


class TestCrossProviderChatFallback:
    """Chat fallback — D-20-9 / D-20-10 / D-20-12 / D-20-16."""

    @pytest.mark.asyncio
    async def test_primary_rate_limit_short_retry_after_retries_then_falls_back(self) -> None:
        """D-20-9 RETRY-THEN-FALLBACK + D-20-10 N=1 retry.

        Primary raises twice → wrapper retries once same-model then
        advances; ``last_attempts`` records ``retried_same_model=True``.
        """
        primary = _ScriptedChatBackend(
            provider="openai",
            model="gpt-4o",
            script=[
                RateLimitError("rl-1", context={"provider": "openai"}),
                RateLimitError("rl-2", context={"provider": "openai"}),
            ],
        )
        secondary = _ScriptedChatBackend(
            provider="anthropic",
            model="claude-sonnet-4-6",
            script=[_ok_chat("anthropic", "claude-sonnet-4-6", "fb")],
        )
        wrapper = MultiModelChatBackend([primary, secondary], tier_name="frontier")
        response = await wrapper.chat([_user()])

        assert response.provider == "anthropic"
        assert response.content == "fb"
        assert primary.call_count == 2  # original + 1 retry
        assert secondary.call_count == 1
        attempts = wrapper.last_attempts
        assert len(attempts) == 1
        assert attempts[0].provider == "openai"
        assert attempts[0].last_error_class == "RateLimitError"
        assert attempts[0].retried_same_model is True

    @pytest.mark.asyncio
    async def test_primary_rate_limit_long_retry_after_immediate_fallback(self) -> None:
        """D-20-9 row 3 — Retry-After > 2s cutoff skips same-model retry."""
        primary = _ScriptedChatBackend(
            provider="openai",
            model="gpt-4o",
            script=[RateLimitError("rl", context={"provider": "openai", "retry_after_s": "10"})],
        )
        secondary = _ScriptedChatBackend(
            provider="deepseek",
            model="deepseek-v3",
            script=[_ok_chat("deepseek", "deepseek-v3")],
        )
        wrapper = MultiModelChatBackend([primary, secondary], tier_name="mid")
        response = await wrapper.chat([_user()])

        assert response.provider == "deepseek"
        assert primary.call_count == 1  # NO retry
        assert wrapper.last_attempts[0].retried_same_model is False

    @pytest.mark.asyncio
    async def test_primary_authentication_error_fallback_with_warning(self) -> None:
        """D-20-12 SKIP-AND-FALLBACK + structured WARNING (loguru sink)."""
        from loguru import logger as _loguru_logger

        captured: list[str] = []
        sink_id = _loguru_logger.add(
            lambda msg: captured.append(str(msg)),
            level="WARNING",
            serialize=True,
        )
        try:
            primary = _ScriptedChatBackend(
                provider="anthropic",
                model="claude-sonnet-4-6",
                script=[AuthenticationError("401", context={"provider": "anthropic"})],
            )
            secondary = _ScriptedChatBackend(
                provider="deepseek",
                model="deepseek-v3",
                script=[_ok_chat("deepseek", "deepseek-v3", "after-auth-fb")],
            )
            wrapper = MultiModelChatBackend([primary, secondary], tier_name="frontier")
            response = await wrapper.chat([_user()])
        finally:
            _loguru_logger.remove(sink_id)

        assert response.provider == "deepseek"
        assert response.content == "after-auth-fb"
        assert primary.call_count == 1  # no retry on auth (FALLBACK-NO-RETRY)
        joined = "".join(captured)
        assert "fallback" in joined.lower()
        assert "AuthenticationError" in joined

    @pytest.mark.asyncio
    async def test_primary_content_policy_violation_surfaces_immediately(self) -> None:
        """D-20-9 SURFACE — content_policy_violation never falls back."""
        primary = _ScriptedChatBackend(
            provider="openai",
            model="gpt-4o",
            script=[
                ProviderError(
                    "refused",
                    context={
                        "provider": "openai",
                        "status_code": "400",
                        "reason": "content_policy_violation",
                    },
                ),
            ],
        )
        secondary = _ScriptedChatBackend(
            provider="anthropic",
            model="claude",
            script=[_ok_chat("anthropic", "claude")],
        )
        wrapper = MultiModelChatBackend([primary, secondary])
        with pytest.raises(ProviderError):
            await wrapper.chat([_user()])
        assert primary.call_count == 1
        assert secondary.call_count == 0, "SURFACE bucket must NOT touch the secondary backend"

    @pytest.mark.asyncio
    async def test_all_backends_exhaust_raises_AllModelsFailedError(self) -> None:
        """D-20-16 — three backends fail; AllModelsFailedError carries attempt_count=3."""
        backends = [
            _ScriptedChatBackend(
                provider=p,
                model=m,
                script=[
                    RateLimitError("rl", context={"provider": p, "retry_after_s": "10"}),
                ],
            )
            for p, m in (("openai", "gpt-4o"), ("anthropic", "claude"), ("deepseek", "v3"))
        ]
        wrapper = MultiModelChatBackend(list(backends), tier_name="frontier")
        with pytest.raises(AllModelsFailedError) as excinfo:
            await wrapper.chat([_user()])
        ctx = excinfo.value.context
        assert ctx["attempt_count"] == "3"
        assert ctx["tier"] == "frontier"
        assert ctx["final_error_class"] == "RateLimitError"
        # attempts_json renders dataclasses as a list of dicts.
        assert "openai" in ctx["attempts_json"]
        assert "anthropic" in ctx["attempts_json"]
        assert "deepseek" in ctx["attempts_json"]

    @pytest.mark.asyncio
    async def test_runtime_provider_credential_missing_falls_back_no_retry(self) -> None:
        """D-20-15 runtime ``ProviderCredentialMissingError`` → FALLBACK-NO-RETRY."""
        primary = _ScriptedChatBackend(
            provider="openai",
            model="gpt-4o",
            script=[
                ProviderCredentialMissingError(
                    "missing",
                    context={"provider": "openai", "env_var": "PERSONA_OPENAI_API_KEY"},
                ),
            ],
        )
        secondary = _ScriptedChatBackend(
            provider="anthropic",
            model="claude",
            script=[_ok_chat("anthropic", "claude")],
        )
        wrapper = MultiModelChatBackend([primary, secondary])
        response = await wrapper.chat([_user()])
        assert response.provider == "anthropic"
        assert primary.call_count == 1  # FALLBACK-NO-RETRY
        assert wrapper.last_attempts[0].last_error_class == "ProviderCredentialMissingError"

    @pytest.mark.asyncio
    async def test_streaming_fallback_before_first_chunk(self) -> None:
        """Pre-first-chunk error → classifier path (fallback engaged)."""
        primary = _ScriptedChatBackend(
            provider="openai",
            model="gpt-4o",
            script=[BackendTimeoutError("t1", context={"provider": "openai"})],
        )
        secondary = _ScriptedChatBackend(
            provider="anthropic",
            model="claude",
            script=[[_stream_chunk("hello "), _stream_chunk("world", is_final=True)]],
        )
        wrapper = MultiModelChatBackend(
            [primary, secondary], tier_name="frontier", max_retries_per_backend=0
        )
        collected: list[str] = []
        async for chunk in wrapper.chat_stream([_user()]):
            collected.append(chunk.delta)
        assert "".join(collected) == "hello world"
        assert wrapper.last_attempts[0].provider == "openai"

    @pytest.mark.asyncio
    async def test_streaming_post_first_chunk_error_raises_verbatim(self) -> None:
        """Two-phase: post-first-chunk error surfaces directly (no fallback)."""
        primary = _ScriptedChatBackend(
            provider="openai",
            model="gpt-4o",
            script=[
                [
                    _stream_chunk("partial "),
                    BackendTimeoutError("mid-stream", context={"provider": "openai"}),
                ]
            ],
        )
        secondary = _ScriptedChatBackend(
            provider="anthropic",
            model="claude",
            script=[[_stream_chunk("fallback", is_final=True)]],
        )
        wrapper = MultiModelChatBackend([primary, secondary])
        collected: list[str] = []
        with pytest.raises(BackendTimeoutError):
            async for chunk in wrapper.chat_stream([_user()]):
                collected.append(chunk.delta)
        assert collected == ["partial "], "partial output preserved before mid-stream raise"
        assert secondary.stream_call_count == 0, "post-first-chunk error MUST NOT fall back"


# ====================================================================== #
# TestCrossProviderImageGenFallback
# Mirror the chat suite + the Spec 15 ContentRejectedError SURFACE invariant.
# ====================================================================== #


class TestCrossProviderImageGenFallback:
    """Image-gen fallback + Spec 15 ContentRejectedError SURFACE invariant."""

    @pytest.mark.asyncio
    async def test_primary_rate_limit_fallback_to_secondary(self) -> None:
        primary = _ScriptedImageBackend(
            provider="openai",
            model="gpt-image-1",
            script=[
                ImageProviderError(
                    "rl",
                    context={
                        "provider": "openai",
                        "reason": "rate_limit",
                        "retry_after_s": "10",
                    },
                ),
            ],
        )
        secondary = _ScriptedImageBackend(
            provider="fal",
            model="flux-pro",
            script=[_ok_image("fal", "flux-pro")],
        )
        wrapper = MultiModelImageBackend([primary, secondary], tier_name="imagegen")
        result = await wrapper.generate("a cat in space")
        assert result.provider == "fal"
        assert len(primary.calls) == 1  # no retry (long retry-after)

    @pytest.mark.asyncio
    async def test_content_rejected_error_surfaces_immediately(self) -> None:
        """Spec 15 invariant + D-20-9 SURFACE — secondary NEVER called."""
        primary = _ScriptedImageBackend(
            provider="openai",
            model="gpt-image-1",
            script=[
                ContentRejectedError(
                    "moderation",
                    context={
                        "provider": "openai",
                        "reason": "provider_moderation",
                        "stage": "input",
                    },
                ),
            ],
        )
        secondary = _ScriptedImageBackend(
            provider="fal",
            model="flux-pro",
            script=[_ok_image("fal", "flux-pro")],
        )
        wrapper = MultiModelImageBackend([primary, secondary], tier_name="imagegen")
        with pytest.raises(ContentRejectedError):
            await wrapper.generate("a forbidden prompt")
        assert len(primary.calls) == 1
        assert len(secondary.calls) == 0, (
            "Spec 15 invariant violated: ContentRejectedError fell back to secondary"
        )

    @pytest.mark.asyncio
    async def test_atomic_generate_no_partial_state_on_mid_fallback(self) -> None:
        """D-20-14 DISCARD + RESTART — caller sees secondary's complete result only."""
        primary = _ScriptedImageBackend(
            provider="openai",
            model="gpt-image-1",
            script=[
                ImageProviderError(
                    "503",
                    context={"provider": "openai", "reason": "transient"},
                ),
            ],
        )
        secondary_result = _ok_image("fal", "flux-pro")
        secondary = _ScriptedImageBackend(
            provider="fal",
            model="flux-pro",
            script=[secondary_result],
        )
        wrapper = MultiModelImageBackend(
            [primary, secondary],
            tier_name="imagegen",
            max_retries_per_backend=0,
        )
        result = await wrapper.generate("a cat")
        assert result is secondary_result
        assert result.images[0].image_bytes == b"\x89PNG-OK"

    @pytest.mark.asyncio
    async def test_all_image_backends_exhaust_raises_AllModelsFailedError(self) -> None:
        """D-20-16 image-wrapper exhaustion shape."""
        backends = [
            _ScriptedImageBackend(
                provider=p,
                model=m,
                script=[
                    ImageProviderError(
                        "down",
                        context={"provider": p, "reason": "credits_expired"},
                    ),
                ],
            )
            for p, m in (("openai", "gpt-image-1"), ("fal", "flux-pro"))
        ]
        wrapper = MultiModelImageBackend(list(backends), tier_name="imagegen")
        with pytest.raises(AllModelsFailedError) as excinfo:
            await wrapper.generate("anything")
        ctx = excinfo.value.context
        assert ctx["attempt_count"] == "2"
        assert ctx["tier"] == "imagegen"
        assert "openai" in ctx["attempts"]
        assert "fal" in ctx["attempts"]


# ====================================================================== #
# TestTurnLogFallbackFields
# T19 _compute_fallback_fields end-to-end through the served trace the
# wrapper stamps on each call's own response (R9-223).
# ====================================================================== #


class TestTurnLogFallbackFields:
    """End-to-end TurnLog 5+1 fallback field projection (T19 + D-20-9)."""

    @pytest.mark.asyncio
    async def test_no_fallback_single_attempt_zero_count(self) -> None:
        """Clean primary success → counts zero, lists empty, engaged False."""
        primary = _ScriptedChatBackend(
            provider="openai",
            model="gpt-4o",
            script=[_ok_chat("openai", "gpt-4o")],
        )
        wrapper = MultiModelChatBackend([primary], tier_name="frontier")
        response = await wrapper.chat([_user()])
        fields = _compute_fallback_fields(wrapper, response.served)
        assert fields["tier_fallback_count"] == 0
        assert fields["tier_fallback_reasons"] == []
        assert fields["tier_fallback_providers"] == []
        assert fields["fallback_engaged"] is False
        assert fields["tier_provider_used"] == "openai"
        assert fields["tier_model_chosen"] == "gpt-4o"

    @pytest.mark.asyncio
    async def test_one_fallback_count_one_engaged_true(self) -> None:
        """One fallback → count=1, lists len 1, engaged True, winner is secondary."""
        primary = _ScriptedChatBackend(
            provider="openai",
            model="gpt-4o",
            script=[ModelNotFoundError("gone", context={"provider": "openai", "model": "gpt-4o"})],
        )
        secondary = _ScriptedChatBackend(
            provider="anthropic",
            model="claude-sonnet-4-6",
            script=[_ok_chat("anthropic", "claude-sonnet-4-6")],
        )
        wrapper = MultiModelChatBackend([primary, secondary], tier_name="frontier")
        response = await wrapper.chat([_user()])
        fields = _compute_fallback_fields(wrapper, response.served)
        assert fields["tier_fallback_count"] == 1
        assert fields["tier_fallback_reasons"] == ["ModelNotFoundError"]
        assert fields["tier_fallback_providers"] == ["openai"]
        assert fields["fallback_engaged"] is True
        assert fields["tier_provider_used"] == "anthropic"
        assert fields["tier_model_chosen"] == "claude-sonnet-4-6"

    @pytest.mark.asyncio
    async def test_three_fallbacks_lists_match_attempt_classes_and_providers(self) -> None:
        """Three failed primaries → four-backend chain; winner is index 3."""
        chain = [
            _ScriptedChatBackend(
                provider="openai",
                model="gpt-4o",
                script=[
                    ModelNotFoundError("nope", context={"provider": "openai", "model": "gpt-4o"})
                ],
            ),
            _ScriptedChatBackend(
                provider="anthropic",
                model="claude",
                script=[AuthenticationError("bad", context={"provider": "anthropic"})],
            ),
            _ScriptedChatBackend(
                provider="deepseek",
                model="v3",
                script=[
                    RateLimitError("rl", context={"provider": "deepseek", "retry_after_s": "30"})
                ],
            ),
            _ScriptedChatBackend(
                provider="groq",
                model="llama-3.1",
                script=[_ok_chat("groq", "llama-3.1", "winner")],
            ),
        ]
        wrapper = MultiModelChatBackend(list(chain), tier_name="frontier")
        response = await wrapper.chat([_user()])
        fields = _compute_fallback_fields(wrapper, response.served)
        assert fields["tier_fallback_count"] == 3
        assert fields["tier_fallback_reasons"] == [
            "ModelNotFoundError",
            "AuthenticationError",
            "RateLimitError",
        ]
        assert fields["tier_fallback_providers"] == ["openai", "anthropic", "deepseek"]
        assert fields["fallback_engaged"] is True
        assert fields["tier_provider_used"] == "groq"
        assert fields["tier_model_chosen"] == "llama-3.1"

    @pytest.mark.asyncio
    async def test_AllModelsFailedError_path_writes_TurnLog_with_full_attempts(self) -> None:
        """Exhaustion path — ledger fully populated for operator dashboard."""
        chain = [
            _ScriptedChatBackend(
                provider=p,
                model=m,
                script=[AuthenticationError("bad", context={"provider": p})],
            )
            for p, m in (("openai", "gpt-4o"), ("anthropic", "claude"))
        ]
        wrapper = MultiModelChatBackend(list(chain), tier_name="frontier")
        with pytest.raises(AllModelsFailedError):
            await wrapper.chat([_user()])
        attempts = wrapper.last_attempts
        assert [a.provider for a in attempts] == ["openai", "anthropic"]
        assert all(a.last_error_class == "AuthenticationError" for a in attempts)

    def test_privacy_only_class_names_in_reasons_field(self) -> None:
        """D-20-9 privacy — only class names leak; never message/context."""

        # A hand-built served trace, so the projection helper sees only the
        # class-name strings the chain records (it never keeps a message).
        served = ServedCall(
            provider="anthropic",
            model="c",
            attempts=(
                BackendAttempt(
                    index=0,
                    provider="openai",
                    model="gpt-4o",
                    error_class="AuthenticationError",
                    status_code=401,
                ),
                BackendAttempt(index=1, provider="anthropic", model="c"),
            ),
        )

        fields = _compute_fallback_fields(None, served)  # type: ignore[arg-type]
        # Only the class-name string leaks; no message / context value.
        assert fields["tier_fallback_reasons"] == ["AuthenticationError"]
        assert fields["tier_fallback_providers"] == ["openai"]
        # Winner of the chain is index 1 (after one fallback) — secondary.
        assert fields["tier_provider_used"] == "anthropic"


# ====================================================================== #
# TestBackwardCompatSingleModelPath
# Acceptance 5d + D-20-17 four-case precedence + D-20-18 reject.
# ====================================================================== #


_PERSONA_VARS = (
    "PERSONA_FRONTIER_MODELS",
    "PERSONA_FRONTIER_PROVIDER",
    "PERSONA_FRONTIER_MODEL",
    "PERSONA_FRONTIER_API_KEY",
    "PERSONA_MID_MODELS",
    "PERSONA_MID_PROVIDER",
    "PERSONA_MID_MODEL",
    "PERSONA_MID_API_KEY",
    "PERSONA_SMALL_MODELS",
    "PERSONA_SMALL_PROVIDER",
    "PERSONA_SMALL_MODEL",
    "PERSONA_SMALL_API_KEY",
    "PERSONA_OPENAI_API_KEY",
    "PERSONA_ANTHROPIC_API_KEY",
    "PERSONA_DEEPSEEK_API_KEY",
    "PERSONA_PROVIDER",
    "PERSONA_MODEL",
    "PERSONA_API_KEY",
)


@pytest.fixture
def _isolated_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Strip every Persona-related env var so each precedence test runs clean."""
    for var in _PERSONA_VARS:
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


class TestBackwardCompatSingleModelPath:
    """D-20-17 four-case parser precedence + D-20-18 reject."""

    def test_triplet_only_env_constructs_bare_single_backend(
        self, _isolated_env: pytest.MonkeyPatch
    ) -> None:
        """D-20-17 case (b) — triplet → bare backend (acceptance 5d)."""
        _isolated_env.setenv("PERSONA_FRONTIER_PROVIDER", "openai")
        _isolated_env.setenv("PERSONA_FRONTIER_MODEL", "gpt-4o")
        _isolated_env.setenv("PERSONA_FRONTIER_API_KEY", "sk-test")
        registry = tier_registry_from_env()
        backend = registry.get("frontier")
        assert not isinstance(backend, MultiModelChatBackend), (
            "triplet-only path MUST construct a bare backend (acceptance 5d)"
        )
        assert backend.provider_name == "openai"
        assert backend.model_name == "gpt-4o"

    def test_models_only_env_constructs_wrapper(self, _isolated_env: pytest.MonkeyPatch) -> None:
        """D-20-17 case (a) — MODELS list wins; wrapper constructed."""
        _isolated_env.setenv("PERSONA_FRONTIER_MODELS", "openai/gpt-4o,anthropic/claude-sonnet-4-6")
        _isolated_env.setenv("PERSONA_OPENAI_API_KEY", "sk-openai")
        _isolated_env.setenv("PERSONA_ANTHROPIC_API_KEY", "sk-anthropic")
        registry = tier_registry_from_env()
        backend = registry.get("frontier")
        assert isinstance(backend, MultiModelChatBackend)
        assert len(backend.backends) == 2
        assert backend.backends[0].provider_name == "openai"
        assert backend.backends[1].provider_name == "anthropic"
        assert backend.tier_name == "frontier"

    def test_both_set_emits_INFO_log_and_models_wins(
        self,
        _isolated_env: pytest.MonkeyPatch,
    ) -> None:
        """D-20-17 case (c) — both set; MODELS wins + INFO log."""
        from loguru import logger as _loguru_logger

        _isolated_env.setenv("PERSONA_FRONTIER_MODELS", "openai/gpt-4o")
        _isolated_env.setenv("PERSONA_FRONTIER_PROVIDER", "anthropic")
        _isolated_env.setenv("PERSONA_FRONTIER_MODEL", "claude")
        _isolated_env.setenv("PERSONA_FRONTIER_API_KEY", "ignored")
        _isolated_env.setenv("PERSONA_OPENAI_API_KEY", "sk-openai")
        captured: list[str] = []
        sink_id = _loguru_logger.add(
            lambda msg: captured.append(str(msg)),
            level="INFO",
            serialize=True,
        )
        try:
            registry = tier_registry_from_env()
        finally:
            _loguru_logger.remove(sink_id)
        backend = registry.get("frontier")
        # MODELS wins → provider is openai (from MODELS), NOT anthropic.
        assert backend.provider_name == "openai"
        # INFO log surfaced naming the ignored triplet vars.
        joined = "".join(captured)
        assert "takes precedence" in joined, (
            f"D-20-17 case (c) INFO log not captured; records: {captured}"
        )

    def test_malformed_models_raises_at_construction(
        self, _isolated_env: pytest.MonkeyPatch
    ) -> None:
        """D-20-17 case (d) — malformed MODELS raises at registry build."""
        _isolated_env.setenv("PERSONA_FRONTIER_MODELS", "no-slash-here")
        with pytest.raises(MalformedTierModelsError) as excinfo:
            tier_registry_from_env()
        assert excinfo.value.context["reason"] == "missing_slash"

    def test_partial_triplet_raises_incomplete_tier_config(
        self, _isolated_env: pytest.MonkeyPatch
    ) -> None:
        """Triplet 1-of-3 + no MODELS → :class:`IncompleteTierConfigError`."""
        _isolated_env.setenv("PERSONA_FRONTIER_PROVIDER", "openai")
        # MODEL + API_KEY intentionally absent.
        with pytest.raises(IncompleteTierConfigError) as excinfo:
            tier_registry_from_env()
        missing = excinfo.value.context["missing_vars"]
        assert "PERSONA_FRONTIER_MODEL" in missing
        assert "PERSONA_FRONTIER_API_KEY" in missing

    def test_local_provider_in_models_list_rejected(
        self, _isolated_env: pytest.MonkeyPatch
    ) -> None:
        """D-20-18 EXPLICIT REJECT — local/ollama refused in MODELS list."""
        _isolated_env.setenv("PERSONA_FRONTIER_MODELS", "ollama/llama3,openai/gpt-4o")
        with pytest.raises(LocalProviderInModelsListError) as excinfo:
            tier_registry_from_env()
        assert "ollama" in excinfo.value.context["hint"] or excinfo.value.context.get("position")


# ====================================================================== #
# TestDeepSeekReasoningStripInvariant
# D-20-X-deepseek-reasoning-strip-invariant verification at the
# integration layer.
# ====================================================================== #


class TestDeepSeekReasoningStripInvariant:
    """D-20-X-deepseek-reasoning-strip-invariant — integration-layer sanity check.

    Exhaustive branch matrix covered by unit tests at
    ``packages/core/tests/unit/backends/test_openai_compat.py``.
    """

    def test_strip_helper_removes_reasoning_only_for_deepseek_provider(self) -> None:
        """Helper preserves field for non-deepseek; strips for deepseek."""
        from persona.backends.openai_compat import _strip_reasoning_for_provider

        messages: list[dict[str, object]] = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": "answer",
                "reasoning_content": "step-by-step CoT",
            },
            {"role": "user", "content": "follow-up"},
        ]
        # Non-deepseek providers preserve the field.
        unchanged = _strip_reasoning_for_provider(messages, "openai")
        assert "reasoning_content" in unchanged[1]
        # DeepSeek strips it on every assistant message.
        stripped = _strip_reasoning_for_provider(messages, "deepseek")
        assert "reasoning_content" not in stripped[1]
        # Non-assistant messages are passed through verbatim regardless.
        assert stripped[0] == messages[0]
        assert stripped[2] == messages[2]


# ====================================================================== #
# TestPersonaErrorContract — defensive check that wrapper-layer errors
# slot under PersonaError (D-20-16) so callers' broad except clauses
# work as expected.
# ====================================================================== #


class TestPersonaErrorContract:
    """D-20-16 — wrapper errors are :class:`PersonaError` subclasses."""

    def test_all_models_failed_error_is_persona_error(self) -> None:
        assert issubclass(AllModelsFailedError, PersonaError)

    def test_malformed_tier_models_error_is_persona_error(self) -> None:
        assert issubclass(MalformedTierModelsError, PersonaError)

    def test_local_provider_error_is_persona_error(self) -> None:
        assert issubclass(LocalProviderInModelsListError, PersonaError)

    def test_incomplete_tier_config_error_is_persona_error(self) -> None:
        assert issubclass(IncompleteTierConfigError, PersonaError)
