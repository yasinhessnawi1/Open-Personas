"""Tests for :class:`persona.backends.multi_model.MultiModelChatBackend`.

Covers Spec 20 T15 deliverables — the cross-provider ordered-fallback
ChatBackend wrapper that composes N concrete backends per:

* **D-20-9** — three-bucket classifier
  (``RETRY-THEN-FALLBACK`` / ``FALLBACK-NO-RETRY`` / ``SURFACE``).
* **D-20-10** — N=1 same-model retry, 200ms ± jitter sleep, then fallback.
* **D-20-12** — cross-provider :class:`AuthenticationError` skip-and-fallback
  with structured WARNING log.
* **D-20-15** — runtime :class:`ProviderCredentialMissingError` →
  FALLBACK-NO-RETRY.
* **D-20-16** — :class:`AllModelsFailedError` slots under
  :class:`PersonaError` (NOT :class:`ProviderError`).

The tests use lightweight scripted :class:`ChatBackend` doubles rather than
real provider SDKs — the wrapper does not care which backend it talks to.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.backends.errors import (
    AllModelsFailedError,
    AuthenticationError,
    BackendTimeoutError,
    BackendVisionNotSupportedError,
    ModelNotFoundError,
    ModelUnavailableError,
    NoVisionCapableModelError,
    ProviderCredentialMissingError,
    ProviderError,
    RateLimitError,
)
from persona.backends.multi_model import (
    AttemptRecord,
    MultiModelChatBackend,
)
from persona.backends.protocol import ChatBackend
from persona.backends.types import ChatResponse, StreamChunk, TokenUsage
from persona.errors import PersonaError
from persona.schema.content import ImageContent, TextContent
from persona.schema.conversation import ConversationMessage

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends.types import ToolSpec

# --------------------------------------------------------------------------- #
# Scripted ChatBackend double
# --------------------------------------------------------------------------- #


class _ScriptedBackend:
    """A ChatBackend that returns or raises whatever it's scripted with.

    ``script`` is a list of "outcomes" consumed one per :meth:`chat` (or
    :meth:`chat_stream`) call. Each outcome is either:

    * an :class:`Exception` instance to raise, or
    * a :class:`ChatResponse` to return (for ``chat``), or
    * a list of :class:`StreamChunk` (or a partial list ending in an
      :class:`Exception`) for ``chat_stream``.

    When the script is exhausted the backend raises :class:`IndexError` so
    bugs in the wrapper that over-invoke a backend surface immediately.
    """

    def __init__(
        self,
        provider: str,
        model: str,
        script: list[object],
        *,
        supports_native_tools: bool = True,
        supports_vision: bool = False,
    ) -> None:
        self._provider = provider
        self._model = model
        self._script = list(script)
        self._supports_native_tools = supports_native_tools
        self._supports_vision = supports_vision
        self.call_count = 0
        self.stream_call_count = 0
        #: Sampling kwargs seen on the most recent chat / chat_stream call.
        self.last_sampling: dict[str, object] = {}

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
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> ChatResponse:
        self.call_count += 1
        self.last_sampling = {"temperature": temperature, "top_p": top_p, "top_k": top_k}
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
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.stream_call_count += 1
        self.last_sampling = {"temperature": temperature, "top_p": top_p, "top_k": top_k}
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        assert isinstance(outcome, list)
        for item in outcome:
            if isinstance(item, Exception):
                raise item
            assert isinstance(item, StreamChunk)
            yield item


def _ok_response(provider: str, model: str, content: str = "ok") -> ChatResponse:
    return ChatResponse(
        content=content,
        usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
        model=model,
        provider=provider,
        latency_ms=1.0,
    )


def _user_msg() -> ConversationMessage:
    return ConversationMessage(role="user", content="hi", created_at=datetime.now(UTC))


def _vision_user_msg() -> ConversationMessage:
    """A user turn carrying an ImageContent block (a 'vision request')."""
    return ConversationMessage(
        role="user",
        content=[
            TextContent(text="what is this?"),
            ImageContent(workspace_path="uploads/a.png", media_type="image/png"),
        ],
        created_at=datetime.now(UTC),
    )


# --------------------------------------------------------------------------- #
# Construction invariants
# --------------------------------------------------------------------------- #


class TestConstruction:
    def test_empty_backends_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="at least one backend"):
            MultiModelChatBackend([])

    def test_single_backend_is_valid(self) -> None:
        backend = _ScriptedBackend("openai", "gpt-4o", [_ok_response("openai", "gpt-4o")])
        wrapper = MultiModelChatBackend([backend])
        assert wrapper.provider_name == "openai"
        assert wrapper.model_name == "gpt-4o"

    def test_is_chat_backend_protocol_member(self) -> None:
        backend = _ScriptedBackend("openai", "gpt-4o", [])
        wrapper = MultiModelChatBackend([backend])
        assert isinstance(wrapper, ChatBackend)

    def test_capability_properties_permissive_any(self) -> None:
        """``supports_native_tools`` + ``supports_vision`` use ``any()``
        semantics (permissive ceiling), NOT ``all()`` (conservative floor).

        Earlier ``all(...)`` semantics broke the runtime tool-call protocol on
        mixed-capability chains (production bug 2026-06-10 19:44 UTC): the
        wrapper claimed False when one slot's specific model wasn't in the
        native-tools allow-list; the runtime skipped the
        ``assistant_with_tool_calls`` append; ``format_tool_result`` still
        emitted ``role="tool"``; orphaned-tool-message on the next round →
        DeepSeek 400. Fix: at call time the wrapper dispatches to whichever
        backend serves the request — if ANY supports the capability, the
        wrapper can deliver it (via fallback if needed). The active backend's
        own property still gates whether THAT backend uses native vs shim.
        """
        a = _ScriptedBackend(
            "openai", "gpt-4o", [], supports_native_tools=True, supports_vision=True
        )
        b = _ScriptedBackend(
            "anthropic", "claude", [], supports_native_tools=False, supports_vision=True
        )
        wrapper = MultiModelChatBackend([a, b])
        # ANY supports native → wrapper reports True (was False under all()).
        assert wrapper.supports_native_tools is True
        # ANY supports vision → wrapper reports True.
        assert wrapper.supports_vision is True

    def test_capability_properties_all_false_when_none_support(self) -> None:
        """When NO backend supports the capability, the wrapper reports False.

        Regression gate: ``any([False, False])`` is False — the wrapper
        correctly fail-loud propagates this so callers requesting an
        unsupported capability hit fail-loud at the wrapper, not a confusing
        400 from one specific backend mid-fallback.
        """
        a = _ScriptedBackend(
            "openai", "gpt-4o", [], supports_native_tools=False, supports_vision=False
        )
        b = _ScriptedBackend(
            "anthropic", "claude", [], supports_native_tools=False, supports_vision=False
        )
        wrapper = MultiModelChatBackend([a, b])
        assert wrapper.supports_native_tools is False
        assert wrapper.supports_vision is False


# --------------------------------------------------------------------------- #
# Happy paths
# --------------------------------------------------------------------------- #


class TestHappyPath:
    @pytest.mark.asyncio
    async def test_single_backend_success(self) -> None:
        backend = _ScriptedBackend("openai", "gpt-4o", [_ok_response("openai", "gpt-4o")])
        wrapper = MultiModelChatBackend([backend])
        response = await wrapper.chat([_user_msg()])
        assert response.provider == "openai"
        assert backend.call_count == 1

    @pytest.mark.asyncio
    async def test_two_backends_primary_succeeds_no_fallback(self) -> None:
        primary = _ScriptedBackend(
            "openai", "gpt-4o", [_ok_response("openai", "gpt-4o", "primary")]
        )
        secondary = _ScriptedBackend(
            "anthropic", "claude", [_ok_response("anthropic", "claude", "secondary")]
        )
        wrapper = MultiModelChatBackend([primary, secondary])
        response = await wrapper.chat([_user_msg()])
        assert response.content == "primary"
        assert primary.call_count == 1
        assert secondary.call_count == 0

    @pytest.mark.asyncio
    async def test_rate_limit_then_success_retries_same_model(self) -> None:
        """RateLimitError without Retry-After → N=1 retry, primary succeeds on retry."""
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [
                RateLimitError("rl", context={"provider": "openai"}),
                _ok_response("openai", "gpt-4o", "after-retry"),
            ],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        wrapper = MultiModelChatBackend([primary, secondary])
        response = await wrapper.chat([_user_msg()])
        assert response.content == "after-retry"
        assert primary.call_count == 2
        assert secondary.call_count == 0


# --------------------------------------------------------------------------- #
# D-20-9 RETRY-THEN-FALLBACK bucket
# --------------------------------------------------------------------------- #


class TestRetryThenFallback:
    @pytest.mark.asyncio
    async def test_timeout_exhausts_retry_then_falls_back(self) -> None:
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [
                BackendTimeoutError("t1", context={"provider": "openai"}),
                BackendTimeoutError("t2", context={"provider": "openai"}),
            ],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        wrapper = MultiModelChatBackend([primary, secondary])
        response = await wrapper.chat([_user_msg()])
        assert response.provider == "anthropic"
        assert primary.call_count == 2  # original + 1 retry
        assert secondary.call_count == 1

    @pytest.mark.asyncio
    async def test_provider_error_5xx_retry_then_fallback(self) -> None:
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [
                ProviderError("500", context={"provider": "openai", "status_code": "500"}),
                ProviderError("500", context={"provider": "openai", "status_code": "500"}),
            ],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        wrapper = MultiModelChatBackend([primary, secondary])
        response = await wrapper.chat([_user_msg()])
        assert response.provider == "anthropic"
        assert primary.call_count == 2

    @pytest.mark.asyncio
    async def test_max_retries_zero_disables_retry(self) -> None:
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [BackendTimeoutError("t1", context={"provider": "openai"})],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        wrapper = MultiModelChatBackend([primary, secondary], max_retries_per_backend=0)
        response = await wrapper.chat([_user_msg()])
        assert response.provider == "anthropic"
        assert primary.call_count == 1


# --------------------------------------------------------------------------- #
# D-20-9 FALLBACK-NO-RETRY bucket
# --------------------------------------------------------------------------- #


class TestFallbackNoRetry:
    @pytest.mark.asyncio
    async def test_rate_limit_with_long_retry_after_skips_retry(self) -> None:
        """Retry-After=10s > 2s cutoff → FALLBACK-NO-RETRY; secondary tried."""
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [RateLimitError("rl", context={"provider": "openai", "retry_after_s": "10"})],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        wrapper = MultiModelChatBackend([primary, secondary])
        response = await wrapper.chat([_user_msg()])
        assert response.provider == "anthropic"
        assert primary.call_count == 1  # no retry

    @pytest.mark.asyncio
    async def test_rate_limit_with_credits_expired_reason_no_retry(self) -> None:
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [RateLimitError("rl", context={"provider": "openai", "reason": "credits_expired"})],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        wrapper = MultiModelChatBackend([primary, secondary])
        response = await wrapper.chat([_user_msg()])
        assert response.provider == "anthropic"
        assert primary.call_count == 1

    @pytest.mark.asyncio
    async def test_model_not_found_falls_back_no_retry(self) -> None:
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [ModelNotFoundError("nope", context={"provider": "openai", "model": "gpt-4o"})],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        wrapper = MultiModelChatBackend([primary, secondary])
        response = await wrapper.chat([_user_msg()])
        assert response.provider == "anthropic"
        assert primary.call_count == 1

    @pytest.mark.asyncio
    async def test_authentication_error_skip_and_fallback_with_warning(self) -> None:
        """D-20-12 — cross-provider auth → SKIP-AND-FALLBACK + WARNING log."""
        from loguru import logger as _loguru_logger

        captured: list[str] = []
        sink_id = _loguru_logger.add(
            lambda msg: captured.append(str(msg)),
            level="WARNING",
            serialize=True,
        )
        try:
            primary = _ScriptedBackend(
                "openai",
                "gpt-4o",
                [AuthenticationError("bad key", context={"provider": "openai"})],
            )
            secondary = _ScriptedBackend(
                "anthropic", "claude", [_ok_response("anthropic", "claude")]
            )
            wrapper = MultiModelChatBackend([primary, secondary], tier_name="frontier")
            response = await wrapper.chat([_user_msg()])
        finally:
            _loguru_logger.remove(sink_id)
        assert response.provider == "anthropic"
        assert primary.call_count == 1
        # WARNING log must mention the fallback engagement.
        joined = "".join(captured)
        assert "fallback" in joined.lower()
        assert "AuthenticationError" in joined

    @pytest.mark.asyncio
    async def test_model_unavailable_403_falls_back_no_retry(self) -> None:
        """R9-073a — production repro: a permanent 403 model-unavailability
        (Cloudflare "not available on the Workers Free plan") on the primary
        must NOT retry the same model and must NOT propagate out of the turn;
        the next model in the tier serves the reply."""
        from loguru import logger as _loguru_logger

        captured: list[str] = []
        sink_id = _loguru_logger.add(
            lambda msg: captured.append(str(msg)),
            level="WARNING",
            serialize=True,
        )
        try:
            primary = _ScriptedBackend(
                "cloudflare",
                "@cf/zai-org/glm-5.2",
                [
                    ModelUnavailableError(
                        "Model @cf/zai-org/glm-5.2 is not available on the Workers Free plan",
                        context={"provider": "cloudflare", "model": "@cf/zai-org/glm-5.2"},
                    )
                ],
            )
            secondary = _ScriptedBackend(
                "nvidia",
                "nemotron-3-super-120b-a12b",
                [_ok_response("nvidia", "nemotron-3-super-120b-a12b", "served by fallback")],
            )
            wrapper = MultiModelChatBackend([primary, secondary], tier_name="frontier")
            response = await wrapper.chat([_user_msg()])
        finally:
            _loguru_logger.remove(sink_id)
        assert response.provider == "nvidia"
        assert response.content == "served by fallback"
        # No pointless retry against the same (permanently unavailable) model.
        assert primary.call_count == 1
        assert secondary.call_count == 1
        # Operator-visible WARNING in the same shape as the transient path.
        joined = "".join(captured)
        assert "fallback" in joined.lower()
        assert "ModelUnavailableError" in joined
        assert "cloudflare" in joined

    @pytest.mark.asyncio
    async def test_model_unavailable_403_third_model_in_tier_serves(self) -> None:
        """Mirrors the exact production tier order: two usable fallbacks
        behind a permanently-403ing primary — the turn must be served, not
        killed."""
        cloudflare = _ScriptedBackend(
            "cloudflare",
            "@cf/zai-org/glm-5.2",
            [
                ModelUnavailableError(
                    "not available on the Workers Free plan",
                    context={"provider": "cloudflare", "model": "@cf/zai-org/glm-5.2"},
                )
            ],
        )
        nvidia = _ScriptedBackend(
            "nvidia",
            "nemotron-3-super-120b-a12b",
            [
                ModelUnavailableError(
                    "also unavailable",
                    context={"provider": "nvidia", "model": "nemotron-3-super-120b-a12b"},
                )
            ],
        )
        anthropic_backend = _ScriptedBackend(
            "anthropic",
            "claude-sonnet-4-6",
            [_ok_response("anthropic", "claude-sonnet-4-6", "final answer")],
        )
        wrapper = MultiModelChatBackend(
            [cloudflare, nvidia, anthropic_backend], tier_name="frontier"
        )
        response = await wrapper.chat([_user_msg()])
        assert response.content == "final answer"
        assert cloudflare.call_count == 1
        assert nvidia.call_count == 1
        assert anthropic_backend.call_count == 1

    @pytest.mark.asyncio
    async def test_provider_credential_missing_runtime_falls_back(self) -> None:
        """D-20-15 runtime path — resolver did not catch this slot earlier."""
        primary = _ScriptedBackend(
            "nvidia",
            "nemotron",
            [
                ProviderCredentialMissingError(
                    "missing",
                    context={"provider": "nvidia", "env_var": "PERSONA_NVIDIA_API_KEY"},
                )
            ],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        wrapper = MultiModelChatBackend([primary, secondary])
        response = await wrapper.chat([_user_msg()])
        assert response.provider == "anthropic"
        assert primary.call_count == 1


# --------------------------------------------------------------------------- #
# D-20-9 SURFACE bucket
# --------------------------------------------------------------------------- #


class TestSurface:
    @pytest.mark.asyncio
    async def test_content_policy_violation_surfaces_no_fallback(self) -> None:
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [
                ProviderError(
                    "blocked",
                    context={
                        "provider": "openai",
                        "status_code": "400",
                        "reason": "content_policy_violation",
                    },
                )
            ],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        wrapper = MultiModelChatBackend([primary, secondary])
        with pytest.raises(ProviderError):
            await wrapper.chat([_user_msg()])
        assert primary.call_count == 1
        assert secondary.call_count == 0

    @pytest.mark.asyncio
    async def test_bad_request_400_generic_surfaces(self) -> None:
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [ProviderError("bad", context={"provider": "openai", "status_code": "400"})],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        wrapper = MultiModelChatBackend([primary, secondary])
        with pytest.raises(ProviderError):
            await wrapper.chat([_user_msg()])
        assert secondary.call_count == 0

    @pytest.mark.asyncio
    async def test_non_persona_error_surfaces_as_programmer_bug(self) -> None:
        primary = _ScriptedBackend("openai", "gpt-4o", [TypeError("bug")])
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        wrapper = MultiModelChatBackend([primary, secondary])
        with pytest.raises(TypeError):
            await wrapper.chat([_user_msg()])
        assert secondary.call_count == 0


# --------------------------------------------------------------------------- #
# Exhaustion → AllModelsFailedError (D-20-16)
# --------------------------------------------------------------------------- #


class TestExhaustion:
    @pytest.mark.asyncio
    async def test_all_three_rate_limit_raises_all_models_failed(self) -> None:
        b1 = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [
                RateLimitError("rl", context={"provider": "openai", "retry_after_s": "10"}),
            ],
        )
        b2 = _ScriptedBackend(
            "anthropic",
            "claude",
            [
                RateLimitError("rl", context={"provider": "anthropic", "retry_after_s": "10"}),
            ],
        )
        b3 = _ScriptedBackend(
            "nvidia",
            "nemotron",
            [
                RateLimitError("rl", context={"provider": "nvidia", "retry_after_s": "10"}),
            ],
        )
        wrapper = MultiModelChatBackend([b1, b2, b3], tier_name="frontier")
        with pytest.raises(AllModelsFailedError) as excinfo:
            await wrapper.chat([_user_msg()])
        err = excinfo.value
        assert isinstance(err, PersonaError)
        assert err.context["tier"] == "frontier"
        assert err.context["attempt_count"] == "3"
        assert err.context["final_error_class"] == "RateLimitError"
        # Each backend hit once (no retry — Retry-After=10s skips retry).
        assert b1.call_count == 1
        assert b2.call_count == 1
        assert b3.call_count == 1

    @pytest.mark.asyncio
    async def test_all_models_permanently_unavailable_surfaces_never_silent(self) -> None:
        """R9-073a — every model in the tier is permanently 403'd: the error
        MUST surface to the caller (AllModelsFailedError), never a silent
        empty reply."""
        b1 = _ScriptedBackend(
            "cloudflare",
            "@cf/zai-org/glm-5.2",
            [ModelUnavailableError("nope", context={"provider": "cloudflare"})],
        )
        b2 = _ScriptedBackend(
            "nvidia",
            "nemotron",
            [ModelUnavailableError("nope", context={"provider": "nvidia"})],
        )
        wrapper = MultiModelChatBackend([b1, b2], tier_name="frontier")
        with pytest.raises(AllModelsFailedError) as excinfo:
            await wrapper.chat([_user_msg()])
        assert excinfo.value.context["attempt_count"] == "2"
        assert excinfo.value.context["final_error_class"] == "ModelUnavailableError"
        # Neither backend was pointlessly retried against itself.
        assert b1.call_count == 1
        assert b2.call_count == 1

    @pytest.mark.asyncio
    async def test_all_models_failed_is_not_provider_error_d20_16(self) -> None:
        """D-20-16 partition — AllModelsFailedError is PersonaError, NOT ProviderError."""
        backend = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [RateLimitError("rl", context={"provider": "openai", "retry_after_s": "10"})],
        )
        wrapper = MultiModelChatBackend([backend])
        with pytest.raises(AllModelsFailedError) as excinfo:
            await wrapper.chat([_user_msg()])
        # MUST NOT be catchable as ProviderError.
        assert not isinstance(excinfo.value, ProviderError)
        assert isinstance(excinfo.value, PersonaError)


# --------------------------------------------------------------------------- #
# AttemptRecord shape
# --------------------------------------------------------------------------- #


class TestAttemptRecord:
    def test_attempt_record_is_frozen_dataclass(self) -> None:
        rec = AttemptRecord(
            provider="openai",
            model="gpt-4o",
            last_error_class="RateLimitError",
            last_error_status_code=429,
            retried_same_model=True,
        )
        with pytest.raises(FrozenInstanceError):
            rec.provider = "anthropic"  # type: ignore[misc]

    @pytest.mark.asyncio
    async def test_attempts_carry_retried_flag(self) -> None:
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [
                BackendTimeoutError("t1", context={"provider": "openai"}),
                BackendTimeoutError("t2", context={"provider": "openai"}),
            ],
        )
        secondary = _ScriptedBackend(
            "anthropic",
            "claude",
            [RateLimitError("rl", context={"provider": "anthropic", "retry_after_s": "10"})],
        )
        wrapper = MultiModelChatBackend([primary, secondary])
        with pytest.raises(AllModelsFailedError) as excinfo:
            await wrapper.chat([_user_msg()])
        # attempts_json carries the retried flag — verify by substring.
        attempts_json = excinfo.value.context["attempts_json"]
        assert "'retried_same_model': True" in attempts_json  # primary retried
        assert "'retried_same_model': False" in attempts_json  # secondary did not


# --------------------------------------------------------------------------- #
# Streaming behaviour
# --------------------------------------------------------------------------- #


def _chunk(delta: str, *, is_final: bool = False) -> StreamChunk:
    usage = TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2) if is_final else None
    return StreamChunk(delta=delta, is_final=is_final, usage=usage)


class TestStreaming:
    @pytest.mark.asyncio
    async def test_stream_fallback_before_first_chunk(self) -> None:
        """Error BEFORE any chunk yielded → wrapper falls back per D-20-9."""
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [BackendTimeoutError("t1", context={"provider": "openai"})] * 2,
        )
        secondary = _ScriptedBackend(
            "anthropic",
            "claude",
            [[_chunk("hello"), _chunk("", is_final=True)]],
        )
        wrapper = MultiModelChatBackend([primary, secondary])
        chunks: list[StreamChunk] = []
        async for c in wrapper.chat_stream([_user_msg()]):
            chunks.append(c)
        assert len(chunks) == 2
        assert chunks[0].delta == "hello"
        assert chunks[-1].is_final is True

    @pytest.mark.asyncio
    async def test_stream_error_after_first_chunk_surfaces(self) -> None:
        """Error AFTER first chunk → surface; secondary NOT tried."""
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [[_chunk("partial"), BackendTimeoutError("mid", context={"provider": "openai"})]],
        )
        secondary = _ScriptedBackend(
            "anthropic",
            "claude",
            [[_chunk("", is_final=True)]],
        )
        wrapper = MultiModelChatBackend([primary, secondary])
        received: list[StreamChunk] = []

        async def _consume() -> None:
            async for c in wrapper.chat_stream([_user_msg()]):
                received.append(c)

        with pytest.raises(BackendTimeoutError):
            await _consume()
        assert received == [_chunk("partial")]
        assert secondary.stream_call_count == 0

    @pytest.mark.asyncio
    async def test_stream_single_backend_success(self) -> None:
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [[_chunk("a"), _chunk("b"), _chunk("", is_final=True)]],
        )
        wrapper = MultiModelChatBackend([primary])
        chunks: list[StreamChunk] = []
        async for c in wrapper.chat_stream([_user_msg()]):
            chunks.append(c)
        assert [c.delta for c in chunks] == ["a", "b", ""]
        assert chunks[-1].is_final is True


class TestEmptyCompletion:
    """R9-033 — an empty completion is a provider failure, not a reply.

    Observed live 2026-07-13 (twice): a frontier-tier turn streamed zero
    ``chunk`` events, the wrapper treated the clean-but-empty stream as
    success, and an empty assistant message persisted silently. An empty
    final completion (no non-whitespace text AND no tool calls) must engage
    the SAME retry-then-fallback walk any transient ``ProviderError`` does.
    """

    @pytest.mark.asyncio
    async def test_stream_empty_completion_falls_back(self) -> None:
        """A zero-content stream → retry the primary once, then fall back."""
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [[_chunk("", is_final=True)], [_chunk("", is_final=True)]],
        )
        secondary = _ScriptedBackend(
            "anthropic",
            "claude",
            [[_chunk("hello"), _chunk("", is_final=True)]],
        )
        wrapper = MultiModelChatBackend([primary, secondary])
        chunks: list[StreamChunk] = []
        async for c in wrapper.chat_stream([_user_msg()]):
            chunks.append(c)
        # Only the SECONDARY's chunks reach the caller — the primary's empty
        # stream (usage-only final chunk) is discarded, never surfaced.
        assert "".join(c.delta for c in chunks) == "hello"
        assert primary.stream_call_count == 2  # D-20-10: one same-model retry
        assert secondary.stream_call_count == 1
        # The attempt ledger records the empty completion as a provider failure.
        assert len(wrapper.last_attempts) == 1
        assert wrapper.last_attempts[0].last_error_class == "EmptyCompletionError"
        assert wrapper.last_attempts[0].retried_same_model is True

    @pytest.mark.asyncio
    async def test_stream_whitespace_only_completion_falls_back(self) -> None:
        """Whitespace-only deltas carry no reply — same failure as zero chunks."""
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [
                [_chunk("  \n"), _chunk("", is_final=True)],
                [_chunk("  \n"), _chunk("", is_final=True)],
            ],
        )
        secondary = _ScriptedBackend(
            "anthropic",
            "claude",
            [[_chunk("real answer"), _chunk("", is_final=True)]],
        )
        wrapper = MultiModelChatBackend([primary, secondary])
        chunks: list[StreamChunk] = []
        async for c in wrapper.chat_stream([_user_msg()]):
            chunks.append(c)
        assert "".join(c.delta for c in chunks) == "real answer"
        assert secondary.stream_call_count == 1

    @pytest.mark.asyncio
    async def test_stream_tool_call_only_completion_is_success(self) -> None:
        """A tool-call-only stream is a VALID reply — no fallback engaged."""
        from persona.backends.types import ToolCallDelta

        tool_chunk = StreamChunk(
            delta="",
            tool_call_delta=ToolCallDelta(call_id="c1", name_delta="echo", arguments_delta="{}"),
        )
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [[tool_chunk, _chunk("", is_final=True)]],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [])
        wrapper = MultiModelChatBackend([primary, secondary])
        chunks: list[StreamChunk] = []
        async for c in wrapper.chat_stream([_user_msg()]):
            chunks.append(c)
        assert any(c.tool_call_delta is not None for c in chunks)
        assert secondary.stream_call_count == 0
        assert wrapper.last_attempts == []

    @pytest.mark.asyncio
    async def test_stream_all_backends_empty_exhausts_to_all_models_failed(self) -> None:
        """Every backend empty → AllModelsFailedError, never a silent empty reply."""
        primary = _ScriptedBackend("openai", "gpt-4o", [[_chunk("", is_final=True)]] * 2)
        secondary = _ScriptedBackend("anthropic", "claude", [[_chunk("", is_final=True)]] * 2)
        wrapper = MultiModelChatBackend([primary, secondary], tier_name="frontier")

        async def _consume() -> None:
            async for _ in wrapper.chat_stream([_user_msg()]):
                pass

        with pytest.raises(AllModelsFailedError) as excinfo:
            await _consume()
        assert excinfo.value.context["final_error_class"] == "EmptyCompletionError"

    @pytest.mark.asyncio
    async def test_chat_empty_completion_falls_back(self) -> None:
        """Non-streaming: an empty ChatResponse engages the same walk."""
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [_ok_response("openai", "gpt-4o", ""), _ok_response("openai", "gpt-4o", "")],
        )
        secondary = _ScriptedBackend(
            "anthropic", "claude", [_ok_response("anthropic", "claude", "real")]
        )
        wrapper = MultiModelChatBackend([primary, secondary])
        response = await wrapper.chat([_user_msg()])
        assert response.content == "real"
        assert primary.call_count == 2  # one same-model retry consumed
        assert wrapper.last_attempts[0].last_error_class == "EmptyCompletionError"

    @pytest.mark.asyncio
    async def test_chat_tool_only_response_is_success(self) -> None:
        """content='' with tool_calls is the documented tool-only reply — success."""
        from persona.schema.tools import ToolCall

        tool_response = ChatResponse(
            content="",
            tool_calls=[ToolCall(name="echo", args={}, call_id="c1")],
            usage=TokenUsage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
            model="gpt-4o",
            provider="openai",
            latency_ms=1.0,
        )
        primary = _ScriptedBackend("openai", "gpt-4o", [tool_response])
        secondary = _ScriptedBackend("anthropic", "claude", [])
        wrapper = MultiModelChatBackend([primary, secondary])
        response = await wrapper.chat([_user_msg()])
        assert response.tool_calls
        assert secondary.call_count == 0
        assert wrapper.last_attempts == []

    def test_empty_completion_error_is_provider_error(self) -> None:
        """The class slots under ProviderError so every existing handler catches it."""
        from persona.backends.errors import EmptyCompletionError

        assert issubclass(EmptyCompletionError, ProviderError)


class TestSamplingPassThrough:
    """The wrapper forwards top_p / top_k verbatim to the active backend."""

    @pytest.mark.asyncio
    async def test_chat_forwards_top_p_and_top_k(self) -> None:
        backend = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        wrapper = MultiModelChatBackend([backend])
        await wrapper.chat([_user_msg()], temperature=0.9, top_p=0.95, top_k=60)
        assert backend.last_sampling == {"temperature": 0.9, "top_p": 0.95, "top_k": 60}

    @pytest.mark.asyncio
    async def test_chat_defaults_leave_sampling_unset(self) -> None:
        backend = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        wrapper = MultiModelChatBackend([backend])
        await wrapper.chat([_user_msg()])
        assert backend.last_sampling == {"temperature": 0.0, "top_p": None, "top_k": None}

    @pytest.mark.asyncio
    async def test_stream_forwards_top_p_and_top_k(self) -> None:
        backend = _ScriptedBackend("ollama", "llama", [[_chunk("a"), _chunk("", is_final=True)]])
        wrapper = MultiModelChatBackend([backend])
        async for _ in wrapper.chat_stream([_user_msg()], temperature=0.9, top_p=0.9, top_k=40):
            pass
        assert backend.last_sampling == {"temperature": 0.9, "top_p": 0.9, "top_k": 40}


# --------------------------------------------------------------------------- #
# Vision-aware in-tier selection + honest failure (image-workspace cascade)
# --------------------------------------------------------------------------- #


class TestVisionAwareSelection:
    """The wrapper must route an image-bearing request to a vision-capable
    candidate, fall through a vision-incapable candidate's refusal, and fail
    loud (never silently text-only) when NO candidate supports vision.

    Regression guard: a text-only request keeps first-candidate fallback
    semantics byte-for-byte (no vision reordering when no image is present).
    """

    @pytest.mark.asyncio
    async def test_image_request_prefers_vision_candidate_chat(self) -> None:
        """[non-vision, vision] + image → the vision candidate serves the call.

        The non-vision primary must NOT be invoked for an image turn; the
        wrapper reorders so the vision-capable backend (e.g. Claude) is tried.
        """
        non_vision = _ScriptedBackend("deepseek", "deepseek-chat", [], supports_vision=False)
        vision = _ScriptedBackend(
            "anthropic",
            "claude",
            [_ok_response("anthropic", "claude", "saw the image")],
            supports_vision=True,
        )
        wrapper = MultiModelChatBackend([non_vision, vision])
        response = await wrapper.chat([_vision_user_msg()])
        assert response.content == "saw the image"
        assert non_vision.call_count == 0
        assert vision.call_count == 1

    @pytest.mark.asyncio
    async def test_image_request_prefers_vision_candidate_stream(self) -> None:
        non_vision = _ScriptedBackend("deepseek", "deepseek-chat", [], supports_vision=False)
        vision = _ScriptedBackend(
            "anthropic",
            "claude",
            [[_chunk("looks like a cat"), _chunk("", is_final=True)]],
            supports_vision=True,
        )
        wrapper = MultiModelChatBackend([non_vision, vision])
        deltas = [c.delta async for c in wrapper.chat_stream([_vision_user_msg()])]
        assert "looks like a cat" in deltas
        assert non_vision.stream_call_count == 0
        assert vision.stream_call_count == 1

    @pytest.mark.asyncio
    async def test_vision_not_supported_error_falls_through(self) -> None:
        """A vision-capable-by-flag candidate that still raises
        BackendVisionNotSupportedError must FALL THROUGH to the next
        vision-capable candidate (reclassified away from SURFACE)."""
        first_vision = _ScriptedBackend(
            "openai",
            "gpt-x",
            [
                BackendVisionNotSupportedError(
                    "no workspace_root configured for image resolution",
                    context={"backend": "openai", "model": "gpt-x", "image_count": "1"},
                )
            ],
            supports_vision=True,
        )
        second_vision = _ScriptedBackend(
            "anthropic",
            "claude",
            [_ok_response("anthropic", "claude", "recovered")],
            supports_vision=True,
        )
        wrapper = MultiModelChatBackend([first_vision, second_vision])
        response = await wrapper.chat([_vision_user_msg()])
        assert response.content == "recovered"
        assert first_vision.call_count == 1
        assert second_vision.call_count == 1

    @pytest.mark.asyncio
    async def test_no_vision_candidate_raises_honest_domain_error_chat(self) -> None:
        """[non-vision only] + image → NoVisionCapableModelError.

        NOT a silent text-only call (the backend must never be invoked), NOT
        an opaque AllModelsFailedError/surface — a clear domain error the loop
        can surface as "no vision-capable model is configured".
        """
        non_vision = _ScriptedBackend("deepseek", "deepseek-chat", [], supports_vision=False)
        wrapper = MultiModelChatBackend([non_vision])
        with pytest.raises(NoVisionCapableModelError):
            await wrapper.chat([_vision_user_msg()])
        # The non-vision backend must NOT have been called text-only.
        assert non_vision.call_count == 0

    @pytest.mark.asyncio
    async def test_no_vision_candidate_raises_honest_domain_error_stream(self) -> None:
        non_vision = _ScriptedBackend("deepseek", "deepseek-chat", [], supports_vision=False)
        wrapper = MultiModelChatBackend([non_vision])
        with pytest.raises(NoVisionCapableModelError):
            async for _ in wrapper.chat_stream([_vision_user_msg()]):
                pass
        assert non_vision.stream_call_count == 0

    @pytest.mark.asyncio
    async def test_no_vision_capable_model_error_is_persona_error_not_provider(self) -> None:
        """D-20-16 partition: the honest failure is wrapper-layer (PersonaError),
        NOT a ProviderError."""
        assert issubclass(NoVisionCapableModelError, PersonaError)
        assert not issubclass(NoVisionCapableModelError, ProviderError)

    @pytest.mark.asyncio
    async def test_text_request_keeps_first_candidate_fallback(self) -> None:
        """Regression: a no-image turn is unaffected by vision reordering — the
        FIRST candidate is tried first even if a later one is vision-capable."""
        primary = _ScriptedBackend(
            "deepseek",
            "deepseek-chat",
            [_ok_response("deepseek", "deepseek-chat", "primary-text")],
            supports_vision=False,
        )
        vision = _ScriptedBackend("anthropic", "claude", [], supports_vision=True)
        wrapper = MultiModelChatBackend([primary, vision])
        response = await wrapper.chat([_user_msg()])
        assert response.content == "primary-text"
        assert primary.call_count == 1
        assert vision.call_count == 0


# --------------------------------------------------------------------------- #
# R9-124: a retired model walks the chain
# --------------------------------------------------------------------------- #
class TestRetiredModel:
    """A model the provider withdrew must never stop the chain.

    NVIDIA retired ``meta/llama-3.3-70b-instruct`` on 2026-08-26 and answered every call
    with ``410 Gone``. That status had no fallback rule, so it SURFACED and the chain never
    reached Claude, sitting right behind it. Every voice turn routed there died in silence
    for ten days.
    """

    @pytest.mark.asyncio
    async def test_a_410_gone_falls_over_to_the_next_model(self) -> None:
        primary = _ScriptedBackend(
            "nvidia",
            "meta/llama-3.3-70b-instruct",
            [ProviderError("Gone", context={"provider": "nvidia", "status_code": "410"})],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        response = await MultiModelChatBackend([primary, secondary]).chat([_user_msg()])
        assert response.model == "claude"
        assert primary.call_count == 1, "a retired model is never retried"
        assert secondary.call_count == 1

    @pytest.mark.asyncio
    async def test_a_retirement_body_without_a_usable_status_falls_over(self) -> None:
        """Groq announces a decommission in the body of a 400; the words are the signal."""
        primary = _ScriptedBackend(
            "groq",
            "llama-3.3-70b-versatile",
            [
                ProviderError(
                    "The model `llama-3.3-70b-versatile` has been decommissioned",
                    context={"provider": "groq", "status_code": "400"},
                )
            ],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        response = await MultiModelChatBackend([primary, secondary]).chat([_user_msg()])
        assert response.model == "claude"

    @pytest.mark.asyncio
    async def test_the_stream_path_walks_past_a_retired_model_too(self) -> None:
        primary = _ScriptedBackend(
            "nvidia",
            "meta/llama-3.3-70b-instruct",
            [ProviderError("Gone", context={"provider": "nvidia", "status_code": "410"})],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [[_chunk("ok", is_final=True)]])
        wrapper = MultiModelChatBackend([primary, secondary])
        deltas = [c.delta async for c in wrapper.chat_stream([_user_msg()])]
        assert "".join(deltas) == "ok"

    @pytest.mark.asyncio
    async def test_a_plain_bad_request_still_surfaces(self) -> None:
        """Scope guard: a 400 with no retirement body is the caller's bug, as before."""
        primary = _ScriptedBackend(
            "openai",
            "gpt-4o",
            [ProviderError("bad", context={"provider": "openai", "status_code": "400"})],
        )
        secondary = _ScriptedBackend("anthropic", "claude", [_ok_response("anthropic", "claude")])
        with pytest.raises(ProviderError):
            await MultiModelChatBackend([primary, secondary]).chat([_user_msg()])
        assert secondary.call_count == 0
