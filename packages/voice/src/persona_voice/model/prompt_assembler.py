"""Per-session persona-conditioning prompt assembly for voice (spec V5 T2).

The voice turn is conditioned by *exactly* the same machinery as the text turn:
the shared :func:`persona_runtime.retrieval.retrieve_context` +
:meth:`persona_runtime.prompt.PromptBuilder.build` (D-V5-6 — never a thinner
"voice prompt" that drops the persona; spec V5 §8, criteria 1+2).

:class:`VoicePromptAssembler` adds the D-V5-1 real-time adaptation: it caches
the **session-constant** identity store-read once (identity is immutable at
runtime — Spec 01), and per turn retrieves only the **variable** stores
(self-facts / worldview / episodic) before assembling the full prompt through
the shared ``PromptBuilder``. The constant work happens once; the conditioning
content is still fully present on every turn. The history compaction that the
text loop runs synchronously moves off the critical path in T6 — this assembler
takes the already-managed history as input.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona_runtime.prompt import PromptMode
from persona_runtime.retrieval import (
    DEFAULT_RETRIEVE_TOP_K,
    reinforce_recalled,
    retrieve_context,
)

if TYPE_CHECKING:
    from persona.schema.chunks import PersonaChunk
    from persona.schema.conversation import ConversationMessage
    from persona_runtime.prompt import DocumentContext, GraphContext, RetrievedContext

    from persona_voice.model.turn_context import VoiceTurnContext

__all__ = ["VoicePromptAssembler"]


class VoicePromptAssembler:
    """Assembles the persona-conditioned voice prompt, caching the constant block.

    Constructed once per voice session over a
    :class:`~persona_voice.model.turn_context.VoiceTurnContext`. The first turn
    reads identity from its store; every later turn reuses the cached identity
    (D-V5-1) and re-queries only the variable stores. The full conditioning
    (identity + constraints + retrieved memory + history + skills) is assembled
    by the shared ``PromptBuilder`` every turn — no bypass.

    Args:
        context: The session-bound runtime collaborators.
    """

    def __init__(self, context: VoiceTurnContext) -> None:
        self._ctx = context
        self._cached_identity: list[PersonaChunk] | None = None

    def _identity(self) -> list[PersonaChunk]:
        """The session-constant identity chunks (read once, then cached)."""
        if self._cached_identity is None:
            self._cached_identity = self._ctx.stores["identity"].get_all(self._ctx.persona_id)
        return self._cached_identity

    def retrieve(
        self,
        user_message: str,
        *,
        top_k: int = DEFAULT_RETRIEVE_TOP_K,
        history_turns: int | None = None,
    ) -> RetrievedContext:
        """Retrieve this turn's conditioning context (cached identity + variable).

        Uses the shared :func:`~persona_runtime.retrieval.retrieve_context` with
        the cached identity hook (D-V5-1) so the identity store is not re-read.
        ``history_turns`` (the live-history length) drives the dynamic per-turn
        budget and recency-augmented episodic recall, so the persona recalls the
        previous call's tail — the same unified-memory continuity the text path
        gets (criteria 1+2; no persona-bypass).
        """
        context = retrieve_context(
            self._ctx.stores,
            self._ctx.persona_id,
            user_message,
            top_k=top_k,
            identity=self._identity(),
            history_turns=history_turns,
        )
        # Spec K8 (K8-D-5): reinforce the recalled episodic chunks. This method
        # runs inside the reply producer's ``asyncio.to_thread`` worker, so the
        # write executes OFF the voice event loop by construction (the
        # starvation rule); fail-soft inside — never breaks a turn.
        reinforce_recalled(self._ctx.stores, self._ctx.persona_id, context)
        return context

    def build(
        self,
        user_message: str,
        *,
        history: list[ConversationMessage],
        max_tokens: int,
        skill_index: str = "",
        matched_skill_content: str | None = None,
        document_context: DocumentContext | None = None,
        graph: GraphContext | None = None,
        context: RetrievedContext | None = None,
        safety_directive: str | None = None,
    ) -> list[ConversationMessage]:
        """Assemble the full persona-conditioned prompt for one voice turn.

        Args:
            user_message: This turn's transcribed user message.
            history: The already-managed conversation history (compacted summary
                + recent verbatim turns). Compaction runs off the critical path
                (T6); this assembler does not block on it.
            max_tokens: The chosen backend's context-window budget (from routing).
            skill_index: The rendered "available skills" block (voice tool/skill
                scope is decided in T7; defaults to empty here).
            matched_skill_content: Already-budgeted active-skill content, if any.
            document_context: Optional retrieved-document context.
            graph: The overlapped graph-knowledge bundle (K3, D-K3-6), already
                retrieved off the critical path and taken only if ready. ``None``
                ⇒ graph-off (the additive default; the prompt is byte-identical).
                Injected into the retrieved context so the shared ``PromptBuilder``
                renders it exactly as the text path does — one mechanism.
            context: A pre-fetched :class:`RetrievedContext` (the caller already
                ran :meth:`retrieve`, e.g. off-thread so the graph query could
                overlap it — D-K3-6). ``None`` ⇒ this method retrieves inline (the
                historical path). Letting the caller supply it is what makes the
                voice overlap genuine without re-retrieving.

        Returns:
            ``[system, *history, user]`` — the same shape the text loop builds,
            via the shared ``PromptBuilder`` (criteria 1+2; no persona-bypass).
        """
        if context is None:
            context = self.retrieve(user_message, history_turns=len(history))
        if graph is not None:
            context = context.model_copy(update={"graph": graph})
        # The reply must be generated in the language TTS will actually speak
        # (Spec 32 B5). The per-call plan's reply_language already accounts for a
        # TTS fall-back to English, so a persona whose declared language the
        # provider cannot speak gets an English reply, not English phonetics over
        # foreign words. ``None`` ⇒ the prompt builder resolves the persona default.
        reply_language = (
            self._ctx.language.reply_language.value if self._ctx.language is not None else None
        )
        return self._ctx.prompt_builder.build(
            self._ctx.persona,
            context,
            history,
            skill_index,
            user_message,
            max_tokens=max_tokens,
            matched_skill_content=matched_skill_content,
            document_context=document_context,
            reply_language=reply_language,
            graph_surfacing_guidance=self._ctx.graph_surfacing_guidance,
            # V11-D-1: the voice path builds in VOICE mode so the spoken-delivery
            # register (V11-D-2) is present — voice ≠ chat, no longer a mirror.
            # Still the one shared PromptBuilder (D-V5-1, never a thinner prompt);
            # mode gates sections inside it, it does not fork the path.
            mode=PromptMode.VOICE,
            # V11-D-5: the R1-soft override directive (path-independent with the chat
            # loop). ``None`` ⇒ byte-identical, R0 floor intact.
            safety_directive=safety_directive,
            # K6 (K6-D-6): the persona speaks the caller's name in-call, resolved once
            # at session setup (off the per-utterance path). ``None`` ⇒ byte-identical.
            user_name=self._ctx.user_name,
        )
