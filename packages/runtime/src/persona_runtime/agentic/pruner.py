"""Keeping a long run's per-step cost flat (Spec W1, T10; D-W1-13).

Every tool result a run collects is carried into every later step. A six-search leg pays for
the first search's output six times, and the bill grows quadratically while the model's answer
does not improve: it needed that 4,000-character fetch on the step it arrived, and needs a line
about it thereafter.

The existing :class:`~persona_runtime.agentic.compactor.StepHistoryCompactor` does not help
here, and is deliberately left exactly as it is. It keys off the MODEL'S WINDOW, firing at 80
percent of it, which in a real leg never happens: a 200-second leg measured 24.5k tokens per
step against a 128k window. The window was never the binding constraint. Cost was.

So this prunes on cost. Once a step's context crosses a ceiling (default
:data:`DEFAULT_COST_CEILING_TOKENS`, one env var away from any other value, and zero disables
it), tool results older than the most recent steps are cut to a bounded head with a marker
saying what happened. What is never touched: the first message (the persona block and the
task, the run's floor), anything that is not a tool result, and everything from the last two
STEPS, which the model either has not been sent yet or has seen exactly once.

Steps, not messages (D-W1-41). A step is one model call and every tool call it asked for, so
a step that batches six lookups is six or seven messages, and a tail measured in messages
would cut the oldest results of a step the model has not been sent yet. D-W1-13 rejected
truncating at dispatch for exactly that reason: the model needs a result whole on the step it
arrives, and T11 asks it to batch independent lookups. The loop knows where each step's
messages begin and passes the boundary in; nothing here infers it.

A marker is left in place of what was cut, because a silently shortened result reads to the
model as the whole truth, and it would then reason from a fragment believing it had the rest.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from persona.skills import count_tokens

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.schema.conversation import ConversationMessage

__all__ = [
    "DEFAULT_COST_CEILING_TOKENS",
    "PROTECTED_STEPS",
    "PRUNED_MARKER",
    "ToolResultPruner",
]

#: The per-step context size above which pruning fires, in tokens (D-W1-13). Not a model limit:
#: a cost limit, chosen so an ordinary leg never trips it and a runaway one stops compounding.
DEFAULT_COST_CEILING_TOKENS: Final = 12_000

#: How much of a pruned tool result survives, in characters. Enough to keep what the result WAS
#: (the query, the first hit, the shape of the answer) and let the model decide whether to look
#: again, without carrying the body forever.
DEFAULT_HEAD_CHARS: Final = 400

#: How many whole steps at the tail are off limits (D-W1-41). Two: the step that just ran,
#: whose results the model has not been sent yet, and the one before it, which it has seen
#: exactly once. The loop counts the steps; this is the number it counts to.
PROTECTED_STEPS: Final = 2

#: What replaces the body of a pruned result. It says the content was cut and that asking again
#: is possible, so the model never treats a fragment as the whole answer.
PRUNED_MARKER: Final = (
    "[earlier tool output trimmed to keep this run affordable. "
    "Call the tool again if you need the rest.]"
)


class ToolResultPruner:
    """Trims old tool results once a step's context gets expensive (D-W1-13).

    Args:
        ceiling_tokens: Prune when the context exceeds this. ``0`` disables pruning entirely.
        head_chars: How much of each pruned result is kept.
    """

    def __init__(
        self,
        *,
        ceiling_tokens: int = DEFAULT_COST_CEILING_TOKENS,
        head_chars: int = DEFAULT_HEAD_CHARS,
    ) -> None:
        self._ceiling = max(0, ceiling_tokens)
        self._head = max(0, head_chars)

    def should_prune(self, context: Sequence[ConversationMessage]) -> bool:
        """Is this context expensive enough to be worth trimming?"""
        if self._ceiling <= 0:
            return False
        return self.size(context) > self._ceiling

    @staticmethod
    def size(context: Sequence[ConversationMessage]) -> int:
        """The token cost of sending this context once.

        Counted over the same rendering the loop and the compactor use, so the ceiling means
        the same thing everywhere, and a multimodal block counts as the text it prints as.
        """
        return count_tokens("\n".join(f"{message.role}: {message.content}" for message in context))

    def prune(
        self, context: list[ConversationMessage], *, protect_from: int
    ) -> list[ConversationMessage]:
        """Return the context with old tool results trimmed, or unchanged if under the ceiling.

        Args:
            context: The run's working context.
            protect_from: Index where the last two steps begin. Nothing from there on is
                touched, whatever it is, because the model has not finished reading it.
                Required, and required from the CALLER: the loop knows where a step starts
                and this class would have to guess (D-W1-41).

        Idempotent: an already-pruned result carries the marker and is left alone, so repeated
        passes over a long run neither re-trim nor stack markers.
        """
        if not self.should_prune(context):
            return context
        limit = max(1, protect_from)
        pruned: list[ConversationMessage] = []
        for index, message in enumerate(context):
            if index == 0 or index >= limit or not self._is_prunable(message):
                pruned.append(message)
                continue
            pruned.append(message.model_copy(update={"content": self._trim(str(message.content))}))
        return pruned

    @staticmethod
    def _is_tool_result(message: ConversationMessage) -> bool:
        """Is this message a tool's output, in either shape the formatter produces?

        A native provider carries results as ``role="tool"``. A provider without native tool
        calling cannot (an orphaned tool message is illegal there, R9-068), so the same result
        arrives as ordinary conversation text and is marked in the metadata instead. Pruning
        only the first shape would leave every non-native run paying full price forever.
        """
        if message.role == "tool":
            return True
        return message.role == "user" and "tool_name" in message.metadata

    def _is_prunable(self, message: ConversationMessage) -> bool:
        """A tool result, long enough to be worth cutting, and not already cut."""
        if not self._is_tool_result(message):
            return False
        if not isinstance(message.content, str):
            return False  # multimodal blocks are not text to trim
        if PRUNED_MARKER in message.content:
            return False
        return len(message.content) > self._head

    def _trim(self, content: str) -> str:
        return f"{content[: self._head].rstrip()}\n{PRUNED_MARKER}"
