"""Tests for the provider-aware tool-result formatter (T05, D-03-6)."""

# ruff: noqa: ANN401, ARG001, ARG002, ERA001
from __future__ import annotations

import pytest
from persona.schema.tools import ToolCall, ToolResult
from persona.tools.formatting import format_tool_result


def _call() -> ToolCall:
    return ToolCall(name="web_search", args={"q": "norway tenancy"}, call_id="tcid-1")


def _result(*, is_error: bool = False, content: str = "results") -> ToolResult:
    return ToolResult(tool_name="web_search", content=content, call_id="tcid-1", is_error=is_error)


# ---------------------------------------------------------------------------
# Section: Anthropic shape
# ---------------------------------------------------------------------------


class TestAnthropicShape:
    """Anthropic tool_result uses the same role=tool shape as OpenAI/DeepSeek
    (spec 11 launch fix). ``_message_to_anthropic`` lifts it into the proper
    structured ``tool_result`` block list on a user message; we used to JSON-
    encode the block ourselves which Anthropic doesn't accept as content."""

    def test_role_is_tool(self) -> None:
        msg = format_tool_result(_call(), _result(), provider_name="anthropic")
        assert msg.role == "tool"

    def test_content_is_raw_result_text(self) -> None:
        msg = format_tool_result(_call(), _result(content="some text"), provider_name="anthropic")
        assert msg.content == "some text"

    def test_error_content_unchanged(self) -> None:
        # No "Error:" prefix on the Anthropic branch — the metadata flag is the
        # signal; the structured block carries the raw content unchanged.
        msg = format_tool_result(
            _call(),
            _result(is_error=True, content="Connection refused"),
            provider_name="anthropic",
        )
        assert msg.content == "Connection refused"
        assert msg.metadata["is_error"] == "True"

    def test_metadata_carries_bookkeeping(self) -> None:
        msg = format_tool_result(_call(), _result(), provider_name="anthropic")
        assert msg.metadata["tool_call_id"] == "tcid-1"
        assert msg.metadata["tool_name"] == "web_search"
        assert msg.metadata["is_error"] == "False"
        assert msg.metadata["provider_format"] == "anthropic"


# ---------------------------------------------------------------------------
# Section: OpenAI-family shape
# ---------------------------------------------------------------------------


# D-20-X-nvidia-allow-set-extend: nvidia added 2026-06-10 after a production
# run-time crash surfaced the formatter's allow-set gap (the atomic invariant
# is a SIX-touch: Provider Literal + DEFAULT_BASE_URLS + _NATIVE_TOOLS_
# CAPABILITY + _VISION_CAPABILITY + _factory.py's _OPENAI_COMPAT_PROVIDERS +
# this provider_name switch). NVIDIA uses the same OpenAI-compat tool-result
# shape as the other openai-SDK providers.
#
# R9-030 (2026-07-13): openrouter hit the identical gap — Spec 22 shipped it
# without touching this switch, so any OpenRouter-served persona (M1
# preferred_model routes there) that dispatched a tool crashed the turn with
# the unknown-provider ValueError below (found by M2-I2's test work, whose
# OR rounds had to avoid tools entirely). cloudflare's case already existed
# in source pre-R9-030 but had no test pin here — closed in the same pass so
# the parametrize list matches the full openai-family case tuple 1:1.
@pytest.mark.parametrize(
    "provider", ["openai", "deepseek", "groq", "together", "nvidia", "openrouter", "cloudflare"]
)
class TestOpenAIFamilyShape:
    """OpenAI-compat providers use role=tool with tool_call_id + content."""

    def test_role_is_tool(self, provider: str) -> None:
        msg = format_tool_result(_call(), _result(), provider_name=provider)
        assert msg.role == "tool"

    def test_content_is_bare_text(self, provider: str) -> None:
        msg = format_tool_result(_call(), _result(content="bare text"), provider_name=provider)
        assert msg.content == "bare text"

    def test_error_prefixed_in_content(self, provider: str) -> None:
        msg = format_tool_result(
            _call(),
            _result(is_error=True, content="API down"),
            provider_name=provider,
        )
        assert msg.content == "Error: API down"

    def test_error_not_double_prefixed(self, provider: str) -> None:
        # Tool body may already include "Error:" — don't duplicate.
        msg = format_tool_result(
            _call(),
            _result(is_error=True, content="Error: already prefixed"),
            provider_name=provider,
        )
        assert msg.content == "Error: already prefixed"

    def test_metadata(self, provider: str) -> None:
        msg = format_tool_result(_call(), _result(), provider_name=provider)
        assert msg.metadata["tool_call_id"] == "tcid-1"
        assert msg.metadata["tool_name"] == "web_search"
        assert msg.metadata["provider_format"] == "openai"


# ---------------------------------------------------------------------------
# Section: Ollama / local (shim) shape
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ["ollama", "local"])
class TestShimShape:
    """Ollama and local HF backends consume the shim's plain-text format."""

    def test_role_is_user(self, provider: str) -> None:
        msg = format_tool_result(_call(), _result(), provider_name=provider)
        assert msg.role == "user"

    def test_content_format(self, provider: str) -> None:
        msg = format_tool_result(_call(), _result(content="42"), provider_name=provider)
        assert msg.content == "web_search returned: 42"

    def test_error_in_content_directly(self, provider: str) -> None:
        msg = format_tool_result(
            _call(),
            _result(is_error=True, content="boom"),
            provider_name=provider,
        )
        # Shim doesn't have a separate is_error field — content carries the error inline.
        assert "boom" in msg.content
        assert msg.metadata["is_error"] == "True"

    def test_metadata(self, provider: str) -> None:
        msg = format_tool_result(_call(), _result(), provider_name=provider)
        assert msg.metadata["provider_format"] == "shim"
        assert msg.metadata["tool_call_id"] == "tcid-1"


# ---------------------------------------------------------------------------
# Section: Unknown provider
# ---------------------------------------------------------------------------


class TestUnknownProvider:
    """Unknown provider_name raises ValueError (D-03-6 — programmer error).

    R9-030 considered degrading an unrecognised future provider to the
    OpenAI-family default with a warn-once instead of raising, but D-03-6 is
    an explicit, still-current decision to fail fast at this boundary
    (providers are a spec-02-controlled vocabulary — an unknown name means
    the caller, or a provider added without touching this file, is wrong).
    These tests pin that posture stays exactly as it was — no fallback.
    """

    def test_raises_value_error(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider_name"):
            format_tool_result(_call(), _result(), provider_name="bogus")

    def test_empty_string_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown provider_name"):
            format_tool_result(_call(), _result(), provider_name="")

    def test_openrouter_no_longer_raises(self) -> None:
        # R9-030 pin: openrouter used to fall into this exact case (the bug)
        # — it must now resolve through the openai-family branch, never here.
        msg = format_tool_result(_call(), _result(), provider_name="openrouter")
        assert msg.role == "tool"
        assert msg.metadata["provider_format"] == "openai"

    def test_message_enumerates_full_vocabulary(self) -> None:
        # R9-030: the error message names every valid provider so the gap is
        # diagnosable from the exception alone (no source dive needed) —
        # this is the improvement, NOT a change to the fail-fast semantic.
        with pytest.raises(ValueError, match="Unknown provider_name") as exc_info:
            format_tool_result(_call(), _result(), provider_name="bogus")
        text = str(exc_info.value)
        for provider in (
            "anthropic",
            "openai",
            "deepseek",
            "groq",
            "together",
            "nvidia",
            "openrouter",
            "cloudflare",
            "ollama",
            "local",
        ):
            assert provider in text, f"{provider!r} missing from the unknown-provider message"


# ---------------------------------------------------------------------------
# Section: ConversationMessage invariants preserved
# ---------------------------------------------------------------------------


class TestConversationMessageInvariants:
    """The returned message must satisfy the existing ConversationMessage contract."""

    def test_created_at_is_tz_aware(self) -> None:
        msg = format_tool_result(_call(), _result(), provider_name="anthropic")
        assert msg.created_at.tzinfo is not None

    def test_message_is_frozen(self) -> None:
        from pydantic import ValidationError

        msg = format_tool_result(_call(), _result(), provider_name="openai")
        with pytest.raises(ValidationError):
            msg.role = "assistant"  # type: ignore[misc]
