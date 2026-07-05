"""The gist summarizer seam — Protocol + stub + tier adapter (Spec K8 T5, K8-D-10).

P7 (the local summarizer model) is PARKED; stub-first is the v1 posture, not a
temporary state. This module is the swap-in seam: the sleep-time engine talks
to :class:`Summarizer` only, so P7 lands later as a third implementation with
zero engine change.

The Protocol is **async** (amending K8-D-10's "sync" sketch, gate-flagged at
T5): the engine is an async A0 handler and ``ChatBackend.chat`` is async (the
Synthesizer precedent) — an async seam awaits naturally, where a sync seam
would force the ``asyncio.run``-inside-sync landmine D-05-X explicitly forbids.

From-originals is enforced ON THE MECHANISM (acceptance 3):
:func:`assemble_summarizer_input` is the single function that turns member
chunks into summarizer input, and it REFUSES any chunk that is a gist
(``member_ids`` set, or a gist-kind id) — so summary-of-summary cannot enter
through the input side, independent of the store refusing it as a member
(pyramid T4). The engine may only feed the summarizer through this assembly.

Failure posture: implementations raise :class:`~persona.errors.SummarizerError`
(domain, never a provider exception) — the engine treats a failed cluster as
skipped-and-reported (fail-soft: no gist ⇒ recall over raw chunks; never
blocks a turn).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.errors import SummarizerError
from persona.schema.chunks import PersonaChunk  # noqa: TC001 — runtime protocol signature
from persona.schema.conversation import ConversationMessage
from persona.skills import count_tokens

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.backends import ChatBackend

__all__ = [
    "StubSummarizer",
    "Summarizer",
    "TierSummarizer",
    "assemble_summarizer_input",
]

#: Hard multiple of ``target_tokens`` an implementation may return before the
#: output is truncated at a sentence boundary (K8-D-10: cap 2× target).
_OUTPUT_CAP_FACTOR = 2

_GIST_ID_MARKER = "::episodic_gist::"

_PROMPT_TEMPLATE = (
    "Write a summary of the following conversation excerpts, including as many "
    "key details as possible: facts, names, dates, decisions, and commitments. "
    "Do not add commentary, framing, or information that is not in the "
    "excerpts. Keep it under {target_tokens} tokens.\n\n{content}"
)


def assemble_summarizer_input(chunks: Sequence[PersonaChunk]) -> str:
    """Assemble raw-chunk content into the summarizer's input (acceptance 3).

    The ONLY sanctioned path from memory to the summarizer. Structurally
    from-originals: any gist offered as input (``member_ids`` set, or a
    gist-kind id) raises — never silently dropped, never summarised.
    Chunks are joined oldest-first so the summary reads chronologically.

    Raises:
        SummarizerError: a gist chunk was offered as summarizer input, or the
            input is empty.
    """
    if not chunks:
        raise SummarizerError("summarizer input must be non-empty", context={})
    offending = [c.id for c in chunks if c.member_ids or _GIST_ID_MARKER in c.id]
    if offending:
        raise SummarizerError(
            "summarizer input must be raw chunks only (from-originals, never summary-of-summary)",
            context={"gist_ids": ",".join(offending)},
        )
    ordered = sorted(chunks, key=lambda c: (c.created_at, c.id))
    return "\n\n".join(c.text for c in ordered)


@runtime_checkable
class Summarizer(Protocol):
    """The engine's summarization port (K8-D-10) — P7's future swap-in seam."""

    async def summarize(self, content: str, *, target_tokens: int) -> str:
        """Summarise ``content`` (an :func:`assemble_summarizer_input` product).

        Returns non-empty text aimed at ``target_tokens``. Implementations
        raise :class:`SummarizerError` on failure — never provider exceptions.
        """
        ...


class StubSummarizer:
    """Deterministic no-model summarizer — the test + development posture.

    Head-truncates the content to ``target_tokens`` (token-counted, cut at the
    last sentence boundary that fits). Zero I/O, zero randomness: identical
    input ⇒ identical output, which is what makes the engine's idempotency
    provable without a model in the loop.
    """

    async def summarize(self, content: str, *, target_tokens: int) -> str:
        if not content.strip():
            raise SummarizerError("nothing to summarize", context={})
        return _truncate_at_sentence(content.strip(), max_tokens=target_tokens)


class TierSummarizer:
    """The interim cloud-tier adapter (K8-D-10) — P7's stand-in.

    Wraps a resolved :class:`ChatBackend` (the worker root resolves the tier
    via ``TierRegistry.get(config.episodic_summary_tier)`` — its OWN knob,
    independent of synthesis; the tier's fallback chain is the env's concern
    and must be production-viable providers). Output discipline: empty ⇒
    ``SummarizerError`` (the engine skips the cluster); over-long ⇒ truncated
    at a sentence boundary within 2× target (reported via the return, not an
    error); provider exceptions re-raised as ``SummarizerError`` (domain
    boundary).
    """

    def __init__(self, *, backend: ChatBackend) -> None:
        self._backend = backend

    async def summarize(self, content: str, *, target_tokens: int) -> str:
        from datetime import UTC, datetime

        prompt = _PROMPT_TEMPLATE.format(target_tokens=target_tokens, content=content)
        message = ConversationMessage(role="user", content=prompt, created_at=datetime.now(UTC))
        try:
            response = await self._backend.chat(messages=[message])
        except Exception as exc:
            raise SummarizerError(
                "summarizer backend call failed",
                context={"provider": getattr(self._backend, "provider_name", "unknown")},
            ) from exc
        text = (response.content or "").strip()
        if not text:
            raise SummarizerError(
                "summarizer returned empty output",
                context={"provider": getattr(self._backend, "provider_name", "unknown")},
            )
        return _truncate_at_sentence(text, max_tokens=target_tokens * _OUTPUT_CAP_FACTOR)


def _truncate_at_sentence(text: str, *, max_tokens: int) -> str:
    """Return ``text`` unchanged if within budget, else cut at a sentence end.

    Deterministic: token counts via the shared ``count_tokens`` (D-05-8:
    estimate-for-budgeting). Falls back to a hard token cut when no sentence
    boundary fits (never returns empty for non-empty input).
    """
    if count_tokens(text) <= max_tokens:
        return text
    sentences = text.replace("\n", " ").split(". ")
    kept: list[str] = []
    for sentence in sentences:
        candidate = ". ".join([*kept, sentence])
        if count_tokens(candidate) > max_tokens:
            break
        kept.append(sentence)
    if kept:
        out = ". ".join(kept)
        return out if out.endswith(".") else out + "."
    # No whole sentence fits: hard-cut by words (fail-safe, non-empty).
    words = text.split()
    out_words: list[str] = []
    for word in words:
        if count_tokens(" ".join([*out_words, word])) > max_tokens:
            break
        out_words.append(word)
    return " ".join(out_words) if out_words else words[0]
