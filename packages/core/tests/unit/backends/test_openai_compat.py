"""Tests for ``OpenAICompatibleBackend``.

Both Anthropic (via the ``anthropic`` SDK) and OpenAI/DeepSeek/Groq/Together
(via the ``openai`` SDK) are exercised with mocked clients. Real provider
calls live behind ``@pytest.mark.external`` (not in this file).
"""

# ruff: noqa: ANN401, SLF001 — mocks use Any return types; tests access private attrs

from __future__ import annotations

import os
from collections.abc import AsyncIterator  # noqa: TC003 — used at runtime in helpers
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import anthropic
import openai
import pytest
from persona.backends.config import BackendConfig
from persona.backends.errors import (
    AuthenticationError,
    BackendTimeoutError,
    ModelNotFoundError,
    ProviderError,
    RateLimitError,
)
from persona.backends.openai_compat import (
    _NATIVE_TOOLS_CAPABILITY,
    _VISION_CAPABILITY,
    OpenAICompatibleBackend,
    _message_to_anthropic,
    _message_to_openai,
    _native_tools_supported,
    _parse_openai_response,
    _strip_reasoning_for_provider,
    vision_supported,
)
from persona.backends.protocol import ChatBackend
from persona.backends.types import ChatResponse, StreamChunk, ToolSpec
from persona.schema.conversation import ConversationMessage  # noqa: TC001
from persona.schema.tools import ToolCall, ToolResult
from persona.tools import format_tool_result
from pydantic import SecretStr


def _user(text: str) -> ConversationMessage:
    return ConversationMessage(role="user", content=text, created_at=datetime.now(UTC))


def _config(
    provider: str, *, api_key: str = "test-key", model: str = "test-model"
) -> BackendConfig:
    return BackendConfig(
        provider=provider,  # type: ignore[arg-type]
        model=model,
        api_key=SecretStr(api_key),
    )


# -----------------------------------------------------------------------------
# Capability matrix
# -----------------------------------------------------------------------------


class TestCapabilityMatrix:
    def test_anthropic_is_all(self) -> None:
        assert _NATIVE_TOOLS_CAPABILITY["anthropic"] == "all"

    def test_openai_is_all(self) -> None:
        assert _NATIVE_TOOLS_CAPABILITY["openai"] == "all"

    def test_groq_is_frozenset(self) -> None:
        cap = _NATIVE_TOOLS_CAPABILITY["groq"]
        assert isinstance(cap, frozenset)
        assert "llama-3.3-70b-versatile" in cap

    def test_together_is_empty_frozenset(self) -> None:
        cap = _NATIVE_TOOLS_CAPABILITY["together"]
        assert cap == frozenset()

    def test_supported_anthropic_any_model(self) -> None:
        assert _native_tools_supported("anthropic", "claude-anything") is True

    def test_supported_groq_listed_model(self) -> None:
        assert _native_tools_supported("groq", "llama-3.3-70b-versatile") is True

    def test_unsupported_groq_unlisted_model(self) -> None:
        assert _native_tools_supported("groq", "whisper-large-v3") is False

    def test_nvidia_is_frozenset_with_launch_models(self) -> None:
        # Spec 20 T09 / D-20-1 launch set — three Nemotron models advertise
        # native tool calling at launch. Unlisted nvidia models fall through
        # to the prompt-based shim (same allow-list semantics as groq /
        # deepseek).
        cap = _NATIVE_TOOLS_CAPABILITY["nvidia"]
        assert isinstance(cap, frozenset)
        assert cap == frozenset(
            {
                "nvidia/llama-3.3-nemotron-super-49b-v1.5",
                "nvidia/nemotron-3-super-120b-a12b",
                "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning",
            }
        )

    def test_supported_nvidia_listed_model(self) -> None:
        assert _native_tools_supported("nvidia", "nvidia/llama-3.3-nemotron-super-49b-v1.5") is True

    def test_unsupported_nvidia_unlisted_model_falls_through_to_shim(self) -> None:
        # Allowlisted provider — unknown model returns False so the
        # OpenAICompatibleBackend uses the prompt-based shim instead of
        # asking the provider for native tool calls it can't service.
        assert _native_tools_supported("nvidia", "nvidia/some-unknown-model") is False


class TestOpenRouterCapabilityInference:
    """Spec 22 D-22-4 + D-22-10 — three-tier resolver (explicit > catalog >
    underlying-model). T08 ships tier 1 (explicit override) + tier 3
    (underlying-model inference); these exercise both plus the suffix
    taxonomy (D-22-6), the dual match key, and ``:free`` asymmetric
    conservatism (D-22-10c)."""

    def test_openrouter_rows_ship_empty(self) -> None:
        # D-22-10f: empty override rows; no pre-seeded entries to go stale.
        assert _NATIVE_TOOLS_CAPABILITY["openrouter"] == frozenset()
        assert _VISION_CAPABILITY["openrouter"] == frozenset()

    # --- tier 3: underlying-model inference ---

    def test_tools_inferred_from_anthropic_underlying(self) -> None:
        assert _native_tools_supported("openrouter", "anthropic/claude-3.5-sonnet") is True

    def test_vision_inferred_from_anthropic_underlying(self) -> None:
        assert vision_supported("openrouter", "anthropic/claude-3.5-sonnet") is True

    def test_tools_inferred_deepseek_bare_name_match(self) -> None:
        # Dual match key — the deepseek row holds the BARE name "deepseek-chat".
        assert _native_tools_supported("openrouter", "deepseek/deepseek-chat") is True

    def test_tools_inferred_nvidia_full_slug_match(self) -> None:
        # Dual match key — the nvidia row holds the FULL slug.
        assert (
            _native_tools_supported("openrouter", "nvidia/llama-3.3-nemotron-super-49b-v1.5")
            is True
        )

    def test_vision_free_suffix_falls_back_to_base(self) -> None:
        # D-22-10c: vision for a :free slug infers from the base slug.
        assert vision_supported("openrouter", "anthropic/claude-3.5-sonnet:free") is True

    def test_tools_free_suffix_forces_false(self) -> None:
        # D-22-10c asymmetric conservatism — tools→False for :free in tier 3.
        assert _native_tools_supported("openrouter", "anthropic/claude-3.5-sonnet:free") is False

    def test_dynamic_variant_stripped_for_inference(self) -> None:
        # D-22-6: :nitro is a routing transform — strip to base for inference.
        assert _native_tools_supported("openrouter", "anthropic/claude-3.5-sonnet:nitro") is True

    def test_unknown_variant_strips_to_base(self) -> None:
        # D-22-10d: unknown suffix strips to base (+ WARN side effect).
        assert _native_tools_supported("openrouter", "anthropic/claude-3.5-sonnet:weird") is True

    def test_unmapped_author_defaults_to_shim(self) -> None:
        # meta-llama / google have no matrix row → conservative shim/no-vision.
        assert _native_tools_supported("openrouter", "meta-llama/llama-3.3-70b-instruct") is False
        assert vision_supported("openrouter", "google/gemini-2.0-flash-001") is False

    # --- tier 1: explicit operator override ---

    def test_explicit_override_wins_over_inference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # An operator-listed full slug forces True even for an unmapped author.
        monkeypatch.setitem(
            _NATIVE_TOOLS_CAPABILITY,
            "openrouter",
            frozenset({"meta-llama/llama-3.3-70b-instruct"}),
        )
        assert _native_tools_supported("openrouter", "meta-llama/llama-3.3-70b-instruct") is True


class TestOpenRouterProvider:
    """Spec 22 T06 — OpenRouter as a first-class OpenAI-compatible provider."""

    def test_openrouter_in_default_base_urls(self) -> None:
        from persona.backends.config import DEFAULT_BASE_URLS

        assert DEFAULT_BASE_URLS["openrouter"] == "https://openrouter.ai/api/v1/"

    def test_construct_openrouter_backend(self) -> None:
        # D-20-X-allow-set-extend: the allow-set must include openrouter or
        # construction raises ProviderError. A successful build proves it.
        backend = OpenAICompatibleBackend(
            _config("openrouter", model="anthropic/claude-3.5-sonnet")
        )
        assert backend.provider_name == "openrouter"
        assert backend.model_name == "anthropic/claude-3.5-sonnet"

    def test_openrouter_backend_infers_capabilities_at_construction(self) -> None:
        backend = OpenAICompatibleBackend(
            _config("openrouter", model="anthropic/claude-3.5-sonnet")
        )
        assert backend.supports_native_tools is True
        assert backend.supports_vision is True

    def test_openrouter_free_slug_tools_false_at_construction(self) -> None:
        backend = OpenAICompatibleBackend(
            _config("openrouter", model="anthropic/claude-3.5-sonnet:free")
        )
        assert backend.supports_native_tools is False
        assert backend.supports_vision is True


class TestToolCallMessageProtocol:
    """The native tool-calling message round-trip (spec 11 soak findings).

    A native provider requires the assistant message that issued the tool_calls
    to precede the tool result, and the result's id to match the call's id.
    These regression-gate the four soak fixes that the (external) soak proved.
    """

    def _now(self) -> datetime:
        return datetime(2026, 5, 29, tzinfo=UTC)

    def test_openai_assistant_message_serialises_tool_calls(self) -> None:
        msg = ConversationMessage(
            role="assistant",
            content="searching…",
            created_at=self._now(),
            tool_calls=[ToolCall(name="web_search", args={"query": "mould"}, call_id="call_0")],
        )
        out = _message_to_openai(msg)
        assert out["role"] == "assistant"
        assert out["tool_calls"][0]["id"] == "call_0"
        assert out["tool_calls"][0]["type"] == "function"
        assert out["tool_calls"][0]["function"]["name"] == "web_search"
        # arguments is a JSON STRING per the OpenAI schema
        assert out["tool_calls"][0]["function"]["arguments"] == '{"query": "mould"}'

    def test_openai_assistant_without_tool_calls_unchanged(self) -> None:
        msg = ConversationMessage(role="assistant", content="hi", created_at=self._now())
        out = _message_to_openai(msg)
        assert "tool_calls" not in out
        assert out == {"role": "assistant", "content": "hi"}

    def test_openai_tool_call_only_assistant_emits_content_none_not_empty(self) -> None:
        """OpenAI spec strictness: when assistant message has tool_calls and no
        narration text, content MUST be None (or omitted) — NOT empty string.

        DeepSeek strict-rejects ``content=""`` + ``tool_calls=[...]`` ("Messages
        with role 'tool' must be a response to a preceding message with
        'tool_calls'" — the API silently drops the malformed assistant message,
        then orphans the next tool message). OpenAI itself is lenient. Hidden
        before Spec 20 because no cross-provider fallback existed; surfaces
        now that MultiModelChatBackend can hand off mid-turn from NVIDIA
        (lenient; Nemotron emits tool-call-only frequently) to DeepSeek
        (strict).
        """
        msg = ConversationMessage(
            role="assistant",
            content="",  # tool-call-only emission (common with Nemotron)
            created_at=self._now(),
            tool_calls=[ToolCall(name="web_search", args={"q": "x"}, call_id="c1")],
        )
        out = _message_to_openai(msg)
        assert out["content"] is None, "content must be None (not '') per OpenAI spec"
        assert out["tool_calls"][0]["id"] == "c1"

    def test_openai_whitespace_only_assistant_with_tool_calls_normalized(self) -> None:
        """Variant: whitespace-only content also normalizes to None."""
        msg = ConversationMessage(
            role="assistant",
            content="   \n  ",
            created_at=self._now(),
            tool_calls=[ToolCall(name="web_search", args={"q": "x"}, call_id="c1")],
        )
        out = _message_to_openai(msg)
        assert out["content"] is None

    def test_openai_narration_plus_tool_calls_preserves_content(self) -> None:
        """Narration text + tool_calls keeps content as-is (no normalization)."""
        msg = ConversationMessage(
            role="assistant",
            content="Searching the web…",
            created_at=self._now(),
            tool_calls=[ToolCall(name="web_search", args={"q": "x"}, call_id="c1")],
        )
        out = _message_to_openai(msg)
        assert out["content"] == "Searching the web…"

    def test_openai_tool_result_id_matches_the_call(self) -> None:
        # the whole pairing: assistant.tool_calls[].id == tool message tool_call_id
        call = ToolCall(name="web_search", args={"query": "x"}, call_id="call_42")
        result = ToolResult(tool_name="web_search", content="results", call_id="call_42")
        result_msg = format_tool_result(call, result, provider_name="deepseek")
        out = _message_to_openai(result_msg)
        assert out["role"] == "tool"
        assert out["tool_call_id"] == "call_42"  # NOT "" (the soak metadata-key bug)

    def test_anthropic_assistant_message_serialises_tool_use_blocks(self) -> None:
        msg = ConversationMessage(
            role="assistant",
            content="searching…",
            created_at=self._now(),
            tool_calls=[ToolCall(name="web_search", args={"query": "mould"}, call_id="tu_1")],
        )
        out = _message_to_anthropic(msg)
        assert out["role"] == "assistant"
        blocks = out["content"]
        assert {"type": "text", "text": "searching…"} in blocks
        tool_use = next(b for b in blocks if b["type"] == "tool_use")
        assert tool_use["id"] == "tu_1"
        assert tool_use["name"] == "web_search"
        assert tool_use["input"] == {"query": "mould"}

    def test_anthropic_consecutive_tool_results_are_coalesced(self) -> None:
        # Production-launch finding: Anthropic 400s with `tool_use ids were
        # found without tool_result blocks immediately after` when an assistant
        # message had 2+ tool_use blocks but the matching tool_results landed in
        # 2+ consecutive user messages. The conversation loop appends one
        # role=tool ConversationMessage per dispatched call, so the merge has to
        # happen at the Anthropic-serialiser boundary.
        from persona.backends.openai_compat import _coalesce_anthropic_tool_results

        assistant_tu = {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "searching…"},
                {"type": "tool_use", "id": "tu_1", "name": "web_search", "input": {"q": "a"}},
                {"type": "tool_use", "id": "tu_2", "name": "web_search", "input": {"q": "b"}},
            ],
        }
        tr1 = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_1", "content": "r1"}],
        }
        tr2 = {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "tu_2", "content": "r2"}],
        }
        text_after = {"role": "user", "content": "thanks"}

        merged = _coalesce_anthropic_tool_results([assistant_tu, tr1, tr2, text_after])

        # The two tool-result user messages collapse into one.
        assert len(merged) == 3
        assert merged[0] is assistant_tu  # assistant untouched
        assert merged[1]["role"] == "user"
        assert isinstance(merged[1]["content"], list)
        assert len(merged[1]["content"]) == 2
        assert {b["tool_use_id"] for b in merged[1]["content"]} == {"tu_1", "tu_2"}
        # A subsequent string-content user message must NOT be folded in.
        assert merged[2] is text_after

    def test_anthropic_tool_result_carries_the_id(self) -> None:
        # Spec 11 launch fix: format_tool_result(anthropic) now produces
        # role="tool" + raw content, and _message_to_anthropic lifts it into
        # a proper structured tool_result block on a user message — matching
        # Anthropic's native tool protocol so multi-call rounds round-trip
        # cleanly without 400s. Previously the closeout flagged this as a
        # Known Limitation; production tools surfaced it on the first live
        # tool call against Anthropic Sonnet as the frontier tier.
        call = ToolCall(name="web_search", args={}, call_id="tu_9")
        result = ToolResult(tool_name="web_search", content="r", call_id="tu_9")
        out = _message_to_anthropic(format_tool_result(call, result, provider_name="anthropic"))
        assert out["role"] == "user"
        assert isinstance(out["content"], list)
        assert out["content"][0]["type"] == "tool_result"
        assert out["content"][0]["tool_use_id"] == "tu_9"
        assert out["content"][0]["content"] == "r"

    def test_unsupported_unknown_provider(self) -> None:
        assert _native_tools_supported("nonsense", "x") is False

    @pytest.mark.asyncio
    async def test_anthropic_streaming_threads_tool_use_id_and_name_through(self) -> None:
        # Production-launch finding: Anthropic streaming sends `content_block_start`
        # carrying the tool_use id + name ONCE, then `content_block_delta`s with
        # only the block `index` and partial input JSON. The reconstruction must
        # thread the real id forward into every ToolCallDelta AND emit the name
        # on the first sighting — otherwise round-2's `_message_to_anthropic`
        # serialises an empty `tool_use.name` and Anthropic 400s with
        # "tool_use.name: String should have at least 1 character".
        from persona.backends.openai_compat import OpenAICompatibleBackend
        from persona.backends.types import ToolCallDelta

        backend = OpenAICompatibleBackend(_config("anthropic"))

        # Simulate the Anthropic events for a single tool call: start (with id +
        # name), two arg deltas, message_delta for usage.
        class _Block:
            def __init__(self) -> None:
                self.type = "tool_use"
                self.id = "toolu_real_xyz"
                self.name = "web_search"

        class _Delta:
            def __init__(self, partial: str) -> None:
                self.type = "input_json_delta"
                self.partial_json = partial

        class _StartEv:
            type = "content_block_start"
            index = 0
            content_block = _Block()

        class _ArgEv:
            def __init__(self, partial: str) -> None:
                self.type = "content_block_delta"
                self.index = 0
                self.delta = _Delta(partial)

        class _MockUsage:
            input_tokens = 10
            output_tokens = 5

        class _FinalMsg:
            usage = _MockUsage()

        class _MockStream:
            def __init__(self, events: list[Any]) -> None:
                self._events = events

            async def __aenter__(self) -> _MockStream:  # type: ignore[name-defined]
                return self

            async def __aexit__(self, *_exc: Any) -> None:
                return None

            def __aiter__(self) -> _MockStream:  # type: ignore[name-defined]
                return self

            async def __anext__(self) -> Any:
                if not self._events:
                    raise StopAsyncIteration
                return self._events.pop(0)

            async def get_final_message(self) -> _FinalMsg:
                return _FinalMsg()

        events: list[Any] = [_StartEv(), _ArgEv('{"qu'), _ArgEv('ery":"mould"}')]

        class _Messages:
            def stream(self, **_k: Any) -> _MockStream:
                return _MockStream(events)

        class _Client:
            messages = _Messages()

        backend._anthropic = _Client()  # type: ignore[assignment]

        deltas: list[ToolCallDelta] = []
        msgs = [ConversationMessage(role="user", content="hi", created_at=datetime.now(UTC))]
        async for chunk in backend.chat_stream(msgs, tools=None):
            if chunk.tool_call_delta is not None:
                deltas.append(chunk.tool_call_delta)

        assert deltas, "no tool_call_delta emitted"
        # The first delta MUST carry the real id + name (so the runtime loop's
        # accumulator records `web_search`, not "").
        assert deltas[0].call_id == "toolu_real_xyz"
        assert deltas[0].name_delta == "web_search"
        # All subsequent deltas thread the SAME real id (not "0" / index).
        assert all(d.call_id == "toolu_real_xyz" for d in deltas)
        # Concatenated args reconstruct the full JSON the model emitted.
        assert "".join(d.arguments_delta for d in deltas) == '{"query":"mould"}'


# -----------------------------------------------------------------------------
# Construction
# -----------------------------------------------------------------------------


class TestConstruction:
    def test_anthropic_constructs(self) -> None:
        backend = OpenAICompatibleBackend(_config("anthropic"))
        assert isinstance(backend, ChatBackend)
        assert backend.provider_name == "anthropic"
        assert backend.model_name == "test-model"
        # supports_native_tools = True for anthropic (all models).
        assert backend.supports_native_tools is True

    def test_openai_constructs(self) -> None:
        backend = OpenAICompatibleBackend(_config("openai"))
        assert isinstance(backend, ChatBackend)
        assert backend.provider_name == "openai"
        assert backend.supports_native_tools is True

    def test_groq_constructs_with_unlisted_model_uses_shim(self) -> None:
        backend = OpenAICompatibleBackend(_config("groq", model="whisper-large-v3"))
        assert backend.supports_native_tools is False

    def test_missing_api_key_raises(self) -> None:
        config = BackendConfig(provider="openai", model="gpt-4o", api_key=None)
        with pytest.raises(AuthenticationError) as info:
            OpenAICompatibleBackend(config)
        assert "openai" in str(info.value)

    def test_empty_api_key_raises(self) -> None:
        config = BackendConfig(provider="openai", model="gpt-4o", api_key=SecretStr(""))
        with pytest.raises(AuthenticationError):
            OpenAICompatibleBackend(config)

    def test_unknown_provider_raises(self) -> None:
        config = BackendConfig(
            provider="ollama",  # not handled by this backend class
            model="llama3",
            api_key=SecretStr("x"),
        )
        with pytest.raises(ProviderError):
            OpenAICompatibleBackend(config)

    def test_custom_base_url_passed_to_client(self) -> None:
        config = BackendConfig(
            provider="openai",
            model="gpt-4o",
            api_key=SecretStr("x"),
            base_url="https://my-proxy.example/v1/",
        )
        backend = OpenAICompatibleBackend(config)
        # _openai client base_url string is configured.
        assert backend._openai is not None  # type: ignore[attr-defined]

    def test_nvidia_constructs_with_launch_model(self) -> None:
        # Spec 20 T09 — D-20-X-nvidia-allow-set-extend invariant. The four
        # anchors (Provider Literal, DEFAULT_BASE_URLS, both capability
        # matrices, allow-set) must land atomically. Missing any one raises
        # ProviderError here — this test gates that they all landed.
        backend = OpenAICompatibleBackend(
            _config("nvidia", model="nvidia/llama-3.3-nemotron-super-49b-v1.5")
        )
        assert isinstance(backend, ChatBackend)
        assert backend.provider_name == "nvidia"
        assert backend.supports_native_tools is True
        # NVIDIA dispatches through the openai SDK branch (non-default base_url),
        # same as deepseek / groq / together.
        assert backend._openai is not None  # type: ignore[attr-defined]
        assert backend._anthropic is None  # type: ignore[attr-defined]

    def test_nvidia_constructs_with_unlisted_model_uses_shim(self) -> None:
        # Allowlisted provider — unknown model still constructs cleanly but
        # routes via the prompt-based shim (mirrors groq's behaviour).
        backend = OpenAICompatibleBackend(_config("nvidia", model="nvidia/some-unknown-model"))
        assert backend.supports_native_tools is False
        assert backend.provider_name == "nvidia"

    def test_nvidia_default_base_url_resolves(self) -> None:
        # NVIDIA's hosted catalog URL — verify the openai client received the
        # /v1/-terminated base URL from DEFAULT_BASE_URLS.
        backend = OpenAICompatibleBackend(
            _config("nvidia", model="nvidia/llama-3.3-nemotron-super-49b-v1.5")
        )
        assert backend._openai is not None  # type: ignore[attr-defined]
        assert "integrate.api.nvidia.com" in str(
            backend._openai.base_url  # type: ignore[union-attr]
        )

    def test_anthropic_base_url_has_no_doubled_v1(self) -> None:
        # Regression for D-10-9: the `anthropic` SDK appends its own
        # /v1/messages, so the default base_url must NOT carry a /v1/ suffix
        # (else requests hit /v1/v1/messages -> 404). The OpenAI-compat
        # providers DO keep /v1/ (their SDK does not append it).
        anth = OpenAICompatibleBackend(_config("anthropic"))
        assert "/v1" not in str(anth._anthropic.base_url)  # type: ignore[union-attr]
        oai = OpenAICompatibleBackend(_config("openai"))
        assert str(oai._openai.base_url).rstrip("/").endswith("/v1")  # type: ignore[union-attr]


# -----------------------------------------------------------------------------
# Anthropic chat (non-streaming)
# -----------------------------------------------------------------------------


def _mock_anthropic_message_response(
    *,
    text: str = "hello",
    model: str = "claude-sonnet-4-6",
    tool_use: tuple[str, str, dict[str, Any]] | None = None,
    input_tokens: int = 10,
    output_tokens: int = 5,
) -> Any:
    """Build a MagicMock that mimics ``anthropic.types.Message``."""
    blocks: list[Any] = []
    text_block = MagicMock()
    text_block.type = "text"
    text_block.text = text
    blocks.append(text_block)
    if tool_use is not None:
        tu_id, tu_name, tu_input = tool_use
        tu_block = MagicMock()
        tu_block.type = "tool_use"
        tu_block.id = tu_id
        tu_block.name = tu_name
        tu_block.input = tu_input
        blocks.append(tu_block)
    response = MagicMock()
    response.content = blocks
    response.model = model
    usage = MagicMock()
    usage.input_tokens = input_tokens
    usage.output_tokens = output_tokens
    response.usage = usage
    return response


class TestAnthropicChat:
    @pytest.mark.asyncio
    async def test_chat_returns_response(self) -> None:
        backend = OpenAICompatibleBackend(_config("anthropic"))
        with patch.object(
            backend._anthropic.messages,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=_mock_anthropic_message_response()),
        ):
            response = await backend.chat([_user("hi")])
        assert isinstance(response, ChatResponse)
        assert response.content == "hello"
        assert response.provider == "anthropic"
        assert response.usage.prompt_tokens == 10
        assert response.usage.completion_tokens == 5
        assert response.latency_ms >= 0.0

    @pytest.mark.asyncio
    async def test_chat_with_native_tool_call(self) -> None:
        backend = OpenAICompatibleBackend(_config("anthropic"))
        mock_response = _mock_anthropic_message_response(
            text="",
            tool_use=("call-123", "web_search", {"query": "kittens"}),
        )
        with patch.object(
            backend._anthropic.messages,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=mock_response),
        ):
            response = await backend.chat(
                [_user("search please")],
                tools=[ToolSpec(name="web_search", description="search", parameters={})],
            )
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "web_search"
        assert response.tool_calls[0].args == {"query": "kittens"}
        assert response.tool_calls[0].call_id == "call-123"

    @pytest.mark.asyncio
    async def test_chat_system_message_split(self) -> None:
        backend = OpenAICompatibleBackend(_config("anthropic"))
        create_mock = AsyncMock(return_value=_mock_anthropic_message_response())
        with patch.object(
            backend._anthropic.messages,
            "create",
            new=create_mock,  # type: ignore[union-attr]
        ):
            await backend.chat(
                [
                    ConversationMessage(
                        role="system",
                        content="You are helpful.",
                        created_at=datetime.now(UTC),
                    ),
                    _user("hi"),
                ]
            )
        call_kwargs = create_mock.call_args.kwargs
        assert call_kwargs["system"] == "You are helpful."
        assert all(m["role"] != "system" for m in call_kwargs["messages"])


# -----------------------------------------------------------------------------
# OpenAI chat (non-streaming)
# -----------------------------------------------------------------------------


def _mock_openai_chat_completion(
    *,
    content: str = "hello",
    model: str = "gpt-4o",
    tool_call: tuple[str, str, str] | None = None,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
) -> Any:
    choice = MagicMock()
    message = MagicMock()
    message.content = content
    if tool_call is not None:
        tc_id, tc_name, tc_args = tool_call
        tc = MagicMock()
        tc.id = tc_id
        fn = MagicMock()
        fn.name = tc_name
        fn.arguments = tc_args
        tc.function = fn
        message.tool_calls = [tc]
    else:
        message.tool_calls = []
    choice.message = message
    response = MagicMock()
    response.choices = [choice]
    response.model = model
    usage = MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    response.usage = usage
    return response


class TestOpenAIChat:
    @pytest.mark.asyncio
    async def test_chat_returns_response(self) -> None:
        backend = OpenAICompatibleBackend(_config("openai"))
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=_mock_openai_chat_completion()),
        ):
            response = await backend.chat([_user("hi")])
        assert response.content == "hello"
        assert response.provider == "openai"
        assert response.usage.total_tokens == 15

    @pytest.mark.asyncio
    @pytest.mark.parametrize("provider", ["openai", "deepseek", "groq", "together"])
    async def test_unified_shape_across_providers(self, provider: str) -> None:
        # Groq/Together: pick a known-supported model or accept shim path.
        model = "llama-3.3-70b-versatile" if provider == "groq" else "test-model"
        backend = OpenAICompatibleBackend(_config(provider, model=model))
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=_mock_openai_chat_completion()),
        ):
            response = await backend.chat([_user("hi")])
        assert response.provider == provider
        assert isinstance(response.content, str)
        assert response.usage.total_tokens == 15

    @pytest.mark.asyncio
    async def test_chat_with_native_tool_call(self) -> None:
        backend = OpenAICompatibleBackend(_config("openai"))
        mock_response = _mock_openai_chat_completion(
            content="",
            tool_call=("call-456", "web_search", '{"query": "kittens"}'),
        )
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=mock_response),
        ):
            response = await backend.chat(
                [_user("search")],
                tools=[ToolSpec(name="web_search", description="search", parameters={})],
            )
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].args == {"query": "kittens"}

    @pytest.mark.asyncio
    async def test_chat_with_malformed_tool_args_is_empty_dict(self) -> None:
        backend = OpenAICompatibleBackend(_config("openai"))
        mock_response = _mock_openai_chat_completion(
            content="",
            tool_call=("call-x", "web_search", "not-json"),
        )
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=mock_response),
        ):
            response = await backend.chat([_user("x")])
        assert response.tool_calls[0].args == {}


# -----------------------------------------------------------------------------
# Streaming — OpenAI
# -----------------------------------------------------------------------------


async def _async_iter(items: list[Any]) -> AsyncIterator[Any]:
    for x in items:
        yield x


def _openai_stream_chunk(
    *, content: str = "", usage: Any | None = None, tool_calls: list[Any] | None = None
) -> Any:
    chunk = MagicMock()
    delta = MagicMock()
    delta.content = content
    delta.tool_calls = tool_calls or []
    choice = MagicMock()
    choice.delta = delta
    chunk.choices = [choice]
    chunk.usage = usage
    return chunk


class TestOpenAIStream:
    @pytest.mark.asyncio
    async def test_stream_yields_chunks_and_final_usage(self) -> None:
        backend = OpenAICompatibleBackend(_config("openai"))
        usage = MagicMock()
        usage.prompt_tokens = 4
        usage.completion_tokens = 6
        chunks_in = [
            _openai_stream_chunk(content="Hel"),
            _openai_stream_chunk(content="lo"),
            _openai_stream_chunk(usage=usage),
        ]
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=_async_iter(chunks_in)),
        ):
            collected: list[StreamChunk] = []
            async for c in backend.chat_stream([_user("hi")]):
                collected.append(c)
        # ≥2 chunks (≥1 delta + final).
        text_chunks = [c for c in collected if not c.is_final and c.delta]
        finals = [c for c in collected if c.is_final]
        assert len(text_chunks) >= 1
        assert len(finals) == 1
        assert finals[0].usage is not None
        assert finals[0].usage.total_tokens == 10


# -----------------------------------------------------------------------------
# Streaming — Anthropic
# -----------------------------------------------------------------------------


class _FakeAnthropicStream:
    """Mimics ``anthropic.AsyncMessageStream`` minimally."""

    def __init__(self, events: list[Any], final_message: Any) -> None:
        self._events = events
        self._final_message = final_message

    async def __aenter__(self) -> _FakeAnthropicStream:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def __aiter__(self) -> AsyncIterator[Any]:
        async def gen() -> AsyncIterator[Any]:
            for ev in self._events:
                yield ev

        return gen()

    async def get_final_message(self) -> Any:
        return self._final_message


def _anthropic_text_delta_event(text: str) -> Any:
    ev = MagicMock()
    ev.type = "content_block_delta"
    delta = MagicMock()
    delta.type = "text_delta"
    delta.text = text
    ev.delta = delta
    return ev


class TestAnthropicStream:
    @pytest.mark.asyncio
    async def test_stream_yields_text_and_final(self) -> None:
        backend = OpenAICompatibleBackend(_config("anthropic"))
        events = [
            _anthropic_text_delta_event("Hel"),
            _anthropic_text_delta_event("lo"),
        ]
        final_msg = _mock_anthropic_message_response(text="Hello", input_tokens=4, output_tokens=2)
        fake_stream = _FakeAnthropicStream(events, final_msg)
        with patch.object(
            backend._anthropic.messages,  # type: ignore[union-attr]
            "stream",
            new=MagicMock(return_value=fake_stream),
        ):
            collected: list[StreamChunk] = []
            async for c in backend.chat_stream([_user("hi")]):
                collected.append(c)
        text_chunks = [c for c in collected if not c.is_final and c.delta]
        finals = [c for c in collected if c.is_final]
        assert len(text_chunks) == 2
        assert finals[0].usage is not None
        assert finals[0].usage.total_tokens == 6


# -----------------------------------------------------------------------------
# Error mapping
# -----------------------------------------------------------------------------


def _fake_response(*, status: int = 200, headers: dict[str, str] | None = None) -> Any:
    resp = MagicMock()
    resp.status_code = status
    resp.headers = headers or {}
    resp.request = MagicMock()
    return resp


class TestErrorMapping:
    @pytest.mark.asyncio
    async def test_anthropic_401_to_authentication_error(self) -> None:
        backend = OpenAICompatibleBackend(_config("anthropic"))
        exc = anthropic.AuthenticationError(
            "bad key", response=_fake_response(status=401), body=None
        )
        with (
            patch.object(
                backend._anthropic.messages,  # type: ignore[union-attr]
                "create",
                new=AsyncMock(side_effect=exc),
            ),
            pytest.raises(AuthenticationError) as info,
        ):
            await backend.chat([_user("x")])
        assert "anthropic" in str(info.value)

    @pytest.mark.asyncio
    async def test_anthropic_429_to_rate_limit(self) -> None:
        backend = OpenAICompatibleBackend(_config("anthropic"))
        exc = anthropic.RateLimitError(
            "slow down",
            response=_fake_response(status=429, headers={"retry-after": "30"}),
            body=None,
        )
        with (
            patch.object(
                backend._anthropic.messages,  # type: ignore[union-attr]
                "create",
                new=AsyncMock(side_effect=exc),
            ),
            pytest.raises(RateLimitError) as info,
        ):
            await backend.chat([_user("x")])
        assert "retry_after_s=30" in str(info.value)

    @pytest.mark.asyncio
    async def test_anthropic_404_to_model_not_found(self) -> None:
        backend = OpenAICompatibleBackend(_config("anthropic", model="imagined"))
        exc = anthropic.NotFoundError(
            "no such model", response=_fake_response(status=404), body=None
        )
        with (
            patch.object(
                backend._anthropic.messages,  # type: ignore[union-attr]
                "create",
                new=AsyncMock(side_effect=exc),
            ),
            pytest.raises(ModelNotFoundError) as info,
        ):
            await backend.chat([_user("x")])
        assert "model=imagined" in str(info.value)

    @pytest.mark.asyncio
    async def test_openai_timeout_to_backend_timeout_error(self) -> None:
        backend = OpenAICompatibleBackend(_config("openai"))
        # openai.APITimeoutError requires a request argument.
        request = MagicMock()
        exc = openai.APITimeoutError(request=request)
        with (
            patch.object(
                backend._openai.chat.completions,  # type: ignore[union-attr]
                "create",
                new=AsyncMock(side_effect=exc),
            ),
            pytest.raises(BackendTimeoutError),
        ):
            await backend.chat([_user("x")])

    @pytest.mark.asyncio
    async def test_unmapped_error_becomes_provider_error(self) -> None:
        backend = OpenAICompatibleBackend(_config("openai"))
        with (
            patch.object(
                backend._openai.chat.completions,  # type: ignore[union-attr]
                "create",
                new=AsyncMock(side_effect=RuntimeError("weird")),
            ),
            pytest.raises(ProviderError) as info,
        ):
            await backend.chat([_user("x")])
        assert "RuntimeError" in str(info.value)


# -----------------------------------------------------------------------------
# Shim fallback
# -----------------------------------------------------------------------------


class TestShimFallback:
    @pytest.mark.asyncio
    async def test_groq_unlisted_model_uses_shim(self) -> None:
        # whisper isn't in the allow-list → shim path.
        backend = OpenAICompatibleBackend(_config("groq", model="whisper-large-v3"))
        # Model emits a JSON tool-call block in text content.
        mock_response = _mock_openai_chat_completion(
            content='I will search. {"tool": "web_search", "args": {"q": "k"}}',
        )
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=mock_response),
        ):
            response = await backend.chat(
                [_user("search")],
                tools=[ToolSpec(name="web_search", description="x", parameters={})],
            )
        assert len(response.tool_calls) == 1
        assert response.tool_calls[0].name == "web_search"
        # Cleaned content has tool-call JSON removed but explanatory text kept.
        assert "I will search." in response.content

    @pytest.mark.asyncio
    async def test_shim_failure_returns_text_passthrough(self) -> None:
        backend = OpenAICompatibleBackend(_config("groq", model="whisper-large-v3"))
        mock_response = _mock_openai_chat_completion(content="just plain text")
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=mock_response),
        ):
            response = await backend.chat(
                [_user("x")],
                tools=[ToolSpec(name="t", description="x", parameters={})],
            )
        assert response.tool_calls == []
        assert response.content == "just plain text"


# -----------------------------------------------------------------------------
# Real-API smoke test (D-10-9) — manual, paid, non-deterministic.
# Proves the Anthropic base_url fix end-to-end: a real chat returns content.
# Skipped unless PERSONA_FRONTIER_API_KEY is set.
# -----------------------------------------------------------------------------


class TestAnthropicRealCall:
    @pytest.mark.external
    @pytest.mark.asyncio
    async def test_anthropic_real_chat_returns_content(self) -> None:
        key = os.environ.get("PERSONA_FRONTIER_API_KEY")
        if not key:
            pytest.skip("PERSONA_FRONTIER_API_KEY not set")
        model = os.environ.get("PERSONA_FRONTIER_MODEL", "claude-sonnet-4-6")
        backend = OpenAICompatibleBackend(
            BackendConfig(provider="anthropic", model=model, api_key=SecretStr(key))
        )
        response = await backend.chat([_user("Reply with the single word: ok")], max_tokens=16)
        # A non-empty reply means the request reached /v1/messages (not /v1/v1/...).
        assert response.content.strip()
        assert response.provider == "anthropic"


# -----------------------------------------------------------------------------
# Spec 20 T12 — Reasoning surface (D-20-2, D-20-3, D-20-X-*)
# -----------------------------------------------------------------------------


def _openai_stream_chunk_with_reasoning(
    *,
    content: str = "",
    reasoning_content: str | None = None,
    reasoning: str | None = None,
    usage: Any | None = None,
) -> Any:
    """Build a fake openai-py ChoiceDelta carrying reasoning via either field name.

    D-20-X-nemotron-field-name-dual-probe: NVIDIA Nemotron canonical
    ``reasoning_content`` + Nano-Omni VLM alias ``reasoning`` arrive as
    Pydantic extras (not statically typed) on the chunk delta.
    """
    chunk = MagicMock()
    delta = MagicMock(spec=["content", "tool_calls", "reasoning_content", "reasoning"])
    delta.content = content
    delta.tool_calls = []
    delta.reasoning_content = reasoning_content
    delta.reasoning = reasoning
    choice = MagicMock()
    choice.delta = delta
    chunk.choices = [choice]
    chunk.usage = usage
    return chunk


class TestNemotronDualProbeStream:
    @pytest.mark.asyncio
    async def test_reasoning_content_field_captured(self) -> None:
        backend = OpenAICompatibleBackend(_config("nvidia"))
        usage = MagicMock(prompt_tokens=1, completion_tokens=2)
        chunks_in = [
            _openai_stream_chunk_with_reasoning(
                content="hello", reasoning_content="step-1 ", reasoning=None
            ),
            _openai_stream_chunk_with_reasoning(usage=usage),
        ]
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=_async_iter(chunks_in)),
        ):
            collected: list[StreamChunk] = []
            async for c in backend.chat_stream([_user("hi")]):
                collected.append(c)
        reasoning_chunks = [c for c in collected if c.reasoning is not None]
        assert reasoning_chunks, "no reasoning surfaced"
        assert reasoning_chunks[0].reasoning == "step-1 "

    @pytest.mark.asyncio
    async def test_reasoning_alias_field_captured(self) -> None:
        backend = OpenAICompatibleBackend(_config("nvidia"))
        usage = MagicMock(prompt_tokens=1, completion_tokens=2)
        chunks_in = [
            _openai_stream_chunk_with_reasoning(
                content="hi", reasoning_content=None, reasoning="nano-omni-thought"
            ),
            _openai_stream_chunk_with_reasoning(usage=usage),
        ]
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=_async_iter(chunks_in)),
        ):
            collected: list[StreamChunk] = []
            async for c in backend.chat_stream([_user("hi")]):
                collected.append(c)
        reasoning_chunks = [c for c in collected if c.reasoning is not None]
        assert reasoning_chunks
        assert reasoning_chunks[0].reasoning == "nano-omni-thought"

    @pytest.mark.asyncio
    async def test_reasoning_only_chunk_no_text(self) -> None:
        backend = OpenAICompatibleBackend(_config("nvidia"))
        usage = MagicMock(prompt_tokens=1, completion_tokens=2)
        chunks_in = [
            _openai_stream_chunk_with_reasoning(
                content="", reasoning_content="silent-thought", reasoning=None
            ),
            _openai_stream_chunk_with_reasoning(content="answer", usage=usage),
        ]
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=AsyncMock(return_value=_async_iter(chunks_in)),
        ):
            collected: list[StreamChunk] = []
            async for c in backend.chat_stream([_user("hi")]):
                collected.append(c)
        reasoning_chunks = [c for c in collected if c.reasoning is not None]
        assert any(c.reasoning == "silent-thought" for c in reasoning_chunks)


class TestExtraBodyPassThrough:
    @pytest.mark.asyncio
    async def test_chat_passes_extra_body_when_set(self) -> None:
        backend = OpenAICompatibleBackend(
            BackendConfig(
                provider="nvidia",
                model="nvidia/llama-3.3-nemotron-super-49b-v1.5",
                api_key=SecretStr("k"),
                extra_body={"chat_template_kwargs": {"thinking": True}},
            )
        )
        create_mock = AsyncMock(return_value=_mock_openai_chat_completion())
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=create_mock,
        ):
            await backend.chat([_user("hi")])
        kwargs = create_mock.call_args.kwargs
        assert kwargs.get("extra_body") == {"chat_template_kwargs": {"thinking": True}}

    @pytest.mark.asyncio
    async def test_chat_omits_extra_body_when_none(self) -> None:
        backend = OpenAICompatibleBackend(_config("openai"))
        create_mock = AsyncMock(return_value=_mock_openai_chat_completion())
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=create_mock,
        ):
            await backend.chat([_user("hi")])
        kwargs = create_mock.call_args.kwargs
        assert "extra_body" not in kwargs


class TestDeepseekReasoningStripInvariant:
    def test_strips_reasoning_content_from_assistant_for_deepseek(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok", "reasoning_content": "secret-thought"},
        ]
        out = _strip_reasoning_for_provider(messages, "deepseek")
        assert out[1] == {"role": "assistant", "content": "ok"}
        assert "reasoning_content" not in out[1]

    def test_preserves_other_assistant_fields_for_deepseek(self) -> None:
        messages: list[dict[str, Any]] = [
            {
                "role": "assistant",
                "content": "ok",
                "reasoning_content": "x",
                "tool_calls": [{"id": "c1"}],
            },
        ]
        out = _strip_reasoning_for_provider(messages, "deepseek")
        assert out[0]["tool_calls"] == [{"id": "c1"}]
        assert "reasoning_content" not in out[0]

    def test_passthrough_for_non_deepseek_providers(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "assistant", "content": "ok", "reasoning_content": "preserved"},
        ]
        for provider in ("openai", "nvidia", "groq", "anthropic", "together"):
            out = _strip_reasoning_for_provider(messages, provider)
            assert out[0].get("reasoning_content") == "preserved", provider

    def test_no_op_when_field_absent(self) -> None:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok"},
        ]
        out = _strip_reasoning_for_provider(messages, "deepseek")
        assert out == messages


class TestSamplingParams:
    """top_p / top_k threading (drafter creativity).

    Anthropic supports BOTH; OpenAI supports top_p only (top_k is a documented
    NO-OP on that path). ``None`` leaves the provider default untouched.
    """

    @pytest.mark.asyncio
    async def test_anthropic_forwards_top_p_and_top_k(self) -> None:
        backend = OpenAICompatibleBackend(_config("anthropic"))
        create_mock = AsyncMock(return_value=_mock_anthropic_message_response())
        with patch.object(
            backend._anthropic.messages,  # type: ignore[union-attr]
            "create",
            new=create_mock,
        ):
            await backend.chat([_user("hi")], temperature=0.9, top_p=0.95, top_k=60)
        kwargs = create_mock.call_args.kwargs
        assert kwargs["top_p"] == 0.95
        assert kwargs["top_k"] == 60
        assert kwargs["temperature"] == 0.9

    @pytest.mark.asyncio
    async def test_anthropic_omits_sampling_when_none(self) -> None:
        backend = OpenAICompatibleBackend(_config("anthropic"))
        create_mock = AsyncMock(return_value=_mock_anthropic_message_response())
        with patch.object(
            backend._anthropic.messages,  # type: ignore[union-attr]
            "create",
            new=create_mock,
        ):
            await backend.chat([_user("hi")])
        kwargs = create_mock.call_args.kwargs
        assert "top_p" not in kwargs
        assert "top_k" not in kwargs

    @pytest.mark.asyncio
    async def test_openai_forwards_top_p_but_drops_top_k(self) -> None:
        backend = OpenAICompatibleBackend(_config("openai"))
        create_mock = AsyncMock(return_value=_mock_openai_chat_completion())
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=create_mock,
        ):
            # top_k is supplied but MUST NOT reach the OpenAI SDK (no such param).
            await backend.chat([_user("hi")], temperature=0.9, top_p=0.95, top_k=60)
        kwargs = create_mock.call_args.kwargs
        assert kwargs["top_p"] == 0.95
        assert "top_k" not in kwargs

    @pytest.mark.asyncio
    async def test_openai_omits_top_p_when_none(self) -> None:
        backend = OpenAICompatibleBackend(_config("openai"))
        create_mock = AsyncMock(return_value=_mock_openai_chat_completion())
        with patch.object(
            backend._openai.chat.completions,  # type: ignore[union-attr]
            "create",
            new=create_mock,
        ):
            await backend.chat([_user("hi")])
        kwargs = create_mock.call_args.kwargs
        assert "top_p" not in kwargs
        assert "top_k" not in kwargs


class TestTextualToolCallRecovery:
    """Native-tools models that serialise a tool call into ``content`` text.

    Some OpenRouter-fronted models, when given native ``tools``, emit the tool
    call as a JSON object in the *content* (the OpenAI ``{"type":"function",
    "name":...,"parameters":...}`` shape) instead of populating structured
    ``tool_calls``. Without recovery the agentic loop treats that text as a
    reasoning step and surfaces the raw JSON to the user. The parser must
    recover the textual call into ``response.tool_calls`` and clear the content.
    """

    @staticmethod
    def _resp(content: str) -> Any:
        msg = SimpleNamespace(content=content, tool_calls=None)
        choice = SimpleNamespace(message=msg)
        usage = SimpleNamespace(prompt_tokens=3, completion_tokens=5)
        return SimpleNamespace(choices=[choice], usage=usage, model="m")

    def test_function_type_shape_recovered(self) -> None:
        text = '{"type":"function","name":"code_execution","parameters":{"code":"print(1)"}}'
        resp = _parse_openai_response(self._resp(text), "openrouter", use_native_tools=True)
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "code_execution"
        assert resp.tool_calls[0].args == {"code": "print(1)"}
        assert resp.content.strip() == ""

    def test_name_arguments_shape_recovered(self) -> None:
        text = '{"name":"web_search","arguments":{"query":"x"}}'
        resp = _parse_openai_response(self._resp(text), "openrouter", use_native_tools=True)
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "web_search"
        assert resp.tool_calls[0].args == {"query": "x"}

    def test_fenced_json_recovered(self) -> None:
        text = '```json\n{"type":"function","name":"calculator","parameters":{"expr":"1+1"}}\n```'
        resp = _parse_openai_response(self._resp(text), "openrouter", use_native_tools=True)
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "calculator"

    def test_plain_prose_is_not_a_tool_call(self) -> None:
        text = 'Here is a JSON example: {"type": "object"} — note the schema.'
        resp = _parse_openai_response(self._resp(text), "openrouter", use_native_tools=True)
        assert resp.tool_calls == []
        assert resp.content == text

    def test_structured_tool_calls_take_precedence(self) -> None:
        # When the SDK already populated structured tool_calls, content is left as-is.
        fn = SimpleNamespace(name="web_search", arguments='{"q":"x"}')
        tc = SimpleNamespace(function=fn, id="call_0")
        msg = SimpleNamespace(content="some narration", tool_calls=[tc])
        resp_obj = SimpleNamespace(
            choices=[SimpleNamespace(message=msg)],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
            model="m",
        )
        resp = _parse_openai_response(resp_obj, "openrouter", use_native_tools=True)
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].name == "web_search"
        assert resp.content == "some narration"


class TestTruncatedToolCallArguments:
    """Non-streaming parse: a tool call whose ``arguments`` JSON was truncated.

    When the model writes a payload (e.g. a large ``code`` string) that exceeds
    the response budget, the provider cuts it off mid-JSON. The parser MUST mark
    the call ``truncated`` (and NOT silently dispatch empty args) so the runtime
    can feed back a "your call was cut off — shorten it" signal instead of the
    cryptic "Field required" that triggers an identical-retry loop.
    """

    @staticmethod
    def _resp(arguments: str, *, finish_reason: str | None = None) -> Any:
        fn = SimpleNamespace(name="code_execution", arguments=arguments)
        tc = SimpleNamespace(function=fn, id="call_0")
        msg = SimpleNamespace(content="", tool_calls=[tc])
        choice = SimpleNamespace(message=msg, finish_reason=finish_reason)
        usage = SimpleNamespace(prompt_tokens=3, completion_tokens=5)
        return SimpleNamespace(choices=[choice], usage=usage, model="m")

    def test_truncated_arguments_marks_call_truncated(self) -> None:
        # arguments cut off mid-JSON (a long ``code`` string never closed).
        truncated_json = '{"code": "import pandas as pd\\nfor i in range(1000):\\n    print(i'
        resp = _parse_openai_response(
            self._resp(truncated_json, finish_reason="length"),
            "openrouter",
            use_native_tools=True,
        )
        assert len(resp.tool_calls) == 1
        call = resp.tool_calls[0]
        assert call.name == "code_execution"
        assert call.truncated is True
        # args end up empty, but the truncated flag tells the runtime to surface
        # guidance rather than execute an empty-args call.
        assert call.args == {}

    def test_length_finish_reason_marks_truncated_even_if_args_parse(self) -> None:
        # Defensive: finish_reason="length" alone signals a cut-off response.
        resp = _parse_openai_response(
            self._resp('{"code": "print(1)"}', finish_reason="length"),
            "openrouter",
            use_native_tools=True,
        )
        assert resp.tool_calls[0].truncated is True

    def test_wellformed_arguments_not_truncated(self) -> None:
        # Regression: a normal, complete tool call dispatches with full args.
        resp = _parse_openai_response(
            self._resp('{"code": "print(1)"}', finish_reason="tool_calls"),
            "openrouter",
            use_native_tools=True,
        )
        call = resp.tool_calls[0]
        assert call.truncated is False
        assert call.args == {"code": "print(1)"}


def test_history_tool_calls_are_covered_in_the_tools_array() -> None:
    """R4-C1-13: a strict provider (groq) rejects a request whose history holds a
    tool_call for a tool absent from ``tools`` — including a hallucinated skill
    call the model already got an is_error for. The tools array must synthesise a
    no-op spec covering every historical tool_call name, so the request validates
    and the model is steered off (it holds the "not available" result)."""
    from datetime import UTC, datetime

    from persona.backends.openai_compat import _openai_tools_covering_history
    from persona.schema.conversation import ConversationMessage
    from persona.schema.tools import ToolCall

    real = [ToolSpec(name="use_skill", description="activate a skill", parameters={})]
    messages = [
        ConversationMessage(role="user", content="make a doc", created_at=datetime.now(UTC)),
        ConversationMessage(
            role="assistant",
            content="",
            created_at=datetime.now(UTC),
            # The model hallucinated a DIRECT call to a skill name (not a real tool).
            tool_calls=[ToolCall(call_id="c1", name="document_generation", args={})],
        ),
    ]
    covered = _openai_tools_covering_history(real, messages)
    names = {d["function"]["name"] for d in covered}
    assert "use_skill" in names  # the real tool survives
    assert "document_generation" in names  # the hallucinated call is now covered
    synth = next(d for d in covered if d["function"]["name"] == "document_generation")
    assert synth["function"]["parameters"] == {"type": "object", "properties": {}}


def test_covering_is_a_noop_when_history_matches_tools() -> None:
    """No synthetic specs when every historical tool_call is a real tool — the
    common case must not grow the tools array."""
    from datetime import UTC, datetime

    from persona.backends.openai_compat import _openai_tools_covering_history
    from persona.schema.conversation import ConversationMessage
    from persona.schema.tools import ToolCall

    real = [ToolSpec(name="use_skill", description="x", parameters={})]
    messages = [
        ConversationMessage(
            role="assistant",
            content="",
            created_at=datetime.now(UTC),
            tool_calls=[ToolCall(call_id="c1", name="use_skill", args={})],
        ),
    ]
    covered = _openai_tools_covering_history(real, messages)
    assert [d["function"]["name"] for d in covered] == ["use_skill"]
