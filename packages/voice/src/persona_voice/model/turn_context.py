"""The session-bound runtime-collaborator bundle for a voice turn (spec V5 T1).

A voice turn must be conditioned by *exactly* the same persona machinery as a
text turn (spec V5 criteria 1+2; the §8 persona-bypass line). Rather than fork
the text :class:`~persona_runtime.loop.ConversationLoop`, V5 composes that
loop's collaborators into a voice-specific assembly under V4's real-time
orchestration (D-V5-6). :class:`VoiceTurnContext` is the dependency-injection
container holding those collaborators for one voice session:

* the **session-bound** persona, the four typed memory stores, and the live
  conversation (the same :class:`~persona.schema.conversation.Conversation`
  model a text chat uses — unified memory, criterion 3);
* the **shared** runtime pieces — :class:`~persona_runtime.prompt.PromptBuilder`,
  the :class:`~persona_runtime.routing.Router`, the
  :class:`~persona_runtime.tier.TierRegistry`, and the
  :class:`~persona.history.ConversationHistoryManager` — reused directly, never
  reimplemented;
* the **optional** voice-routing inputs — the
  :class:`~persona_runtime.routing.FirstTokenLatencyTracker` (D-V5-2 secondary
  refinement) and the Spec 23 :class:`~persona_runtime.routing.IntelligentRouter`
  (the within-tier model selection the voice TTFT gate layers on, D-V5-2).

The container is frozen: the *references* are fixed for the session (the
``Conversation`` it points at is itself mutated per turn — that is the live
conversation, by design). Construction fails fast (``ENGINEERING_STANDARDS``
§1.2) if any of the four typed stores is missing, because a turn that cannot
retrieve identity / self-facts / worldview / episodic cannot condition the
persona — the exact failure the spec forbids.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona_voice.model.errors import VoiceIntegrationError

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from persona.history import ConversationHistoryManager
    from persona.schema.conversation import Conversation
    from persona.schema.persona import Persona
    from persona.stores.protocol import MemoryStore
    from persona.tools import Toolbox
    from persona_runtime.crisis_encoder import CrisisScorer
    from persona_runtime.prompt import GraphContext, GraphRecency, PromptBuilder
    from persona_runtime.routing import FirstTokenLatencyTracker, IntelligentRouter, Router
    from persona_runtime.tier import TierRegistry
    from persona_runtime.unified_recall import UnifiedProjection

    from persona_voice.agent.language import CallLanguagePlan
    from persona_voice.model.origination_gate import VoiceOriginationGate

__all__ = ["REQUIRED_STORE_KINDS", "VoiceTurnContext"]

#: The four typed memory stores a voice turn must be able to retrieve from to
#: condition the persona (matches the text loop's ``self._stores`` keys —
#: ``loop.py``). Missing any one is a fail-fast construction error.
REQUIRED_STORE_KINDS: tuple[str, ...] = ("identity", "self_facts", "worldview", "episodic")


@dataclass(frozen=True)
class VoiceTurnContext:
    """Session-bound runtime collaborators a voice turn composes (spec V5 T1).

    Built once per voice session by the composition root (the V1 token/session
    bootstrap) and closed over by the ``ModelReplyProducer`` (T4) and the
    memory-write listener (T8). Holds the persona, the four typed stores, the
    live conversation, and the shared runtime pieces — so the voice turn is
    conditioned identically to a text turn (no persona-bypass; D-V5-6).

    Args:
        persona: The session's persona (identity + constraints are constant for
            the session — cached once per session in T2 per D-V5-1).
        stores: The four typed memory stores keyed by kind. MUST contain every
            key in :data:`REQUIRED_STORE_KINDS`; validated at construction.
        conversation: The live conversation (the same model as text chat;
            mutated per turn as messages are appended — unified memory).
        prompt_builder: The shared persona-conditioning prompt assembler
            (reused directly, never reimplemented — D-V5-6).
        router: The rule-based tier router (Spec 05; instant decision).
        tier_registry: Resolves a tier name to its backend + metadata.
        history_manager: The summarise-and-compact history manager (compaction
            runs off the critical path in T6 — D-V5-3).
        latency_tracker: Optional per-model first-token-latency tracker; the
            D-V5-2 voice routing gate's secondary refinement (the static
            ``ModelMetadata.latency_p50_ms`` is the primary gate input).
        intelligent_router: Optional Spec 23 within-tier model selector; the
            voice TTFT gate (D-V5-2) layers on it when the persona enables it.
        toolbox: Optional toolbox of the persona's tools; the voice tool policy
            (T7, D-V5-4) offers the voice-viable + deferred subset to the model.
            ``None`` → a tool-free voice turn.

    Raises:
        VoiceIntegrationError: A required typed store is missing (fail-fast —
            an unconditionable persona would be a persona-bypass, spec V5 §8).
    """

    persona: Persona
    stores: Mapping[str, MemoryStore]
    conversation: Conversation
    prompt_builder: PromptBuilder
    router: Router
    tier_registry: TierRegistry
    history_manager: ConversationHistoryManager
    latency_tracker: FirstTokenLatencyTracker | None = None
    intelligent_router: IntelligentRouter | None = None
    toolbox: Toolbox | None = None
    language: CallLanguagePlan | None = None
    """The per-call language plan (Spec 32 B2). ``reply_language`` drives the
    prompt builder's reply-language injection (B5); ``None`` ⇒ the persona's
    declared default is resolved at prompt-build time (the text-path behaviour)."""
    reply_language_mirror: bool = False
    """Spec V14 (D-V14-12): select the B5 reply-language MIRROR directive instead
    of the pin. ``True`` when the TTS provider auto-follows the reply TEXT's
    language (ElevenLabs) — the persona replies in the USER's language, defaulting
    to its own declared language when unclear (so a two-language conversation
    speaks each reply in its own language on ONE voice). ``False`` (the default)
    is the incumbent pin — byte-identical to the pre-V14 text + Cartesia paths."""
    user_name: str | None = None
    """The caller's display name (Spec K6, K6-D-6), resolved ONCE at session setup
    from our ``users`` table (off the per-utterance path). Drives the "You are
    speaking with {name}." line so the persona addresses the caller by name in the
    call, exactly as in chat. ``None`` ⇒ a nameless caller ⇒ byte-identical prompt."""
    graph_retrieval: Callable[[str], GraphContext] | None = None
    """The owner-scoped graph-knowledge retrieval (K3, D-K3-6). ``None`` ⇒ the
    voice turn runs graph-off. When wired, the reply producer starts it at turn
    onset (concurrent with pre-model work) and takes the result only if ready by
    assembly time — zero serial wall-clock on the TTFT path (the voice profile is
    traversal-off + a tighter node budget)."""
    graph_surfacing_guidance: Callable[[str, GraphRecency], str | None] | None = None
    """The K4 per-category care-text provider for the surfacing slot (K4-D-3).
    ``None`` ⇒ the reserved no-op (no care text rendered). Wired alongside
    ``graph_retrieval`` so a surfaced wellbeing-tagged node rides its care guidance
    on a voice turn exactly as on the text path."""
    crisis_encoder: CrisisScorer | None = None
    """The R6 crisis encoder (R6-D-3). ``None`` ⇒ the voice turn's safety gate is
    lexical-only (V11). The reply producer runs the composed classify OFF the event loop
    (``asyncio.to_thread``) so the CPU-bound score never starves the voice loop; fail-soft→
    R0 (encoder error / not-yet-warm / timeout ⇒ lexical) lives in ``classify_user_message``."""
    unified_recall: Callable[[str], UnifiedProjection] | None = None
    """The K9 unified recall (K9-D-1/D-11), env-gated OFF by default. When wired (via
    ``build_voice_unified_recall``), it REPLACES the separate episodic + graph legs with one
    fused+reranked+gated path, projected into ``episodic`` + ``graph`` — run inside the reply
    producer's ``asyncio.to_thread`` so the reranker never touches the voice loop (a stall
    degrades to fused; K9-D-3/D-4). ``None`` (default) ⇒ today's two-path voice recall,
    byte-identical; ``graph_retrieval`` stays the V13 graph shell then."""
    core_block_provider: Callable[[], str | None] | None = None
    """The K9 core-memory block reader (K9-D-10). ``None`` ⇒ no block, byte-identical. The
    block is background-refreshed on the K8 engine cadence (never the turn path); the voice turn
    only READS it for injection, inside the same off-loop retrieval (acceptance-7)."""
    origination_gate: VoiceOriginationGate | None = None
    """The A9 voice task-origination gate (A9-D-1). ``None`` ⇒ **byte-identical** voice turn
    (the gate is never consulted). When wired, the reply producer runs it AFTER the R1-hard
    safety bypass (crisis precedence) and BEFORE routing/retrieval: a recognized task/schedule
    ask is echoed + confirmed for the ear and then **delegated** to the chat pipeline (voice
    never executes with the mid model — A9-D-5/D-7); a no-cue turn pays only the cheap regex
    and proceeds unchanged (A9-D-1, criterion 2). Graph stays OFF; the gate never reads it."""

    def __post_init__(self) -> None:
        missing = [kind for kind in REQUIRED_STORE_KINDS if kind not in self.stores]
        if missing:
            raise VoiceIntegrationError(
                "VoiceTurnContext is missing required typed memory store(s); the "
                "persona cannot be conditioned without them",
                context={
                    "missing_stores": ",".join(missing),
                    "provided_stores": ",".join(sorted(self.stores)),
                    "persona_id": self.persona.persona_id or "",
                },
            )

    @property
    def persona_id(self) -> str:
        """The session persona's id.

        Raises:
            VoiceIntegrationError: The persona has no ``persona_id`` (it is
                required to key store reads/writes — the text loop raises the
                same invariant).
        """
        pid = self.persona.persona_id
        if pid is None:
            raise VoiceIntegrationError(
                "persona_id is required for a voice turn",
                context={"persona_role": self.persona.identity.role},
            )
        return pid
