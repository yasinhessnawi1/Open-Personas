"""Provider-aware tool-result formatter for spec 03.

Converts a (:class:`ToolCall`, :class:`ToolResult`) pair into a
:class:`ConversationMessage` whose role and content shape match the
provider's expected tool-result message. The runtime (spec 05) calls this
once per tool dispatch and the resulting message is appended to the
conversation history before the next model call.

Provider shapes (research.md §7):

- **Anthropic**: a ``tool_result`` content block inside a ``user`` message,
  carrying ``tool_use_id``, ``content``, optional ``is_error: true``. We emit
  ``role="tool"`` with the raw result text and let ``_message_to_anthropic``
  build the structured block from ``metadata`` — same shape as the OpenAI
  branch (spec 11 launch fix).
- **OpenAI / DeepSeek / Groq / Together / NVIDIA / OpenRouter / Cloudflare**:
  a separate message with ``role="tool"``, ``tool_call_id``, ``name``,
  ``content``. The error flag is conveyed by prefixing ``content`` with
  ``"Error: "``. Every provider in this family dispatches through
  :class:`persona.backends.openai_compat.OpenAICompatibleBackend` (an
  OpenAI-SDK wire shape) per D-20-X-nvidia-allow-set-extend — the atomic
  invariant is a multi-touch across ``Provider`` (config.py),
  ``DEFAULT_BASE_URLS``, the capability matrices, ``_factory.py``'s
  allow-set, AND this formatter's case tuple. **OpenRouter (Spec 22) shipped
  without touching this file** — any OpenRouter-served persona that
  dispatched a tool crashed the turn on the unknown-provider ``ValueError``
  below until R9-030 closed the gap (found by M2-I2's test work).
- **Ollama / local (HF) shim**: plain-text message with ``role="user"``
  formatted as ``"<tool_name> returned: <content>"``. The shim's bookkeeping
  picks tool-call ids from ``metadata``.

Unknown ``provider_name`` raises :class:`ValueError` — a programmer-error
boundary, NOT a domain exception (D-03-6): providers are a spec-02-controlled
vocabulary (:data:`persona.backends.config.Provider`), so an unrecognised
name means the CALLER is wrong, not that the domain hit a legitimate unknown
case. R9-030 considered degrading a future unknown provider to the
OpenAI-family default with a warn-once instead of raising, but D-03-6 is an
explicit, still-current decision against that: silently reformatting for a
provider whose real wire shape was never verified risks a confusing
downstream 400 at the vendor API instead of a clear error at this boundary.
Fail-fast stays; R9-030's actual fix was restoring 1:1 case coverage of the
``Provider`` vocabulary (the bug was an incomplete case list, not the
fail-fast posture) — see the case arms below.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.schema.conversation import ConversationMessage

if TYPE_CHECKING:
    from persona.schema.tools import ToolCall, ToolResult

__all__ = ["format_tool_result"]


def format_tool_result(
    tool_call: ToolCall,
    result: ToolResult,
    *,
    provider_name: str,
    native: bool = True,
) -> ConversationMessage:
    """Format a tool result for the given provider's API.

    Args:
        tool_call: The originating :class:`ToolCall` (carries ``call_id``).
        result: The :class:`ToolResult` returned by the tool dispatch.
        provider_name: One of ``anthropic``, ``openai``, ``deepseek``,
            ``groq``, ``together``, ``nvidia`` (Spec 20), ``openrouter``
            (Spec 22, R9-030), ``cloudflare``, ``ollama``, ``local`` — the
            full :data:`persona.backends.config.Provider` vocabulary.
        native: Whether the ACTIVE backend will actually issue native
            ``tool_calls`` for this turn — i.e. the caller's
            ``backend.supports_native_tools`` (R9-068). ``False`` forces the
            shim form regardless of ``provider_name``. See the pairing
            invariant below for why this parameter exists.

    Returns:
        A :class:`ConversationMessage` with the correct ``role`` and
        ``content`` for the provider. ``metadata`` carries the tool-call
        bookkeeping every provider needs (``tool_call_id``, ``tool_name``,
        ``is_error``, ``provider_format``).

    Raises:
        ValueError: If ``provider_name`` is not one of the ten supported
            providers. This is a programmer-error boundary (D-03-6), not a
            domain exception — deliberately fail-fast (see the module
            docstring for why R9-030 kept this posture).

    THE PAIRING INVARIANT (R9-068). A ``role="tool"`` message is only legal
    when the PRECEDING ``assistant`` message carries the matching
    ``tool_calls``. The runtime appends that assistant message only when
    ``backend.supports_native_tools`` is true. Before R9-068 this function
    decided the result FORMAT from ``provider_name`` alone, so the two
    decisions read DIFFERENT inputs and could disagree: native-tools support
    is resolved per ``(provider, MODEL)`` against a capability matrix
    (``_native_tools_supported``), while the format branch matched the
    provider only. An openrouter model absent from that matrix therefore
    produced ``supports_native_tools=False`` (assistant message SKIPPED) plus
    a ``role="tool"`` result (native format) — an orphaned tool message, and a
    provider 400: "tool message has no preceding assistant tool call".

    That failure was first seen 2026-06-10 in a MIXED fallback chain and
    patched in ``MultiModelBackend.supports_native_tools`` by widening
    ``all(...)`` to ``any(...)``. That patch only rescued mixed chains; a
    chain whose models are ALL outside the matrix still reported ``False``
    and still emitted ``role="tool"``. R9-068 fixes it at the source instead:
    ``native`` now drives BOTH decisions, so the append and the format cannot
    disagree by construction.
    """
    now = datetime.now(UTC)

    if not native:
        # Shim form: the active backend will NOT emit native tool_calls, so
        # there will be no preceding assistant.tool_calls for a role="tool"
        # message to attach to. Carry the result as ordinary conversation text
        # (identical to the ollama/local branch below) — always legal, never
        # orphanable. provider_format stays "shim" so the backend adapters
        # treat it as text rather than lifting it into a structured block.
        return ConversationMessage(
            role="user",
            content=f"{result.tool_name} returned: {result.content}",
            created_at=now,
            metadata={
                "tool_call_id": tool_call.call_id,
                "tool_name": result.tool_name,
                "is_error": str(result.is_error),
                "provider_format": "shim",
            },
        )

    match provider_name:
        case "anthropic":
            # Spec 11 launch fix: emit role="tool" with the raw result text +
            # metadata, mirroring the OpenAI/DeepSeek path. `_message_to_anthropic`
            # then lifts it into a proper structured `tool_result` block list on
            # a user message. Previously we JSON-encoded the block into `content`
            # (a string) which Anthropic doesn't recognise — broke every native
            # tool round-trip the moment Astrid/Kai actually used a tool.
            return ConversationMessage(
                role="tool",
                content=result.content,
                created_at=now,
                metadata={
                    "tool_call_id": tool_call.call_id,
                    "tool_name": result.tool_name,
                    "is_error": str(result.is_error),
                    "provider_format": "anthropic",
                },
            )

        case "openai" | "deepseek" | "groq" | "together" | "nvidia" | "openrouter" | "cloudflare":
            content = result.content
            if result.is_error and not content.startswith("Error:"):
                content = f"Error: {content}"
            return ConversationMessage(
                role="tool",
                content=content,
                created_at=now,
                metadata={
                    "tool_call_id": tool_call.call_id,
                    "tool_name": result.tool_name,
                    "is_error": str(result.is_error),
                    "provider_format": "openai",
                },
            )

        case "ollama" | "local":
            return ConversationMessage(
                role="user",
                content=f"{result.tool_name} returned: {result.content}",
                created_at=now,
                metadata={
                    "tool_call_id": tool_call.call_id,
                    "tool_name": result.tool_name,
                    "is_error": str(result.is_error),
                    "provider_format": "shim",
                },
            )

        case _:
            # D-03-6 fail-fast, kept deliberately (R9-030): providers are a
            # spec-02-controlled vocabulary, so a name outside it means the
            # CALLER (or a provider added to Provider without touching this
            # match) is wrong — never silently reformat for an unverified
            # wire shape. The message enumerates the full vocabulary so the
            # gap is diagnosable from the exception alone (no source dive).
            msg = (
                f"Unknown provider_name: {provider_name!r}; expected one of "
                "anthropic, openai, deepseek, groq, together, nvidia, openrouter, "
                "cloudflare, ollama, local"
            )
            raise ValueError(msg)
