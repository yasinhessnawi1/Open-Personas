"""Data types crossing the backend boundary.

Frozen Pydantic v2 models with ``extra="forbid"`` everywhere — these
shapes are returned by :class:`persona.backends.protocol.ChatBackend`
implementations and consumed by the runtime (spec 05), the audit log
(future), and the HTTP API (spec 08). See ``docs/specs/spec_02/decisions.md``
D-02-2 for the Pydantic-over-dataclass rationale.

Spec 02 §4.1 enumerates these shapes. The validators here enforce the
invariants the spec describes (e.g., ``TokenUsage.total_tokens`` consistency).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from persona.schema.tools import ToolCall  # noqa: TC001 — Pydantic needs runtime ref

if TYPE_CHECKING:
    from persona.tools.protocol import ToolDescriptor

__all__ = [
    "ChatResponse",
    "ReasoningBlock",
    "StreamChunk",
    "ToolCallDelta",
    "ToolSpec",
    "TokenUsage",
    "reasoning_as_text",
    "tool_spec_from_tool",
]


class TokenUsage(BaseModel):
    """Token accounting for a single backend call.

    Attributes:
        prompt_tokens: Tokens consumed by the prompt (system + history + user).
        completion_tokens: Tokens emitted by the model in the response.
        total_tokens: Sum of the above. Validated to equal
            ``prompt_tokens + completion_tokens``.
        cost_usd: Response-side ACTUAL cost in USD (Spec M2, D-M2-3) — what the
            routed request really cost, parsed fail-open from OpenRouter usage
            accounting (``usage.cost``; OpenRouter credits ≈ USD). ``None``
            when the provider reported none — every non-OpenRouter backend,
            and any absent/malformed value (never a guess). ``0.0`` is a valid
            actual (``:free`` routes). Additive-optional: pre-M2 constructors
            are untouched.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0.0)

    @model_validator(mode="after")
    def _total_matches_sum(self) -> TokenUsage:
        expected = self.prompt_tokens + self.completion_tokens
        if self.total_tokens != expected:
            msg = (
                f"TokenUsage.total_tokens={self.total_tokens} does not equal "
                f"prompt_tokens+completion_tokens={expected}"
            )
            raise ValueError(msg)
        return self


class ToolSpec(BaseModel):
    """A tool description the model uses to decide what to call.

    Distinct from :class:`persona.schema.tools.Tool` (the runtime-side
    Protocol) — ``ToolSpec`` is pure data shipped to the provider. Convert
    via :func:`tool_spec_from_tool`.

    Attributes:
        name: Tool name. Must match the persona YAML's ``tools`` allow-list
            and the :class:`persona.schema.tools.ToolCall.name` the model
            emits back.
        description: One-line description the model uses to decide.
        parameters: JSON Schema for the tool's keyword arguments. We do not
            validate well-formedness here — the provider rejects malformed
            schemas with a 400.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str
    description: str
    parameters: dict[str, Any]


class ToolCallDelta(BaseModel):
    """Incremental tool-call fragment emitted during streaming.

    Backends concatenate ``arguments_delta`` strings keyed by ``call_id``
    until the stream ends, then emit a final :class:`ToolCall` on the
    consumer side. Spec 02 keeps the deltas raw so callers can reconstruct
    or discard as they prefer.

    Attributes:
        call_id: Provider-supplied identifier; concatenation key.
        name_delta: Incremental fragment of the tool name. Most providers
            send the full name in the first delta; some stream it.
        arguments_delta: Incremental fragment of the arguments JSON string.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str
    name_delta: str = ""
    arguments_delta: str = ""


class ChatResponse(BaseModel):
    """Single-call response from a :class:`ChatBackend`.

    Attributes:
        content: Assistant text. Empty string is allowed (tool-only
            responses).
        tool_calls: Structured tool requests parsed from the provider's
            native tool-calling response or from the prompt-based shim.
            Empty if the model produced no tool calls.
        usage: Token accounting (always populated).
        model: Echo of the model name the backend used.
        provider: Provider identifier ("anthropic", "openai", "deepseek",
            "groq", "together", "nvidia", "ollama", "local").
        latency_ms: Wall-clock time from request send to response complete,
            measured client-side via :func:`time.perf_counter`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    content: str
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage
    model: str
    provider: str
    latency_ms: float = Field(ge=0.0)


class ReasoningBlock(BaseModel):
    """One structured reasoning block from a provider that emits typed blocks (D-20-2).

    Used in the list arm of ``StreamChunk.reasoning: str | list[ReasoningBlock] | None``.
    The ``str`` arm covers NVIDIA / OpenAI Chat Completions / DeepSeek-R1
    (single concatenated CoT string). The ``list`` arm covers Anthropic
    extended thinking (and OpenAI Responses API summary-vs-text distinction)
    where the on-wire shape is a sequence of typed blocks that cannot be
    losslessly collapsed to a single string (``signature`` is a cryptographic
    HMAC that MUST round-trip; ``data`` on redacted blocks is an opaque
    encrypted blob).

    Attributes:
        kind: Block discriminator. ``"thinking"`` = plaintext reasoning;
            ``"redacted_thinking"`` = opaque encrypted blob (Anthropic);
            ``"summary"`` / ``"text"`` = OpenAI Responses API distinction.
        text: Plaintext content when ``kind in {"thinking", "summary", "text"}``.
            ``None`` for ``"redacted_thinking"``.
        signature: Anthropic per-block cryptographic HMAC. Opaque to Persona;
            MUST round-trip verbatim if Persona re-sends the block to the
            provider as input on a later turn.
        data: Anthropic redacted opaque encrypted blob. Only set when
            ``kind == "redacted_thinking"``.
        index: Anthropic ``content[]`` block position OR OpenAI Responses
            ``output_index``. Provider-assigned; preserved so callers can
            sort or pair with deltas. ``None`` for str-arm shims that emit
            individual blocks without positional metadata.
        id: Provider-assigned item id (OpenAI Responses ``item_id``).
            ``None`` for providers that don't emit one.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["thinking", "redacted_thinking", "summary", "text"]
    text: str | None = None
    signature: str | None = None
    data: str | None = None
    index: int | None = None
    id: str | None = None


class StreamChunk(BaseModel):
    """One chunk yielded by :meth:`ChatBackend.chat_stream`.

    Attributes:
        delta: Text fragment. Empty string is allowed (e.g., the final
            chunk carrying only usage data).
        tool_call_delta: Tool-call fragment for this chunk, or None.
        is_final: True on the last chunk of the stream. Consumers stop
            iterating after observing this — backends MAY still close their
            iterator after, but ``is_final=True`` is the authoritative end.
        usage: Token accounting; populated only on the final chunk. None on
            intermediate chunks.
        reasoning: Optional reasoning surface (D-20-2). Two arms:

            * ``str`` — single concatenated CoT delta. Used by NVIDIA
              Nemotron, OpenAI Chat Completions ``reasoning_content``,
              and DeepSeek-R1. The runtime concatenates these per-chunk
              strings into one buffer per turn.
            * ``list[ReasoningBlock]`` — typed structured blocks (Anthropic
              extended thinking with ``signature``-bearing thinking blocks
              and opaque redacted blocks; OpenAI Responses API with
              ``summary`` vs ``text`` distinction). Cannot be losslessly
              collapsed to a single string — use :func:`reasoning_as_text`
              for str-only consumers.

            ``None`` when the provider emitted no reasoning on this chunk.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    delta: str
    tool_call_delta: ToolCallDelta | None = None
    is_final: bool = False
    usage: TokenUsage | None = None
    reasoning: str | list[ReasoningBlock] | None = None


def reasoning_as_text(r: str | list[ReasoningBlock] | None) -> str | None:
    """Collapse the list arm of ``StreamChunk.reasoning`` to plain text (D-20-2).

    For str-only consumers (prompt builder, audit logger, UI text rendering)
    that don't need to round-trip Anthropic ``signature`` or distinguish
    OpenAI Responses ``summary`` from ``text``.

    Args:
        r: A reasoning surface — ``str``, ``list[ReasoningBlock]``, or ``None``.

    Returns:
        The concatenated plain text, or ``None`` if no plaintext content
        was present (e.g., a list containing only ``"redacted_thinking"``
        blocks collapses to ``None``, not the empty string).
    """
    if r is None:
        return None
    if isinstance(r, str):
        return r
    parts = [b.text for b in r if b.text is not None]
    return "".join(parts) if parts else None


def tool_spec_from_tool(tool: ToolDescriptor) -> ToolSpec:
    """Convert a tool's metadata surface into a wire-shape :class:`ToolSpec`.

    Spec 01's ``Tool`` Protocol carries ``name``, ``description``, and
    ``parameters_schema`` and is a :class:`ToolDescriptor` subtype. Spec
    03's ``AsyncTool`` also extends ``ToolDescriptor``. This helper accepts
    either (D-03-2) and produces the JSON-shaped counterpart shipped to
    providers. Callers don't redefine the conversion at every site.

    Args:
        tool: Any object satisfying :class:`persona.tools.protocol.ToolDescriptor`.

    Returns:
        A :class:`ToolSpec` with the same name, description, and JSON
        schema dict.
    """
    return ToolSpec(
        name=tool.name,
        description=tool.description,
        parameters=dict(tool.parameters_schema),
    )
