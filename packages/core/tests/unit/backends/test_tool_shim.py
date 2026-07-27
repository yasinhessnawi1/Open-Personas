"""Tests for ``persona.backends._tool_shim``.

Covers:
- ``render_tool_instructions`` for empty / single / multiple tools.
- ``parse_tool_calls`` happy path, malformed JSON (fail-safe), multiple
  blocks, no blocks, partial blocks, args validation.
- ``parse_tool_call_delta`` round-trip across streaming fragments.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from persona.backends._tool_shim import (
    ShimState,
    new_shim_state,
    parse_tool_call_delta,
    parse_tool_calls,
    render_tool_instructions,
)
from persona.backends.types import ToolSpec
from persona.schema.tools import ToolCall


def _spec(name: str, *, props: dict[str, Any] | None = None) -> ToolSpec:
    return ToolSpec(
        name=name,
        description=f"Tool {name}.",
        parameters={
            "type": "object",
            "properties": props or {},
        },
    )


class TestRenderInstructions:
    def test_empty_tools_returns_empty_string(self) -> None:
        assert render_tool_instructions([]) == ""

    def test_single_tool_rendered(self) -> None:
        spec = _spec("web_search", props={"query": {"type": "string"}})
        text = render_tool_instructions([spec])
        assert "web_search" in text
        assert "Tool web_search." in text
        assert '{"tool": "<name>"' in text

    def test_multiple_tools_listed(self) -> None:
        a = _spec("a", props={"x": {"type": "integer"}})
        b = _spec("b")
        text = render_tool_instructions([a, b])
        assert "a:" in text
        assert "b:" in text

    def test_required_params_have_no_marker(self) -> None:
        spec = ToolSpec(
            name="x",
            description="x",
            parameters={
                "type": "object",
                "properties": {"q": {"type": "string"}, "k": {"type": "integer"}},
                "required": ["q"],
            },
        )
        text = render_tool_instructions([spec])
        # 'q: string' (required, no '?') and 'k?: integer' (optional)
        assert "q: string" in text
        assert "k?: integer" in text


class TestParseToolCalls:
    def test_no_braces_returns_text_passthrough(self) -> None:
        text = "Just plain text."
        cleaned, calls = parse_tool_calls(text)
        assert cleaned == text
        assert calls == []

    def test_empty_string(self) -> None:
        cleaned, calls = parse_tool_calls("")
        assert cleaned == ""
        assert calls == []

    def test_single_tool_call(self) -> None:
        payload = '{"tool": "web_search", "args": {"query": "kittens"}}'
        cleaned, calls = parse_tool_calls(payload)
        assert calls == [ToolCall(name="web_search", args={"query": "kittens"})]
        assert cleaned == ""

    def test_tool_call_surrounded_by_text(self) -> None:
        text = (
            'I will search now.\n{"tool": "web_search", "args": {"query": "kittens"}}\n'
            "Then I will summarise."
        )
        cleaned, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].name == "web_search"
        assert "search now" in cleaned
        assert "summarise" in cleaned

    def test_multiple_tool_calls_in_order(self) -> None:
        payload = '{"tool": "a", "args": {}} and then {"tool": "b", "args": {"x": 1}}'
        cleaned, calls = parse_tool_calls(payload)
        assert [c.name for c in calls] == ["a", "b"]
        assert calls[1].args == {"x": 1}
        assert "and then" in cleaned

    def test_malformed_json_returns_passthrough(self) -> None:
        # Brackets present but invalid JSON inside → fail-safe (D-02-14).
        text = "{tool: web_search, args: kittens}"
        cleaned, calls = parse_tool_calls(text)
        assert cleaned == text
        assert calls == []

    def test_mixed_good_and_bad_blocks(self) -> None:
        text = 'first {bad json here} second {"tool": "good", "args": {}}'
        cleaned, calls = parse_tool_calls(text)
        assert [c.name for c in calls] == ["good"]
        # Bad block is preserved in cleaned text (it didn't match a tool call).
        assert "bad json here" in cleaned

    def test_missing_tool_field_skipped(self) -> None:
        text = '{"args": {"x": 1}}'
        cleaned, calls = parse_tool_calls(text)
        assert calls == []
        # The block didn't parse as a tool call → kept as text.
        assert cleaned == text

    def test_non_string_tool_field_skipped(self) -> None:
        text = '{"tool": 42, "args": {}}'
        cleaned, calls = parse_tool_calls(text)
        assert calls == []

    def test_non_dict_args_skipped(self) -> None:
        text = '{"tool": "x", "args": [1, 2, 3]}'
        cleaned, calls = parse_tool_calls(text)
        assert calls == []

    def test_nested_braces_in_args(self) -> None:
        text = '{"tool": "x", "args": {"nested": {"a": 1, "b": [2, 3]}}}'
        cleaned, calls = parse_tool_calls(text)
        assert len(calls) == 1
        assert calls[0].args == {"nested": {"a": 1, "b": [2, 3]}}

    def test_unclosed_brace_does_not_crash(self) -> None:
        text = 'I started but never finished {"tool": "x", "args": {'
        cleaned, calls = parse_tool_calls(text)
        # No exception; tool call is not emitted because block is incomplete.
        assert calls == []

    def test_round_trip_render_and_parse(self) -> None:
        spec = _spec("web_search", props={"query": {"type": "string"}})
        instructions = render_tool_instructions([spec])
        # Simulate model output: explanation + JSON tool call.
        model_output = (
            f"I will use the tool now.\n"
            f"{json.dumps({'tool': 'web_search', 'args': {'query': 'x'}})}"
        )
        assert instructions  # used in real prompts; here we just confirm both work
        cleaned, calls = parse_tool_calls(model_output)
        assert calls == [ToolCall(name="web_search", args={"query": "x"})]
        assert "use the tool now" in cleaned


class TestParseToolCallDelta:
    def test_emits_text_for_simple_chunk(self) -> None:
        state = ShimState()
        text, delta = parse_tool_call_delta("hello world", state)
        assert text == "hello world"
        assert delta is None

    def test_emits_delta_when_block_completes(self) -> None:
        state = ShimState()
        text, delta = parse_tool_call_delta('{"tool": "web_search", "args": {"q": "x"}}', state)
        assert delta is not None
        assert delta.name_delta == "web_search"
        assert json.loads(delta.arguments_delta) == {"q": "x"}
        assert text == ""

    def test_split_across_chunks(self) -> None:
        state = ShimState()
        # First chunk: partial JSON.
        text1, delta1 = parse_tool_call_delta('text {"tool": "web_', state)
        assert text1 == "text "
        assert delta1 is None
        # Second chunk: rest of JSON.
        text2, delta2 = parse_tool_call_delta('search", "args": {"q": "x"}}', state)
        assert delta2 is not None
        assert delta2.name_delta == "web_search"
        assert text2 == ""

    def test_text_before_and_after_block(self) -> None:
        state = ShimState()
        text, delta = parse_tool_call_delta('before {"tool": "x", "args": {}} after', state)
        assert delta is not None
        # The 'after' portion is yielded as text on this call.
        assert "before" in text
        assert "after" in text

    def test_call_seq_increments(self) -> None:
        state = ShimState()
        _, delta1 = parse_tool_call_delta('{"tool": "a", "args": {}}', state)
        _, delta2 = parse_tool_call_delta('{"tool": "b", "args": {}}', state)
        assert delta1 is not None
        assert delta2 is not None
        assert delta1.call_id == "shim-1"
        assert delta2.call_id == "shim-2"

    def test_empty_chunk(self) -> None:
        state = ShimState()
        text, delta = parse_tool_call_delta("", state)
        assert text == ""
        assert delta is None

    def test_malformed_json_does_not_emit(self) -> None:
        state = ShimState()
        # `{bad: json}` parses as a block but json.loads fails — fail-safe (D-02-14).
        text, delta = parse_tool_call_delta("{bad: json}", state)
        assert delta is None
        # Body of the failed block was buffered; nothing yielded as text either.

    def test_state_carries_across_invocations(self) -> None:
        state = ShimState()
        parse_tool_call_delta("hello ", state)
        # Buffer should be empty (no open block).
        assert state.brace_depth == 0
        # Now open and close a block in two halves.
        parse_tool_call_delta('{"tool":', state)
        assert state.brace_depth == 1
        _, delta = parse_tool_call_delta(' "x", "args": {}}', state)
        assert delta is not None


class TestNewShimState:
    """R9-046: synthetic call_id numbering must be resumable across rounds."""

    def test_default_reproduces_original_single_round_numbering(self) -> None:
        state = new_shim_state()
        _, delta1 = parse_tool_call_delta('{"tool": "a", "args": {}}', state)
        _, delta2 = parse_tool_call_delta('{"tool": "b", "args": {}}', state)
        assert delta1 is not None
        assert delta2 is not None
        assert delta1.call_id == "shim-1"
        assert delta2.call_id == "shim-2"

    def test_seeded_state_resumes_numbering_from_prior_round(self) -> None:
        # Round 1: a fresh backend-instance counter starts at 0.
        round1 = new_shim_state(starting_call_seq=0)
        _, delta1 = parse_tool_call_delta('{"tool": "a", "args": {}}', round1)
        assert delta1 is not None
        assert delta1.call_id == "shim-1"

        # Round 2: the backend threads round1's final call_seq back in —
        # a fresh ShimState() (call_seq reset to 0) would collide on
        # "shim-1" again; the seeded state must not.
        round2 = new_shim_state(starting_call_seq=round1.call_seq)
        _, delta2 = parse_tool_call_delta('{"tool": "b", "args": {}}', round2)
        assert delta2 is not None
        assert delta2.call_id == "shim-2"
        assert delta2.call_id != delta1.call_id

    def test_three_rounds_never_collide(self) -> None:
        seq = 0
        seen_ids: list[str] = []
        for tool_name in ("a", "b", "c"):
            state = new_shim_state(starting_call_seq=seq)
            _, delta = parse_tool_call_delta(f'{{"tool": "{tool_name}", "args": {{}}}}', state)
            assert delta is not None
            seen_ids.append(delta.call_id)
            seq = state.call_seq  # thread forward, as each backend does post-round
        assert seen_ids == ["shim-1", "shim-2", "shim-3"]
        assert len(set(seen_ids)) == 3


@pytest.mark.parametrize(
    "provider_text",
    [
        'Provider sent: {"tool": "web_search", "args": {"query": "kittens"}}',
        'Bare {"tool": "web_search", "args": {"query": "kittens"}}',
        '{"tool": "web_search", "args": {"query": "kittens"}} (end of message)',
    ],
)
def test_parse_robust_to_position(provider_text: str) -> None:
    _, calls = parse_tool_calls(provider_text)
    assert len(calls) == 1
    assert calls[0].name == "web_search"
