"""What this run has already tried, and what it learned (Spec W1, T10; D-W1-11).

A long agentic run loops. It searches the same phrase that returned nothing, reads the same
file it read four steps ago, and calls the tool that errored with exactly the arguments that
made it error. Each repeat costs a model call, a tool call and a place in the context, and none
of them can produce anything the run does not already have. The model is not being stupid: the
result scrolled past and nothing told it so.

The ledger is that "nothing". It remembers, per run, what every call returned under
``(tool name, canonical arguments)`` and answers two questions before dispatch:

- **Did this exact call already FAIL?** Then it fails again, for every tool without exception,
  and the model is handed the original error plus the one instruction that changes anything:
  try something different. Repeating a deterministic failure is the clearest waste there is.
- **Did this exact call already SUCCEED, and is it safe to replay?** Then the stored result is
  returned at zero cost. "Safe to replay" is an explicit allowlist of read-only tools
  (D-W1-11), never an inference: :class:`~persona.tools.protocol.ToolKind` classifies where a
  tool came from, not whether it changes anything, so inferring from it would happily replay a
  file write or a sandbox run.

Reads are only replayable while nothing has changed underneath them. Any call to a tool NOT on
the allowlist may have written something, so it invalidates every cached read (D-W1-11's
condition: a write between two identical reads must yield fresh content). That is deliberately
blunter than tracking paths: the ledger never touches the filesystem, and a conservative
invalidation costs one re-read while a missed one silently serves a stale file.

Blunter still, and on purpose: an off-allowlist call invalidates whether it SUCCEEDED or
failed (D-W1-42). A shell command, a sandbox run or a file write that errored halfway may
have changed the world before it did, and the ledger cannot tell the difference between that
and a call that never touched anything. The cost of assuming the worst is one re-read.

Per run. A ledger is created with the run and dies with it: nothing here is a cache across
runs, and no state outlives the loop that owns it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Final

from persona.schema.tools import ToolResult

if TYPE_CHECKING:
    from persona.schema.tools import ToolCall

__all__ = [
    "READ_ONLY_TOOLS",
    "REPEAT_ERROR_HINT",
    "CallLedger",
    "LedgerHit",
    "canonical_key",
]

#: The tools whose successful results may be served again from the ledger (D-W1-11).
#:
#: An explicit list, not an inference. Every entry answers the same question twice with the same
#: answer and changes nothing by being asked: a search, a fetch, a read, a projection of state
#: the run already has, or a pure computation. MCP tools are absent and stay absent until a
#: server can declare read-only semantics; adding a name here is one line, and getting it wrong
#: means replaying a side effect, so the bar is "provably changes nothing".
READ_ONLY_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "web_search",
        "web_fetch",
        "file_read",
        "task_introspect",
        "schedule_introspect",
        "datetime",
        "calculator",
        "json_query",
        "regex_match",
        "text_diff",
    }
)

#: What the model is told when it repeats a call that already failed. The prior error alone is
#: not enough: it already had that and tried again anyway, so the hint has to say what to do.
REPEAT_ERROR_HINT: Final = (
    "You already tried this exact call in this run and it failed the same way. "
    "Do not repeat it. Change the arguments, use a different tool, or work with what you have."
)

#: What the model is told when a read is served from the ledger, so a cached answer is never
#: mistaken for fresh evidence that the world still looks this way.
CACHED_RESULT_NOTE: Final = "(unchanged since you asked earlier in this run)"


def canonical_key(call: ToolCall) -> str:
    """The identity of a call: its name plus its arguments in a stable form.

    Arguments are serialised with sorted keys, so ``{"a": 1, "b": 2}`` and ``{"b": 2, "a": 1}``
    are the same call, which is what a model retrying "the same thing" usually produces.
    Anything unserialisable falls back to ``repr``, so an exotic argument makes the key MISS
    rather than raise: the cost of a miss is one dispatch, the cost of a raise is the run.
    """
    try:
        arguments = json.dumps(call.args, sort_keys=True, default=repr)
    except (TypeError, ValueError):  # pragma: no cover - default=repr already covers the field
        arguments = repr(call.args)
    return f"{call.name} {arguments}"


@dataclass(frozen=True)
class LedgerHit:
    """A call the ledger can answer without dispatching.

    ``result`` is what the model receives; ``kind`` says which guard fired, for the step trace
    and the tests (``"repeat_error"`` or ``"cached_read"``).
    """

    result: ToolResult
    kind: str


@dataclass
class _Entry:
    result: ToolResult
    generation: int


@dataclass
class CallLedger:
    """Per-run memory of what has been tried (Spec W1, T10)."""

    _entries: dict[str, _Entry] = field(default_factory=dict)
    #: Bumped by any call that might have changed something. Cached reads from an older
    #: generation are not served, so a write between two identical reads yields fresh content.
    _generation: int = 0

    def check(self, call: ToolCall) -> LedgerHit | None:
        """What the ledger already knows about this exact call, if anything."""
        entry = self._entries.get(canonical_key(call))
        if entry is None:
            return None
        if entry.result.is_error:
            return LedgerHit(result=self._repeat_error(call, entry.result), kind="repeat_error")
        if call.name not in READ_ONLY_TOOLS:
            return None  # a successful side effect is never replayed
        if entry.generation != self._generation:
            return None  # something may have changed underneath this read since
        return LedgerHit(result=self._cached(call, entry.result), kind="cached_read")

    def remember(self, call: ToolCall, result: ToolResult) -> None:
        """Record what this call returned, and invalidate reads if it may have written."""
        if call.name not in READ_ONLY_TOOLS:
            # Something outside the allowlist ran: the world may have moved, so every cached
            # read is now suspect. Outcome does not enter into it (D-W1-42): a write that
            # failed halfway still wrote the half.
            #
            # The bump happens BEFORE the entry is stored, so this call lands in the
            # generation it created rather than the one it invalidated. Otherwise the two
            # guards in :meth:`check` would overlap: a write would be refused for looking
            # stale, and the rule that actually matters (a side effect is never replayed)
            # would never be the reason, which a mutation of it proved by surviving.
            self._generation += 1
        self._entries[canonical_key(call)] = _Entry(result=result, generation=self._generation)

    @staticmethod
    def _repeat_error(call: ToolCall, prior: ToolResult) -> ToolResult:
        return ToolResult(
            tool_name=call.name,
            call_id=call.call_id,
            is_error=True,
            content=f"{prior.content}\n\n{REPEAT_ERROR_HINT}",
        )

    @staticmethod
    def _cached(call: ToolCall, prior: ToolResult) -> ToolResult:
        return prior.model_copy(
            update={
                "call_id": call.call_id,  # this call's id, so the provider can pair it up
                "content": f"{prior.content}\n\n{CACHED_RESULT_NOTE}",
            }
        )
