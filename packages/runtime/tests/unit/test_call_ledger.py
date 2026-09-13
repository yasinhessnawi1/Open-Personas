"""The per-run call ledger: what a run has already tried (Spec W1, T10; D-W1-11).

The loop tests prove the guards where they actually matter, in a real run. These pin the
rules the ledger itself holds, one at a time, so a change of policy (what counts as the
same call, what may be replayed, what invalidates a cached read) fails HERE with a
sentence naming the rule, rather than as a puzzling loop test.
"""

from __future__ import annotations

from persona.schema.tools import ToolCall, ToolResult
from persona_runtime.agentic.call_ledger import (
    CACHED_RESULT_NOTE,
    READ_ONLY_TOOLS,
    REPEAT_ERROR_HINT,
    CallLedger,
    canonical_key,
)


def _call(name: str, call_id: str = "c1", **args: object) -> ToolCall:
    return ToolCall(name=name, args=dict(args), call_id=call_id)


def _ok(name: str, content: str = "fine") -> ToolResult:
    return ToolResult(tool_name=name, content=content, is_error=False)


def _err(name: str, content: str = "upstream 503") -> ToolResult:
    return ToolResult(tool_name=name, content=content, is_error=True)


def test_an_unseen_call_is_not_answered() -> None:
    assert CallLedger().check(_call("web_search", query="rent")) is None


def test_the_same_arguments_in_a_different_order_are_the_same_call() -> None:
    """A model retrying "the same thing" rarely reproduces the key order, and a ledger
    that missed on that would guard nothing in practice."""
    assert canonical_key(_call("web_search", a=1, b=2)) == canonical_key(
        _call("web_search", b=2, a=1)
    )


def test_different_arguments_are_a_different_call() -> None:
    assert canonical_key(_call("web_search", query="rent")) != canonical_key(
        _call("web_search", query="deposit")
    )


def test_an_exotic_argument_misses_rather_than_raising() -> None:
    """The cost of a miss is one dispatch; the cost of a raise would be the run."""
    key = canonical_key(_call("web_search", when=object()))
    assert key.startswith("web_search ")


def test_a_repeat_of_a_failed_call_is_refused_with_the_original_error_and_what_to_do() -> None:
    ledger = CallLedger()
    call = _call("web_search", query="rent")
    ledger.remember(call, _err("web_search"))

    hit = ledger.check(_call("web_search", call_id="c2", query="rent"))

    assert hit is not None
    assert hit.kind == "repeat_error"
    assert hit.result.is_error is True
    assert "upstream 503" in hit.result.content  # what went wrong the first time
    assert REPEAT_ERROR_HINT in hit.result.content  # and what to do instead
    assert hit.result.call_id == "c2"  # this call's id, so the provider can pair it


def test_a_failed_call_is_refused_for_every_tool_not_only_read_only_ones() -> None:
    """A deterministic failure repeats deterministically whatever the tool does, so the
    refusal is not limited to reads. Note this says nothing about side effects: a write
    that failed may still have written (D-W1-42), which is why it invalidates reads."""
    ledger = CallLedger()
    call = _call("file_write", path="/tmp/x", content="hi")
    assert "file_write" not in READ_ONLY_TOOLS
    ledger.remember(call, _err("file_write", "permission denied"))

    hit = ledger.check(call)

    assert hit is not None
    assert hit.kind == "repeat_error"


def test_a_repeated_read_is_served_from_the_ledger_and_says_it_is_not_fresh() -> None:
    ledger = CallLedger()
    call = _call("web_search", query="rent")
    ledger.remember(call, _ok("web_search", "three results"))

    hit = ledger.check(_call("web_search", call_id="c2", query="rent"))

    assert hit is not None
    assert hit.kind == "cached_read"
    assert "three results" in hit.result.content
    assert CACHED_RESULT_NOTE in hit.result.content
    assert hit.result.is_error is False
    assert hit.result.call_id == "c2"


def test_a_successful_side_effect_is_never_replayed() -> None:
    """Writes, sandbox runs and anything else off the allowlist run again every time.
    Serving one from a cache would report an effect that did not happen twice."""
    ledger = CallLedger()
    call = _call("code_execution", code="print(1)")
    ledger.remember(call, _ok("code_execution", "1"))

    assert ledger.check(call) is None


def test_a_write_between_two_identical_reads_invalidates_the_cached_read() -> None:
    """D-W1-11's stated condition. The ledger never touches the filesystem, so it cannot
    know WHAT a write touched; it assumes the worst and costs one re-read."""
    ledger = CallLedger()
    read = _call("file_read", path="/notes.md")
    ledger.remember(read, _ok("file_read", "old contents"))
    assert ledger.check(read) is not None  # cached while nothing has happened

    ledger.remember(_call("file_write", path="/notes.md", content="new"), _ok("file_write"))

    assert ledger.check(read) is None  # the read must happen again


def test_a_write_that_failed_invalidates_the_cached_read_too() -> None:
    """D-W1-42. A write, a shell command or a sandbox run that errored may have changed the
    world before it failed, and nothing in an error result says how far it got. The ledger
    assumes the worst, which costs one re-read; assuming the best serves a stale file."""
    ledger = CallLedger()
    read = _call("file_read", path="/notes.md")
    ledger.remember(read, _ok("file_read", "contents"))

    ledger.remember(_call("file_write", path="/notes.md"), _err("file_write", "denied"))

    assert ledger.check(read) is None


def test_another_read_does_not_invalidate_a_cached_read() -> None:
    ledger = CallLedger()
    first = _call("web_search", query="rent")
    ledger.remember(first, _ok("web_search", "results"))
    ledger.remember(_call("file_read", path="/notes.md"), _ok("file_read", "contents"))

    assert ledger.check(first) is not None


def test_mcp_tools_are_not_replayable() -> None:
    """A server's tool can do anything; it stays off the allowlist until one can say
    otherwise. The name shape here is what the MCP toolbox registers."""
    ledger = CallLedger()
    call = _call("mcp__github__search_issues", q="open")
    ledger.remember(call, _ok("mcp__github__search_issues", "12 issues"))

    assert ledger.check(call) is None
