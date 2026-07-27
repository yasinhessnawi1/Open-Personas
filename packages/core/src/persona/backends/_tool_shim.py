"""Prompt-based tool-calling shim for providers without native tools.

Two responsibilities:

1. :func:`render_tool_instructions` — produce a structured instruction
   block appended to the system message, listing the available tools and
   the wire format the model must use.
2. :func:`parse_tool_calls` — extract :class:`ToolCall` blocks from a
   model's text output. Fail-safe: parse errors yield ``(text, [])``
   (D-02-14).

Wire format (D-02-6): each tool call is a JSON object
``{"tool": "name", "args": {...}}``. The parser tolerates surrounding text
and finds blocks by balanced-brace matching, then attempts ``json.loads``
on each candidate. Failed candidates are silently skipped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from persona.backends.types import ToolCallDelta, ToolSpec  # noqa: TC001 — runtime use
from persona.schema.tools import ToolCall

__all__ = [
    "ShimState",
    "new_shim_state",
    "parse_tool_call_delta",
    "parse_tool_calls",
    "render_tool_instructions",
]


_INSTRUCTION_HEADER = (
    "You may call tools by emitting one or more JSON objects in your response, "
    "each shaped exactly:\n"
    '    {"tool": "<name>", "args": {<keyword args>}}\n'
    "Each tool call must be a single self-contained JSON object on its own. "
    "You may include explanatory text before or after, but tool-call JSON "
    "must parse as standalone JSON. Available tools:\n"
)


def render_tool_instructions(tools: list[ToolSpec]) -> str:
    """Render the system-message instruction block for the given tools.

    The block is appended to the system message by the calling backend.
    Empty list → empty string (no header).

    Args:
        tools: Available tools the model may call.

    Returns:
        A string with one ``- name: description (parameters: ...)`` line
        per tool, prefixed by an explanation of the wire format.
        Empty string if ``tools`` is empty.
    """
    if not tools:
        return ""

    lines = [_INSTRUCTION_HEADER]
    for tool in tools:
        params_summary = _summarise_parameters(tool.parameters)
        lines.append(f"- {tool.name}: {tool.description}")
        if params_summary:
            lines.append(f"    parameters: {params_summary}")
    return "\n".join(lines)


def _summarise_parameters(parameters: dict[str, Any]) -> str:
    """Render a one-line summary of a JSON Schema parameters dict."""
    if not parameters:
        return ""
    props = parameters.get("properties")
    if not isinstance(props, dict) or not props:
        return ""
    required = set(parameters.get("required", []))
    parts: list[str] = []
    for pname, schema in props.items():
        ptype = schema.get("type", "any") if isinstance(schema, dict) else "any"
        marker = "" if pname in required else "?"
        parts.append(f"{pname}{marker}: {ptype}")
    return ", ".join(parts)


def parse_tool_calls(model_output: str) -> tuple[str, list[ToolCall]]:
    """Extract tool calls from a model's text response.

    Searches the response for ``{"tool": ...}`` JSON blocks using
    balanced-brace matching, attempts ``json.loads`` on each candidate,
    and collects the well-formed ones into :class:`ToolCall` objects.

    Args:
        model_output: Raw text from the model.

    Returns:
        ``(cleaned_text, tool_calls)``. ``cleaned_text`` has the parsed
        tool-call JSON blocks removed (whitespace collapsed). If no
        parseable blocks are found, ``cleaned_text == model_output`` and
        ``tool_calls == []``. **Never raises** — malformed JSON is treated
        as text passthrough (D-02-14).
    """
    if not model_output or "{" not in model_output:
        return model_output, []

    tool_calls: list[ToolCall] = []
    remaining_segments: list[str] = []
    last_end = 0

    for span_start, span_end, candidate in _iter_brace_spans(model_output):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        tool_name = parsed.get("tool")
        args = parsed.get("args", {})
        if not isinstance(tool_name, str) or not tool_name:
            continue
        if not isinstance(args, dict):
            continue
        tool_calls.append(ToolCall(name=tool_name, args=args))
        remaining_segments.append(model_output[last_end:span_start])
        last_end = span_end

    if not tool_calls:
        return model_output, []

    remaining_segments.append(model_output[last_end:])
    cleaned = " ".join(seg.strip() for seg in remaining_segments if seg.strip())
    return cleaned, tool_calls


def _iter_brace_spans(text: str) -> list[tuple[int, int, str]]:
    """Yield ``(start, end, substring)`` for top-level balanced ``{...}`` runs.

    Naive but robust enough for our use case: tracks brace depth with a
    flag for string literals (escaped quotes respected). Returns each
    top-level brace block independently so multiple tool-call objects in
    one response parse in order.
    """
    spans: list[tuple[int, int, str]] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_string = False
        escape = False
        start = i
        while i < n:
            ch = text[i]
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif in_string:
                if ch == '"':
                    in_string = False
            elif ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    spans.append((start, i + 1, text[start : i + 1]))
                    i += 1
                    break
            i += 1
        else:
            # Unclosed brace — give up on this candidate.
            break
    return spans


@dataclass
class ShimState:
    """Mutable bookkeeping for incremental shim parsing during streaming.

    Used only by :func:`parse_tool_call_delta`. Each backend maintains one
    ``ShimState`` per stream. The state accumulates text fragments and,
    when a complete top-level ``{...}`` block has been seen, attempts to
    parse it and emits a :class:`ToolCallDelta`.

    Attributes:
        buffer: Accumulated text since the last yielded text chunk.
        brace_depth: Current balanced-brace depth inside ``buffer``.
        in_string: True if currently inside a JSON string literal.
        escape: True if the next character is escaped.
        call_seq: Counter used to synthesise call_ids for shim-emitted
            calls (shim has no provider-supplied call ids).
    """

    buffer: str = ""
    brace_depth: int = 0
    in_string: bool = False
    escape: bool = False
    call_seq: int = 0
    pending_block_start: int = -1
    _emitted_text_len: int = field(default=0, repr=False)


def new_shim_state(*, starting_call_seq: int = 0) -> ShimState:
    """Construct a :class:`ShimState` for one LLM round of a (possibly
    multi-round) agentic tool-calling turn.

    Backends must NOT let synthetic ``call_id`` numbering reset to
    ``shim-1`` on every round of a multi-round turn (LLM calls a tool,
    gets the result fed back, calls again, ...) — a fresh ``ShimState()``
    per round would then emit ``shim-1``/``shim-2`` in round 1 AND again
    in round 2, colliding across rounds. Any consumer that pairs
    ``tool_call`` <-> ``tool_result`` by ``call_id`` across the whole
    turn (not just within one round) would then mismatch (R9-046).

    Callers thread the previous round's final ``ShimState.call_seq`` back
    in via ``starting_call_seq`` so numbering keeps counting up instead of
    resetting. Each of the four provider call sites (``openai_compat.py``
    x2, ``hf_local.py``, ``ollama.py``) keeps a small instance-level
    counter (``self._shim_call_seq``) that persists for the life of the
    backend instance — which the runtime's tier registry caches and
    reuses across every round of a turn — and updates it from the
    resulting ``ShimState.call_seq`` once each round's stream completes.
    Default ``starting_call_seq=0`` reproduces the original single-round
    numbering (``shim-1``, ``shim-2``, ...).

    Args:
        starting_call_seq: The call-id sequence number to resume counting
            from (0 for a turn's first round).

    Returns:
        A fresh :class:`ShimState` seeded to continue numbering.
    """
    return ShimState(call_seq=starting_call_seq)


def parse_tool_call_delta(chunk: str, state: ShimState) -> tuple[str, ToolCallDelta | None]:
    """Update ``state`` with ``chunk`` and possibly emit a tool-call delta.

    Streaming counterpart to :func:`parse_tool_calls`. Backends call this
    for every text fragment from the provider; it returns the text the
    consumer should see (non-tool-call text) and at most one completed
    :class:`ToolCallDelta` per call.

    Args:
        chunk: New text fragment from the provider stream.
        state: Mutable bookkeeping carried across calls.

    Returns:
        ``(text_for_consumer, tool_call_delta_or_None)``. The consumer
        passes ``text_for_consumer`` as ``StreamChunk.delta`` and the
        optional delta as ``StreamChunk.tool_call_delta``. Multiple tool
        calls require multiple invocations.
    """
    if not chunk:
        return "", None

    text_segments: list[str] = []
    emitted: ToolCallDelta | None = None
    text_start = 0
    i = 0
    chunk_len = len(chunk)

    while i < chunk_len:
        ch = chunk[i]
        in_block = state.brace_depth > 0 or state.pending_block_start != -1

        if state.escape:
            state.escape = False
        elif ch == "\\":
            state.escape = True
        elif state.in_string:
            if ch == '"':
                state.in_string = False
        elif ch == '"':
            state.in_string = True
        elif ch == "{":
            if state.brace_depth == 0:
                # Flush any pending non-tool text.
                if i > text_start:
                    text_segments.append(chunk[text_start:i])
                state.pending_block_start = len(state.buffer)
                state.buffer += ch
                state.brace_depth = 1
                i += 1
                continue
            state.brace_depth += 1
        elif ch == "}":
            state.brace_depth -= 1
            if state.brace_depth == 0:
                # Block complete — try to parse from state.buffer[pending_block_start:].
                state.buffer += ch
                block_text = state.buffer[state.pending_block_start :]
                try:
                    parsed = json.loads(block_text)
                except json.JSONDecodeError:
                    parsed = None
                if (
                    isinstance(parsed, dict)
                    and isinstance(parsed.get("tool"), str)
                    and parsed.get("tool")
                    and isinstance(parsed.get("args", {}), dict)
                ):
                    state.call_seq += 1
                    emitted = ToolCallDelta(
                        call_id=f"shim-{state.call_seq}",
                        name_delta=parsed["tool"],
                        arguments_delta=json.dumps(parsed.get("args", {})),
                    )
                state.pending_block_start = -1
                state.buffer = ""
                text_start = i + 1
                i += 1
                if emitted is not None:
                    # Consume remainder as text on next call to keep one-delta-per-call.
                    text_segments.append(chunk[text_start:])
                    return "".join(text_segments), emitted
                continue

        if in_block:
            state.buffer += ch
        i += 1

    # Trailing text outside any block.
    if state.brace_depth == 0 and text_start < chunk_len:
        text_segments.append(chunk[text_start:])

    return "".join(text_segments), emitted
