"""Trimming old tool results once a run gets expensive (Spec W1, T10; D-W1-13, D-W1-41).

The loop test proves the saving over a real run, and proves the step boundary where it is
actually computed. These pin what the pruner will and will not touch, because everything it
must NOT touch is a way a run could silently lose the thing it was reasoning from: the
floor, the steps the model has not finished reading, the model's own words, and any result
short enough that trimming it buys nothing.

``protect_from`` is passed in, never inferred (D-W1-41): the caller knows where a step
begins, and a tail measured in MESSAGES cuts the oldest results of a batched step before the
model has been sent them. These tests pass the boundary the loop would pass.
"""

from __future__ import annotations

from datetime import UTC, datetime

from persona.schema.conversation import ConversationMessage
from persona_runtime.agentic.pruner import (
    DEFAULT_COST_CEILING_TOKENS,
    PROTECTED_STEPS,
    PRUNED_MARKER,
    ToolResultPruner,
)

_LONG = " ".join(f"finding{n}" for n in range(600))  # comfortably over any head


def _msg(role: str, content: str, **metadata: str) -> ConversationMessage:
    return ConversationMessage(
        role=role, content=content, created_at=datetime.now(UTC), metadata=dict(metadata)
    )


def _tool_result(content: str, *, native: bool = True) -> ConversationMessage:
    """A tool result in one of the two shapes the formatter produces (R9-068)."""
    if native:
        return _msg("tool", content, tool_name="web_search", provider_format="anthropic")
    return _msg("user", f"web_search returned: {content}", tool_name="web_search")


def _context(*, results: int, native: bool = True) -> list[ConversationMessage]:
    """One search per step: the floor, then an assistant turn and a result per step."""
    context = [_msg("system", "persona block and the task")]
    for _ in range(results):
        context.append(_msg("assistant", "searching"))
        context.append(_tool_result(_LONG, native=native))
    return context


def _last_two_steps(context: list[ConversationMessage], *, per_step: int) -> int:
    """The boundary the loop would pass for a run whose steps are ``per_step`` messages."""
    return max(1, len(context) - PROTECTED_STEPS * per_step)


def test_a_cheap_context_is_returned_untouched() -> None:
    pruner = ToolResultPruner()
    context = _context(results=1)

    assert pruner.should_prune(context) is False
    assert pruner.prune(context, protect_from=1) is context


def test_the_default_ceiling_is_the_ruled_one() -> None:
    """Not a model window: a cost ceiling, chosen so an ordinary leg never trips it."""
    assert DEFAULT_COST_CEILING_TOKENS == 12_000


def test_two_whole_steps_are_protected() -> None:
    """D-W1-41's number: the step that just ran (unread) and the one before it (read once)."""
    assert PROTECTED_STEPS == 2


def test_a_zero_ceiling_disables_pruning_entirely() -> None:
    """The whole rollback: one env var back to 0 and a run behaves exactly as before."""
    pruner = ToolResultPruner(ceiling_tokens=0)
    context = _context(results=20)

    assert pruner.should_prune(context) is False
    assert pruner.prune(context, protect_from=1) is context


def test_old_results_are_cut_to_a_bounded_head_with_a_marker() -> None:
    pruner = ToolResultPruner(ceiling_tokens=200, head_chars=100)
    context = _context(results=6)
    pruned = pruner.prune(context, protect_from=_last_two_steps(context, per_step=2))

    first_result = pruned[2]
    assert first_result.content.startswith(_LONG[:50])  # what it WAS is still readable
    assert PRUNED_MARKER in first_result.content  # and the model is told the rest was cut
    assert len(str(first_result.content)) <= 100 + len(PRUNED_MARKER) + 1  # head + marker


def test_nothing_from_the_protected_steps_is_touched_however_many_messages_they_hold() -> None:
    """The defect D-W1-41 fixes. One step that batched five searches is six messages, so a
    four-message tail would have cut the oldest of the five before the model was sent it."""
    pruner = ToolResultPruner(ceiling_tokens=200, head_chars=100)
    context = [
        _msg("system", "persona block and the task"),
        _msg("assistant", "an older step"),
        _tool_result(_LONG),
        _msg("assistant", "batching five lookups"),
        *[_tool_result(_LONG) for _ in range(5)],
    ]
    batched_step_starts = 3

    pruned = pruner.prune(context, protect_from=batched_step_starts)

    assert PRUNED_MARKER in str(pruned[2].content)  # the older step's result went
    for index in range(batched_step_starts, len(context)):
        assert PRUNED_MARKER not in str(pruned[index].content), index
        assert pruned[index] is context[index]


def test_the_floor_and_the_protected_tail_survive_verbatim() -> None:
    pruner = ToolResultPruner(ceiling_tokens=200, head_chars=100)
    context = _context(results=6)
    pruned = pruner.prune(context, protect_from=_last_two_steps(context, per_step=2))

    assert pruned[0] is context[0]
    assert pruned[-1] is context[-1]
    assert pruned[-4] is context[-4]  # two whole steps of two messages each


def test_the_first_message_is_never_trimmed_whatever_it_is() -> None:
    """The floor rule is positional: index 0 is the persona block and the task, the ground
    the whole run stands on, and the pruner does not inspect it to decide. The shape here
    (a tool result at index 0) cannot arise from the loop, which is the point: the guard
    must not depend on the floor happening to be a system message. A mutation removing it
    survived every other test, so it is pinned here.
    """
    pruner = ToolResultPruner(ceiling_tokens=200, head_chars=100)
    context = [_tool_result(_LONG), *_context(results=6)[1:]]
    pruned = pruner.prune(context, protect_from=_last_two_steps(context, per_step=2))

    assert pruned[0] is context[0]
    assert PRUNED_MARKER not in str(pruned[0].content)


def test_nothing_but_a_tool_result_is_trimmed() -> None:
    """The model's own reasoning and the system's instructions are not tool output, and a
    run that lost them would stop making sense to itself."""
    pruner = ToolResultPruner(ceiling_tokens=200, head_chars=100)
    context = [
        _msg("system", "persona block and the task"),
        _msg("assistant", _LONG),
        _msg("user", _LONG),  # a plain user turn, no tool_name in the metadata
        _tool_result(_LONG),
        _msg("assistant", "still going"),
        _msg("assistant", "still going"),
    ]
    pruned = pruner.prune(context, protect_from=4)

    assert pruned[1].content == _LONG
    assert pruned[2].content == _LONG
    assert PRUNED_MARKER in str(pruned[3].content)


def test_the_shim_shape_is_pruned_too() -> None:
    """A provider without native tool calling carries results as ordinary user text with
    the tool named in the metadata (R9-068). Pruning only the native shape would leave
    every non-native run paying full price forever."""
    pruner = ToolResultPruner(ceiling_tokens=200, head_chars=100)
    context = _context(results=6, native=False)
    pruned = pruner.prune(context, protect_from=_last_two_steps(context, per_step=2))

    assert PRUNED_MARKER in str(pruned[2].content)


def test_pruning_twice_changes_nothing_the_second_time() -> None:
    """A long run prunes at many steps; markers must not stack and heads must not shrink
    with each pass until nothing is left."""
    pruner = ToolResultPruner(ceiling_tokens=200, head_chars=100)
    context = _context(results=6)
    boundary = _last_two_steps(context, per_step=2)
    once = pruner.prune(context, protect_from=boundary)
    twice = pruner.prune(list(once), protect_from=boundary)

    assert [m.content for m in twice] == [m.content for m in once]
    assert str(twice[2].content).count(PRUNED_MARKER) == 1


def test_a_result_shorter_than_the_head_is_left_alone() -> None:
    """Trimming it would buy nothing and cost the marker."""
    pruner = ToolResultPruner(ceiling_tokens=1, head_chars=1000)
    context = [
        _msg("system", "floor"),
        _tool_result("short answer"),
        *[_msg("assistant", "x") for _ in range(4)],
    ]
    pruned = pruner.prune(context, protect_from=4)

    assert pruned[1].content == "short answer"
    assert PRUNED_MARKER not in str(pruned[1].content)


def test_pruning_actually_costs_less() -> None:
    pruner = ToolResultPruner(ceiling_tokens=200, head_chars=100)
    context = _context(results=6)
    boundary = _last_two_steps(context, per_step=2)

    assert pruner.size(pruner.prune(context, protect_from=boundary)) < pruner.size(context)
