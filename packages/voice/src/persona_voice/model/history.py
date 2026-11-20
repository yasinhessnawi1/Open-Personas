"""Off-critical-path history compaction for voice (spec V5 T6; D-V5-3).

Summarise-and-compact (Spec 05) folds old turns into a running summary via a
small-tier model call — seconds of latency. On a live voice turn that would make
the persona go silent mid-conversation (criterion 7, the §3 freeze risk). So V5
splits it:

* :meth:`VoiceHistoryCompactor.live_history` — the **fast** prompt-history view
  used on the live turn. It assembles the *current* compacted summary + the
  un-summarised verbatim tail; it never calls the summariser and never advances
  the compaction boundary (no model call, no mutation). It is correct (never
  loses turns) but may carry a little more verbatim than ``keep_recent`` until
  the background compaction catches up — the PromptBuilder trims to the budget.
* :meth:`VoiceHistoryCompactor.compact` — the **slow** step, awaited only in a
  background inter-turn task (scheduled after the turn is recorded — T8). It
  reuses the text loop's ``boundary > compacted_up_to`` decision math
  (:meth:`is_compaction_due`) and the shared
  :meth:`~persona.history.ConversationHistoryManager.manage` to advance the
  summary + boundary, so the *next* live turn's fast view is already compact.

The discipline (criterion 7): nothing on the user-stops→persona-starts path ever
awaits :meth:`compact`. :meth:`live_history` is the only history call the
generator makes, and it is pure/fast.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Protocol

from persona.schema.conversation import ConversationMessage
from persona_runtime.routing import served_identity

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.backends.protocol import ChatBackend
    from persona.backends.types import BackendAttempt, ChatResponse
    from persona.history import ConversationHistoryManager
    from persona.schema.conversation import Conversation
    from persona_runtime.tier import TierRegistry

__all__ = [
    "LlmUsageListener",
    "SmallTierSummariser",
    "VoiceHistoryCompactor",
    "make_small_tier_summariser",
]

# Mirrors the text loop's summarise instruction (boilerplate summary mechanics,
# not persona conditioning — a deliberate small duplication to avoid coupling the
# voice path to a loop-private constant).
_VOICE_SUMMARISE_INSTRUCTION = (
    "Summarise the following conversation excerpt into a short paragraph, "
    "preserving names, facts, and decisions. Be concise."
)


class VoiceHistoryCompactor:
    """Live-fast / background-slow history management for voice (D-V5-3).

    Args:
        history_manager: The shared summarise-and-compact manager (its
            ``compact_every`` / ``keep_recent`` define the boundary math).
    """

    def __init__(self, history_manager: ConversationHistoryManager) -> None:
        self._hm = history_manager

    def live_history(self, conversation: Conversation) -> list[ConversationMessage]:
        """The fast prompt-history view for a live turn (no model call, no mutation).

        Returns the current compacted summary (if any) followed by the verbatim
        messages not yet folded into it. Never calls the summariser and never
        advances ``compacted_up_to`` — so the live turn never blocks on
        summarisation (criterion 7).
        """
        if conversation.turn_count <= self._hm.compact_every:
            return list(conversation.messages)
        recent = list(conversation.messages[conversation.compacted_up_to :])
        if not conversation.compacted_summary:
            return recent
        summary_message = ConversationMessage(
            role="system",
            content=f"Earlier in this conversation: {conversation.compacted_summary}",
            created_at=datetime.now(UTC),
            metadata={"kind": "compacted_summary"},
        )
        return [summary_message, *recent]

    def is_compaction_due(self, conversation: Conversation) -> bool:
        """Whether a new compaction boundary has been crossed (text-loop math).

        The replica of the text loop's ``_will_compact`` predicate (D-05-X):
        ``turn_count > compact_every`` AND ``boundary > compacted_up_to``.
        """
        if conversation.turn_count <= self._hm.compact_every:
            return False
        boundary = conversation.turn_count - self._hm.keep_recent
        return boundary > conversation.compacted_up_to

    async def compact(
        self,
        conversation: Conversation,
        summariser: Callable[[list[ConversationMessage]], Awaitable[str]],
    ) -> bool:
        """Run one compaction off the critical path (await the slow summarise).

        MUST be awaited only in a background / inter-turn task, never on the live
        turn path. Pre-computes the summary by awaiting ``summariser`` (the
        small-tier call), then hands the shared ``manage`` a sync no-op assembler
        returning the pre-computed text — the D-05-X sync/async bridge, run
        off-path. Mutates ``conversation`` (summary + boundary) for the next turn.

        Returns:
            ``True`` if a compaction ran; ``False`` if none was due.
        """
        if not self.is_compaction_due(conversation):
            return False
        boundary = conversation.turn_count - self._hm.keep_recent
        new_range = list(conversation.messages[conversation.compacted_up_to : boundary])
        precomputed = await summariser(new_range)
        self._hm.manage(conversation, summariser=lambda _messages: precomputed)
        return True


class LlmUsageListener(Protocol):
    """Where a model call's usage goes to be billed (the turn meter's feed, Spec M3)."""

    def note_llm_usage(
        self,
        *,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float | None,
        provider: str,
        model: str,
        failed_tries: tuple[BackendAttempt, ...] = (),
    ) -> None: ...


class SmallTierSummariser:
    """The small-tier async summariser for background compaction (D-V5-3).

    Mirrors the text loop's ``_summarise``: renders the excerpt and asks the
    small tier (boilerplate work) for a short paragraph. Used by the background
    compaction task only — never on the live turn path.

    R9-299: the summary is a paid provider call, so its usage goes to the session's
    turn meter (:meth:`set_usage_listener`), at the model that served it. The meter
    bills it with the next committed turn, as one more model round of that turn.
    Without a listener (no billing wired) the call is made and nothing is recorded.
    """

    def __init__(self, tier_registry: TierRegistry) -> None:
        self._tiers = tier_registry
        self._listener: LlmUsageListener | None = None

    def set_usage_listener(self, listener: LlmUsageListener) -> None:
        """Late-bind the meter (it is built after the recorder that holds this)."""
        self._listener = listener

    async def __call__(self, messages: list[ConversationMessage]) -> str:
        backend = self._tiers.get("small")
        rendered = "\n".join(f"{m.role}: {m.content}" for m in messages)
        now = datetime.now(UTC)
        prompt = [
            ConversationMessage(
                role="system", content=_VOICE_SUMMARISE_INSTRUCTION, created_at=now
            ),
            ConversationMessage(role="user", content=rendered, created_at=now),
        ]
        response = await backend.chat(prompt)
        self._report(backend, response)
        return response.content.strip()

    def _report(self, backend: ChatBackend, response: ChatResponse) -> None:
        """Hand the call's usage to the meter, under the model that served it.

        Read defensively: a backend double may return a response with no usage, and a
        call that reported none has nothing to bill.
        """
        usage = getattr(response, "usage", None)
        if self._listener is None or usage is None:
            return
        served = getattr(response, "served", None)
        provider, model = served_identity(backend, served)
        self._listener.note_llm_usage(
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            cost_usd=usage.cost_usd,
            provider=provider,
            model=model,
            failed_tries=served.charged_failed_tries() if served is not None else (),
        )


def make_small_tier_summariser(tier_registry: TierRegistry) -> SmallTierSummariser:
    """Build the small-tier async summariser for background compaction (D-V5-3)."""
    return SmallTierSummariser(tier_registry)
