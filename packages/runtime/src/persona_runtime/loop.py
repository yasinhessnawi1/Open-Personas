"""The ConversationLoop — the integration keystone (T07).

One ``turn(conversation, user_message)`` runs the full per-turn sequence
(spec §4.1): retrieve context → manage history → build prompt → route →
stream-generate with a tool-call sub-loop → episodic write-back → final chunk.
Everything it composes (stores, history manager, backends, toolbox, skills)
already exists and is green; this module is the conductor.

The decisions that shape this file:

- **D-05-X (sync/async summariser bridge):** the history manager is sync and
  pure. The loop predicts whether ``manage()`` will compact this turn (the same
  ``boundary > compacted_up_to`` math), pre-computes the summary by awaiting the
  small tier, then hands ``manage()`` a sync no-op assembler that returns the
  pre-computed string. Never ``asyncio.run()`` inside that callable.
- **D-05-11 (one round counter):** tool re-prompts and ``use_skill`` re-prompts
  share one ``max_tool_rounds`` budget. At the cap, append a system nudge and do
  one final tool-free generation.
- **D-05-12 (episodic write-back):** the write is the LAST step, after the
  generation loop and before the final yield. Because ``turn`` is an async
  generator, a consumer that stops iterating early (or an exception mid-stream)
  never reaches the write — a failed/cancelled turn writes nothing. No
  ``try/finally``.
- **D-05-13 (tool-call reconstruction):** streamed ``ToolCallDelta`` fragments
  are accumulated by ``call_id`` and parsed into ``ToolCall``s at stream end;
  malformed args JSON → empty dict (the ``@tool`` decorator validates).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, NamedTuple, TypedDict

from persona.audit import AuditAction
from persona.autonomy import policy_for, resolve_autonomy
from persona.backends.types import reasoning_as_text
from persona.errors import SkillCompositionDepthError, SkillCycleError
from persona.logging import get_logger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, make_chunk_id
from persona.schema.conversation import ConversationMessage
from persona.schema.tools import ToolCall, ToolResult, truncated_tool_call_message
from persona.skills import (
    DenyUnvettedConsent,
    collect_skill_supplements,
    count_tokens,
    default_nonce,
    injection_consent_state,
    render_skill_index,
    skill_audit_event,
    subordinate,
)
from persona.skills.composition import AdmissionResult, SkillCompositionState
from persona.tools import format_tool_result

from persona_runtime.activity import dispatch_with_activity
from persona_runtime.agentic.events import RunEvent
from persona_runtime.ambiguity import DetectionContext, detect_ambiguity, should_ask
from persona_runtime.emotional import ConvertMode, FeelingTagConverter, convert_text
from persona_runtime.errors import ScheduleParseError
from persona_runtime.graph_window import set_recent_window_from_messages
from persona_runtime.logging import (
    SkillInvocation,
    TurnLog,
    cost_basis_for,
    detect_tool_refusals,
    estimate_cost_cents,
)
from persona_runtime.proactive_mcp_gap import build_mcp_gap_question, detect_mcp_gap
from persona_runtime.proactive_tool_gap import build_tool_gap_question, detect_tool_gap
from persona_runtime.question_author import TemplateQuestionAuthor
from persona_runtime.questions import QuestionRegistry
from persona_runtime.retrieval import retrieve_context
from persona_runtime.routing import (
    FirstTokenLatencyTracker,
    HeuristicRouter,
    RoutingContext,
    RoutingDecision,
    classifiers,
    reorder_primary,
)
from persona_runtime.safety_intercept import InterceptAction, classify_user_message
from persona_runtime.task_origination import (
    Clause,
    ContractDraft,
    RecognitionKind,
    RescheduleResolutionKind,
    SteeringVerb,
    build_task_originated_event,
    canonicalize_draft,
    changed_clauses,
    classify_amendment_materiality,
    detect_reschedule_cue,
    detect_steering_cue,
    is_affirmative_confirmation,
    render_clause,
    render_echo,
)
from persona_runtime.task_origination.reschedule_flow import assemble_reschedule_echo

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable

    from persona.audit import AuditLogger
    from persona.backends import ChatBackend, StreamChunk, TokenUsage
    from persona.history import ConversationHistoryManager
    from persona.sandbox.result import SandboxFile
    from persona.schedules import QuietHours
    from persona.schema.content import MessageContent
    from persona.schema.conversation import Conversation
    from persona.schema.persona import Persona
    from persona.schema.skills import SkillSpec
    from persona.skills import SkillConsentPort, SkillInjector, SkillScanner
    from persona.stores.protocol import MemoryStore
    from persona.tasks import TaskStateReader
    from persona.tools import Toolbox

    from persona_runtime.crisis_encoder import CrisisScorer
    from persona_runtime.images import TurnImage
    from persona_runtime.logging import TurnLogWriter
    from persona_runtime.prompt import (
        DocumentContext,
        GraphContext,
        GraphRecency,
        PromptBuilder,
        RetrievedContext,
    )
    from persona_runtime.question_author import QuestionAuthor
    from persona_runtime.questions import ProactiveQuestion
    from persona_runtime.routing import IntelligentRouter, Router
    from persona_runtime.task_origination import (
        AmendmentInterpreter,
        RescheduleInterpreter,
        StandingIntentRecognizer,
        SteeringInterpreter,
    )
    from persona_runtime.tier import TierRegistry

__all__ = ["ConversationLoop"]

_logger = get_logger("runtime.loop")

_DEFAULT_MAX_TOKENS = 4096
_MAX_TOOL_ROUNDS_NUDGE = (
    "You have used the maximum number of tool calls for this turn. "
    "Please provide your response based on the information gathered so far."
)
_SUMMARISE_INSTRUCTION = (
    "Summarise the following conversation excerpt into a short paragraph, "
    "preserving names, facts, and decisions. Be concise."
)
# Persisted (never a blank bubble) when a turn produced NO visible text at all —
# only tool calls, with an empty final generation. An honest marker beats an
# empty assistant row that "vanishes on refresh".
_NO_VISIBLE_TEXT_MARKER = "(No response was generated for this turn.)"


def _backend_max_tokens(backend: ChatBackend) -> int:
    """Best-effort context budget for the prompt builder.

    The ``ChatBackend`` Protocol does not expose ``max_tokens`` (it's on the
    backend's ``BackendConfig``), so we read it opportunistically and fall back
    to a safe default. The prompt builder treats this as the whole-prompt window.
    """
    value = getattr(backend, "max_tokens", None)
    return value if isinstance(value, int) and value > 0 else _DEFAULT_MAX_TOKENS


_ROUTING_SUMMARY_AXES: tuple[str, ...] = ("cost", "quality", "latency")


def _routing_event_summary(decision: RoutingDecision) -> dict[str, Any]:
    """Concise model-decision summary for the wire (Spec 31, D-31-1).

    Carries the chosen model, the single dominant scoring axis (argmax of the
    weights actually used), and the model-fallback flag + reason. NEVER the raw
    ``score_vector`` — that stays on the JSONL TurnLog. The frontend templates
    the localised "why" phrase from these structured/enum fields.
    """
    weights = decision.weights_used
    dominant: str | None = None
    if weights:
        dominant = max(_ROUTING_SUMMARY_AXES, key=lambda axis: weights.get(axis, 0.0))
    return {
        "chosen_model": decision.model,
        "dominant_factor": dominant,
        "model_fallback_engaged": decision.model_fallback_engaged,
        "model_fallback_reason": decision.model_fallback_reason,
    }


def _text_chunk(text: str) -> StreamChunk:
    """A non-final content chunk."""
    from persona.backends import StreamChunk

    return StreamChunk(delta=text)


def _final_chunk(usage: TokenUsage | None) -> StreamChunk:
    """The authoritative end-of-turn chunk (``is_final=True``)."""
    from persona.backends import StreamChunk

    return StreamChunk(delta="", is_final=True, usage=usage)


#: The persona's reply on a confirmed contract (Spec A4). The actual task create happens api-side
#: off the emitted event; this is the immediate, honest acknowledgement.
_CONTRACT_CONFIRMED_TEXT = "Done — I've set that up. I'll keep you posted."

#: Steering acknowledgements (Spec A4, T9b). The actual mutation happens api-side off the event.
_STEER_PAUSED_TEXT = "Paused — just say the word when you want it going again."
_STEER_RESUMED_TEXT = "Done — it's running again."
_STEER_CANCELLED_TEXT = "Cancelled — I've stopped that task."

#: Reschedule acknowledgements (Spec A8, T6). The actual retime/rerule happens api-side off the
#: emitted event, through the CAS-guarded door; this is the immediate honest ack.
_RESCHEDULE_APPLIED_TEXT = "Done — I've moved it. The next run is at the new time."
_RESCHEDULE_DECLINE_TEXT = (
    "I couldn't set that cadence reliably — want to try a fixed daily or weekly time instead?"
)


def _pending_cancel_task_id(conversation: Conversation) -> str | None:
    """The task id of a pending cancel confirmation, if the last assistant turn proposed one."""
    for message in reversed(conversation.messages):
        if message.role != "assistant":
            continue
        raw = message.metadata.get("cancel_proposal")
        return raw if isinstance(raw, str) else None
    return None


def _pending_reschedule_event(conversation: Conversation) -> dict[str, object] | None:
    """The pending ``task_rescheduled`` event payload, if the last assistant turn proposed one."""
    import json

    for message in reversed(conversation.messages):
        if message.role != "assistant":
            continue
        raw = message.metadata.get("reschedule_proposal")
        if not isinstance(raw, str):
            return None
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else None
    return None


def _render_amendment(before: ContractDraft, after: ContractDraft) -> str:
    """Re-echo an amendment (Spec A4, T9): a material change re-echoes the whole contract for a
    fresh confirm; a tuning change re-echoes just the changed clause(s). Both stay pending — the
    user must still give a clean confirmation."""
    from persona.approvals.records import Materiality

    if classify_amendment_materiality(before, after) is Materiality.MATERIAL:
        return (
            "That's a bigger change — here's the updated plan:\n"
            f"{render_echo(after)}\n\nShall I go ahead?"
        )
    clauses = changed_clauses(before, after) or (Clause.GOAL,)
    lines = "\n".join(render_clause(after, clause) for clause in clauses)
    return f"Done:\n{lines}\n\nShall I go ahead?"


def _pending_contract_draft(conversation: Conversation) -> ContractDraft | None:
    """The contract draft of the most recent assistant turn, if it was a proposal (Spec A4).

    Returns the parsed :class:`ContractDraft` when the last assistant message carries a
    ``contract_proposal`` (so this turn's user message is its confirm/adjust reply), else
    ``None``. Only the *most recent* assistant turn counts — a proposal two turns back has
    already been superseded.
    """
    for message in reversed(conversation.messages):
        if message.role != "assistant":
            continue
        raw = message.metadata.get("contract_proposal")
        return ContractDraft.model_validate_json(raw) if isinstance(raw, str) else None
    return None


def _build_multimodal_user_message(
    user_message: str, images: list[TurnImage]
) -> list[MessageContent]:
    """Compose the current turn's multimodal user content (image-workspace cascade).

    Returns ``[TextContent(user_message), ImageContent(...), ...]`` — the text
    first, then one :class:`ImageContent` per uploaded image in caller order.
    The text block is always present (even for an empty message) so the
    resulting list is never the degenerate single-TextContent shape
    :class:`ConversationMessage` forbids, and the image blocks flow straight
    into the backend vision serialisers.

    Each block carries the already-resolved bytes inline (``inline_bytes``):
    the chat tier backend is app-scoped/cached and never receives a per-request
    ``workspace_root``, so the serialiser would otherwise have no way to read
    the upload — the image would silently never reach the model. The caller
    (``chat_service``) has already resolved the bytes from the upload store into
    each :class:`TurnImage`, so we pass them through here. ``inline_bytes`` is
    in-memory only and is dropped at the API persistence boundary (content is
    collapsed to its text blocks), so the Spec 13 store-by-reference invariant
    (D-13-X-now option c) is unaffected — the ``messages`` table still grows
    with reference count, not image bytes.
    """
    from persona.schema.content import ImageContent, TextContent

    blocks: list[MessageContent] = [TextContent(text=user_message)]
    blocks.extend(
        ImageContent(
            workspace_path=img.workspace_path,
            media_type=img.media_type,
            inline_bytes=img.content_bytes,
        )
        for img in images
    )
    return blocks


def _stage_images_as_input_files(images: list[TurnImage]) -> list[SandboxFile]:
    """Stage uploaded images as sandbox input files (image-workspace cascade Part 4).

    Each image becomes a :class:`SandboxFile` carrying its bytes inline at the
    same ``workspace_path`` it is referenced by, so ``code_execution`` finds it
    under the sandbox input mount (``/workspace/in/<workspace_path>``). The
    files are appended to the loop's ``deferred_input_files`` and drained by the
    composition root's input-files provider on the next ``code_execution``
    dispatch.
    """
    from persona.sandbox.result import SandboxFile

    return [
        SandboxFile(
            path=img.workspace_path,
            content_bytes=img.content_bytes,
            size_bytes=len(img.content_bytes),
            media_type=img.media_type,
        )
        for img in images
    ]


@dataclass
class _RoundOutcome:
    """Mutable accumulator a streamed round fills while it yields text deltas.

    Lets ``_stream_round`` yield each text delta as it arrives (so the SSE streams
    char-by-char — acceptance §6 #2) while still capturing the full text, the
    reconstructed tool calls, and the final usage for the tool sub-loop +
    episodic write-back.

    Spec 20 T12 (D-20-5): ``reasoning_text`` accumulates the raw reasoning
    delta strings emitted by ``StreamChunk.reasoning`` (str arm) across the
    round. The runtime hashes this at write-back and discards the raw text
    — content-hash-only persistence per the D-15-X-hard-line-filter
    precedent.
    """

    text: str = ""
    calls: list[ToolCall] = field(default_factory=list)
    usage: TokenUsage | None = None
    reasoning_text: str = ""


@dataclass(frozen=True)
class _ProactiveDecision:
    """Outcome of the spec-21 proactive-question decision (T06).

    Exactly one of the two fields is meaningful at a time: ``question`` set →
    ask it and end the turn; ``assumption_nudge`` set → a signal fired but was
    not asked (gated/deduped/class-C), so prepend this stated-assumption system
    instruction (D-21-18) and generate normally. Both ``None`` is never
    returned (the caller gets ``None`` instead when no signal fired).
    """

    question: ProactiveQuestion | None = None
    assumption_nudge: str | None = None


class _ComposedSkill(NamedTuple):
    """Result of one ``use_skill`` composition step (Spec 24, D-24-4).

    ``content`` is the new accumulated active-skill-content block to splice into
    the next prompt (``None`` when nothing was injected); ``message`` is a
    system message to surface to the model (``None`` when the skill activated
    cleanly); ``record`` is the telemetry entry for an activated skill (``None``
    when the skill was refused/skipped).
    """

    content: str | None
    message: str | None
    record: SkillInvocation | None


class ConversationLoop:
    """Orchestrates a single conversation turn (spec §4).

    Pure dependency injection (engineering standards §1.2). The loop owns no
    state beyond its collaborators; it receives the ``Conversation`` per
    :meth:`turn` and never owns it (D-S05-4). The composition root owns the
    :class:`~persona_runtime.tier.TierRegistry` lifecycle, not the loop (D-05-4).
    """

    def __init__(
        self,
        *,
        persona: Persona,
        stores: dict[str, MemoryStore],
        toolbox: Toolbox,
        skill_scanner: SkillScanner,
        skill_injector: SkillInjector,
        scanned_skills: list[SkillSpec],
        history_manager: ConversationHistoryManager,
        prompt_builder: PromptBuilder,
        router: Router,
        tier_registry: TierRegistry,
        turn_log_writer: TurnLogWriter,
        max_tool_rounds: int = 5,
        latency_tracker: FirstTokenLatencyTracker | None = None,
        question_author: QuestionAuthor | None = None,
        intelligent_router: IntelligentRouter | None = None,
        day_spent_cents_provider: Callable[[], float] | None = None,
        graph_retrieval: Callable[[str], GraphContext] | None = None,
        nonce_source: Callable[[], str] | None = None,
        skill_consent: SkillConsentPort | None = None,
        audit_logger: AuditLogger | None = None,
        graph_surfacing_guidance: Callable[[str, GraphRecency], str | None] | None = None,
        crisis_encoder: CrisisScorer | None = None,
        user_name_provider: Callable[[], str | None] | None = None,
        self_node_sync: Callable[[str | None], None] | None = None,
        standing_recognizer: StandingIntentRecognizer | None = None,
        amendment_interpreter: AmendmentInterpreter | None = None,
        steering_interpreter: SteeringInterpreter | None = None,
        task_reader_provider: Callable[[], TaskStateReader | None] | None = None,
        reschedule_interpreter: RescheduleInterpreter | None = None,
        timezone_provider: Callable[[], str] | None = None,
        quiet_hours_provider: Callable[[], QuietHours | None] | None = None,
    ) -> None:
        self._persona = persona
        self._stores = stores
        self._toolbox = toolbox
        # Spec A4 (A4-D-2 / A4-D-X): the standing-intent → contract flow. ``None`` → the
        # contract gate is inert and the turn behaves exactly as before (additive). When
        # present, a standing-intent turn opens the compact echo and a clean confirm reply
        # emits the ``task_originated`` event the api worker turns into a task.
        self._standing_recognizer = standing_recognizer
        # Spec A4 (A4-D-4): adjust-by-reply on a pending proposal. ``None`` → a non-confirm
        # reply abandons the proposal; when present, a tweak amends the draft + re-echoes the
        # changed clause and stays pending (only a clean confirm creates).
        self._amendment_interpreter = amendment_interpreter
        # Spec A4 (T9b, criterion 8): steer a LIVE task by saying so (pause/resume/cancel).
        # Both ``None`` → the steering gate is inert. The reader provider resolves "which task"
        # (list-then-resolve) + the goal for the cancel consequence; pause/resume apply
        # immediately, cancel asks a consequence-aware confirmation first.
        self._steering_interpreter = steering_interpreter
        self._task_reader_provider = task_reader_provider
        # Spec A8 (T6): the conversational reschedule verb — retime/rerule a live task ("move it to
        # 9", "make it weekly", "skip tomorrow's"). ``None`` → inert. When present it joins the
        # steering seam as a propose→confirm flow: resolve the target (honest — ambiguous lists +
        # asks), re-echo the FULL new clause in the user's tz (+ a quiet-hours warn), and a clean
        # confirm emits ``task_rescheduled`` for the worker to apply through the CAS door.
        self._reschedule_interpreter = reschedule_interpreter
        self._timezone_provider = timezone_provider
        self._quiet_hours_provider = quiet_hours_provider
        # Spec S1 (S1-D-2 / S1-D-X-nonce-injection): the per-injection delimiter
        # nonce source for the subordination guard. ``None`` defaults to the
        # production ``default_nonce`` (secrets-backed); tests pin it for
        # determinism. An unpredictable nonce per injection defeats an
        # adversarial SKILL.md forging the guard's closing marker to escape it.
        self._nonce_source = nonce_source or default_nonce
        # Spec S1 (S1-D-6/D-7): the enable-time consent gate + the injection
        # audit. Consent defaults to DENY-unvetted (above-threshold skills are
        # not injectable until Spec S3 wires a real consent store); ``builtin``/
        # ``vetted`` never consult it. The audit logger is optional — ``None`` is
        # the no-op sink (the audit must never crash or perturb the turn).
        self._skill_consent: SkillConsentPort = skill_consent or DenyUnvettedConsent()
        self._audit = audit_logger
        # K3 (D-K3-X-a2-seam): the owner-scoped graph-knowledge retrieval, queried
        # per turn alongside the persona's own memory. ``None`` (the default) is
        # the additive zero-graph path — every existing caller is byte-identical
        # until the composition root wires a retriever (RuntimeFactory).
        self._graph_retrieval = graph_retrieval
        # K4 (K4-D-3 / K4-D-X-surfacing-recency-seam): the per-category care text that
        # rides K3's surfacing slot for any injected wellbeing-tagged node. ``None`` (the
        # default) is the reserved no-op — the prompt renders no care text, byte-identical.
        self._graph_surfacing_guidance = graph_surfacing_guidance
        # R6 (R6-D-3): the app-scoped crisis encoder the composition root wires. ``None``
        # (the default) is the additive lexical-only path — byte-identical to V11 until a
        # root injects it (RuntimeFactory). Composed with the lexical gate inside
        # ``classify_user_message``; fail-soft→R0 lives there.
        self._crisis_encoder = crisis_encoder
        # K6 (K6-D-6): the display name of the person this turn is with, resolved per
        # turn from OUR ``users`` table via a provider closure (contextvar → DB, the
        # same owner scope as graph retrieval). ``None`` (the default) ⇒ no name line
        # in the prompt, byte-identical. K6-D-4 addendum: the runtime prompt path is
        # the SELF node's creation trigger — ``self_node_sync`` create-or-renames it to
        # the resolved name once per turn (a no-op for a nameless turn). Both ``None``
        # until the composition root wires them (RuntimeFactory) — additive, no regression.
        self._user_name_provider = user_name_provider
        self._self_node_sync = self_node_sync
        # Spec 21 T06: proactive clarifying questions (D-21-1). The author turns
        # an ambiguity signal into the 3+1 question; the template author is the
        # D-21-14 mandatory fallback and the default, a model-backed author is
        # injected when available. Not feature-flagged — core v0.1 autonomy.
        self._question_author = question_author or TemplateQuestionAuthor()
        self._scanner = skill_scanner
        self._injector = skill_injector
        self._scanned_skills = scanned_skills
        self._skills_by_name = {s.name: s for s in scanned_skills}
        self._history = history_manager
        self._builder = prompt_builder
        self._router = router
        self._tiers = tier_registry
        self._turn_log_writer = turn_log_writer
        self._max_tool_rounds = max_tool_rounds
        # Spec 25 T12 (§2.1 / D-25-5/6 / D-25-X-t12-window-location): rolling
        # 10-turn fallback-rate window lives HERE in the turn loop (turns ≠
        # backend calls; one backend instance serves many conversations, so the
        # window cannot live on the backend). ``_fallback_alerting`` is the
        # hysteresis state: enter ALERTING at strict >30% (≥4/10) with a
        # min-sample guard; clear only at ≤20% (≤2/10). Edge-triggered logging.
        self._fallback_window: deque[bool] = deque(maxlen=10)
        self._fallback_alerting = False
        # Spec 25 T21 (§2.9 RISKY half) — refusal auto-retry guardrail.
        # DEFAULT-OFF: only an explicit truthy ``PERSONA_REFUSAL_RETRY_ENABLED``
        # ("1"/"true"/"yes", case-insensitive) arms it. When off the turn loop
        # is detection-only (T11/T12 observability). Read once at construction.
        self._refusal_retry_enabled = os.environ.get(
            "PERSONA_REFUSAL_RETRY_ENABLED", ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        # Spec 18 T06: per-process first-token-latency tracker
        # (D-18-X-first-token-measurement-impl). Composition-root-owned so
        # multiple loops share a single EWMA estimate per model; an unset
        # tracker is the legacy path (no measurement, no UnifiedRouter
        # latency signal).
        self._latency_tracker = latency_tracker
        # Spec 23 T11: opt-in model-within-tier selection (D-23-X-seam-shape).
        # ``None`` (the default) is the v0.1 path — every existing caller stays
        # byte-identical. The per-session spend tally is loop-owned (D-23-7 /
        # D-25-X-t12-window-location precedent: the loop owns the rolling window,
        # not the stateless router/backend); it accumulates each turn's cost and
        # feeds the soft per-session budget ramp.
        self._intelligent_router = intelligent_router
        self._session_spent_cents: float = 0.0
        # Spec R7 (R7-D-1 discharge of D-23-X): per-day enforcement is no longer
        # deferred — the DURABLE cross-session per-day number is ``turn_logs``
        # (recorded actual ``cost_cents``), read via an injected provider closure
        # (the runtime_factory provider pattern). The old fail-loud construction
        # guard (a configured ``max_cents_per_day`` couldn't be enforced without a
        # cross-session store) is REMOVED: the soft per-day cost-bias ramp now runs
        # against real recorded spend. ``None`` (no provider wired — unit tests, or
        # the community/no-DB path) fails soft to ``0.0`` = the pre-R7 behaviour, so
        # nothing regresses. NB: the ramp reads ``turn_logs`` (cents), NOT R7's
        # ``day_spend`` credits counter — the units never matched; the ledger did.
        self._day_spent_cents_provider = day_spent_cents_provider
        # Spec 18 T06 strangler-fig affordance: legacy callers may have
        # constructed `Router()` without a registry; ensure the router's
        # registry slot is wired so `route(context)` can do Layer 1 filtering.
        # New callers should construct `HeuristicRouter(tier_registry=...)`
        # directly. Documented mutation per D-18-X-strangler-fig-alias-shape.
        if isinstance(router, HeuristicRouter) and router._tier_registry is None:  # noqa: SLF001
            router._tier_registry = tier_registry  # noqa: SLF001
        # M1a per-turn deferred input_files (D-16-2, D-16-2-state-location).
        # Mutated by the use_skill intercept; drained by the composition
        # root's deferred_input_files_provider callable wired into the
        # code_execution factory. Cleared at every turn() entry.
        self.deferred_input_files: list[SandboxFile] = []

    async def turn(
        self,
        conversation: Conversation,
        user_message: str,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
        *,
        turn_has_image: bool = False,
        images: list[TurnImage] | None = None,
        documents: list[SandboxFile] | None = None,
        document_context: DocumentContext | None = None,
        consent_granted_tools: list[str] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Process one user turn, yielding StreamChunks for the response.

        Handles tool calls and ``use_skill`` activation internally (dispatch →
        feed result → re-generate, capped at ``max_tool_rounds``). Writes
        episodic memory + a :class:`TurnLog` only after generation completes
        (D-05-12). The current ``user_message`` and the assistant response are
        appended to ``conversation`` on success.

        Args:
            conversation: The live conversation (mutated on success; D-S05-4).
            user_message: This turn's user message.
            on_event: Optional async callback for granular turn events
                (``tool_calling`` / ``tool_result`` as tools dispatch, and a
                run-level ``tier`` event once the tier is chosen). Mirrors
                :meth:`AgenticLoop.run`'s ``on_event`` and reuses the SAME
                :class:`RunEvent` shapes — one event vocabulary covers both the
                chat and run-viewer SSE streams (the API maps them). Fired in
                order, interleaved with the yielded text chunks. ``None`` → the
                events are simply not surfaced (the loop's behaviour is
                otherwise unchanged). Events use ``step=-1`` (a turn is a single
                conceptual step, not the run viewer's numbered steps).
            turn_has_image: Whether the current user message carries any
                :class:`~persona.schema.content.ImageContent` block. Threaded
                through to :meth:`Router.choose` so the tier pre-filter can
                restrict the candidate set to vision-capable tiers (T13-T09).
                Caller computes this from the raw user message before
                normalising to ``str``. Defaults to ``False`` for the legacy
                text-only call sites.
            images: The uploaded images attached to this turn (image-workspace
                cascade). When present they are routed to BOTH destinations:
                (a) the model — folded into the current user message as a
                multimodal ``[TextContent, ImageContent, ...]`` list so the
                backend vision serialisers send them, and (b) the sandbox —
                staged as :class:`SandboxFile` entries on
                ``deferred_input_files`` so ``code_execution`` can read them as
                files. ``None``/empty leaves the text-only path unchanged
                (``content=str``, nothing staged). The caller resolves the
                image bytes from the upload store before calling.
            documents: Uploaded NON-image documents (.md/.pdf/.txt/...) attached
                to this turn (document-workspace cascade). Each is staged onto
                ``deferred_input_files`` so ``code_execution`` / a sandbox
                ``file_read`` can read the ACTUAL uploaded file (not just the
                ``document_context`` synopsis). The caller resolves the bytes
                from the upload/document store into :class:`SandboxFile`
                carriers (``path`` = the sandbox-relative ``uploads/<name>``
                reference). ``None``/empty stages nothing. The model still sees
                the textual synopsis via ``document_context``; this param is the
                sandbox leg only.
            consent_granted_tools: Tools the user enabled (via the runtime
                tool-consent flow, spec 26 T11) immediately before this turn —
                recorded on the turn's ``TurnLog.tool_consent_granted`` for
                telemetry (T12). ``None``/empty for ordinary turns; the API
                passes the granted names on the retry-after-consent message.

        Yields:
            :class:`StreamChunk` objects ending with ``is_final=True``.
        """
        persona_id = self._require_persona_id()
        started = time.perf_counter()
        self.deferred_input_files.clear()  # M1a per-turn reset (D-16-2)

        # R1 turn-time safety gate (Spec V11, V11-D-5). Classify the user message
        # ONCE per turn (sub-ms, no model call). R1-hard (acute, explicit W1) is the
        # OUT-OF-BAND BYPASS: the persona — and the lock that is adversarially biased
        # against noticing crisis — is taken out of the loop entirely. We emit the
        # deterministic safe completion INSTEAD of generating, persist it as the
        # turn's response, and return. One acute path: bypass (never inject AND
        # generate). A SOFT verdict instead injects the override directive below
        # (path-independent with the voice path); NONE leaves the always-on R0 floor.
        safety_verdict = classify_user_message(
            user_message,
            locale=self._persona.identity.language_default,
            encoder=self._crisis_encoder,
        )
        if safety_verdict.action is InterceptAction.HARD and safety_verdict.completion is not None:
            completion_text = safety_verdict.completion.chat_text
            yield _text_chunk(completion_text)
            now_hard = datetime.now(UTC)
            conversation.messages.append(
                ConversationMessage(role="user", content=user_message, created_at=now_hard)
            )
            conversation.messages.append(
                ConversationMessage(role="assistant", content=completion_text, created_at=now_hard)
            )
            # Persist to episodic so history + recall reflect the exchange. No
            # TurnLog: no model ran, so there is no usage/tier telemetry to record.
            self._write_episodic(persona_id, user_message, completion_text)
            yield _final_chunk(None)
            return

        # Image-workspace cascade (Parts 1/2/4): when the turn carries uploaded
        # images, fold them into BOTH destinations up front.
        #   (a) Model — ``user_prompt_content`` becomes a multimodal
        #       ``[TextContent, ImageContent, ...]`` list so the PromptBuilder
        #       emits a vision-bearing user message (resolved by the backend
        #       serialisers at send time). It is also the write-back content so
        #       the persisted turn matches what the model saw.
        #   (b) Sandbox — each image's bytes are staged onto
        #       ``deferred_input_files`` under ``in/`` so ``code_execution`` can
        #       read it as a file (drained by the API's augmented provider).
        # Retrieval / proactive / routing keep operating on the plain ``str``
        # ``user_message`` (embeddings + classifiers are text-only).
        user_prompt_content: str | list[MessageContent] = user_message
        if images:
            user_prompt_content = _build_multimodal_user_message(user_message, images)
            self.deferred_input_files.extend(_stage_images_as_input_files(images))

        # Document-workspace cascade: uploaded NON-image documents are staged
        # onto the SAME sandbox bucket as images so ``code_execution`` / a
        # sandbox ``file_read`` reads the ACTUAL uploaded file (not just the
        # ``document_context`` synopsis). The model leg is ``document_context``
        # (text synopsis); this is the sandbox leg only. Bytes are resolved by
        # the caller from the document store into SandboxFile carriers.
        if documents:
            self.deferred_input_files.extend(documents)

        # Spec 35 (D-35-4/D-35-5): collect a per-store recall trace during
        # retrieval so the chat can name each typed store consulted ("Recalling
        # from episodic memory"). Emitted later — only once we commit to
        # composing (past the proactive-question gate) — so a turn that ends in a
        # clarifying question shows no spurious recall state. `on_event is None`
        # (and the voice turn, which never reaches this loop) collects nothing.
        recall_trace: list[tuple[str, int]] = []

        def _note_recall(store: str, count: int) -> None:
            recall_trace.append((store, count))

        # K4 (K4-D-2): publish this turn's recent-conversation window BEFORE retrieval so
        # the graph gate can tell "has the user opened this topic" from the conversation,
        # not the bare query (which would re-close the gate on a follow-up that doesn't
        # re-name the topic — the uncanny-concealment failure). The current message rides
        # as the gate's query; the window is the recent prior turns (text content only).
        set_recent_window_from_messages(conversation.messages)
        context = self._retrieve(
            persona_id,
            user_message,
            history_turns=conversation.turn_count,
            on_recall=(_note_recall if on_event is not None else None),
        )
        history, compacted = await self._manage_history(conversation)

        # Spec A4 (T9b, criterion 8): steer a LIVE task by saying so. Runs first — "cancel that" /
        # "pause it" is about an existing task, not a new one. Pause/resume apply immediately; a
        # cancel asks a consequence-aware confirmation, so a misheard cancel never kills a task.
        if self._steering_interpreter is not None and self._task_reader_provider is not None:
            pending_cancel = _pending_cancel_task_id(conversation)
            if pending_cancel is not None:
                if is_affirmative_confirmation(user_message):
                    if on_event is not None:
                        await on_event(
                            RunEvent.task_steering(
                                verb=SteeringVerb.CANCEL.value, task_id=pending_cancel
                            )
                        )
                    yield _text_chunk(_STEER_CANCELLED_TEXT)
                    now_sc = datetime.now(UTC)
                    conversation.messages.append(
                        ConversationMessage(role="user", content=user_message, created_at=now_sc)
                    )
                    conversation.messages.append(
                        ConversationMessage(
                            role="assistant", content=_STEER_CANCELLED_TEXT, created_at=now_sc
                        )
                    )
                    yield _final_chunk(None)
                    return
                # Not a clean confirm → do NOT cancel; the cancel proposal lapses, fall through.
            elif detect_steering_cue(user_message):
                reader = self._task_reader_provider()
                if reader is not None:
                    from persona.tasks import summarise_task

                    summaries = [summarise_task(t) for t in reader.list_active()]
                    intent = await self._steering_interpreter.interpret(user_message, summaries)
                    if intent is not None and intent.verb is SteeringVerb.CANCEL:
                        goal = next(
                            (s.goal for s in summaries if s.task_id == intent.task_id), "this task"
                        )
                        prompt = f'Just to confirm — cancelling this stops "{goal}". Cancel it?'
                        yield _text_chunk(prompt)
                        now_cp = datetime.now(UTC)
                        conversation.messages.append(
                            ConversationMessage(
                                role="user", content=user_message, created_at=now_cp
                            )
                        )
                        conversation.messages.append(
                            ConversationMessage(
                                role="assistant",
                                content=prompt,
                                created_at=now_cp,
                                metadata={"cancel_proposal": intent.task_id},
                            )
                        )
                        yield _final_chunk(None)
                        return
                    if intent is not None:  # pause / resume — apply immediately, no confirm
                        if on_event is not None:
                            await on_event(
                                RunEvent.task_steering(
                                    verb=intent.verb.value, task_id=intent.task_id
                                )
                            )
                        ack = (
                            _STEER_PAUSED_TEXT
                            if intent.verb is SteeringVerb.PAUSE
                            else _STEER_RESUMED_TEXT
                        )
                        yield _text_chunk(ack)
                        now_st = datetime.now(UTC)
                        conversation.messages.append(
                            ConversationMessage(
                                role="user", content=user_message, created_at=now_st
                            )
                        )
                        conversation.messages.append(
                            ConversationMessage(role="assistant", content=ack, created_at=now_st)
                        )
                        yield _final_chunk(None)
                        return
                # No resolvable steering intent → fall through to the contract gate / chat.

        # Spec A8 (T6): the conversational reschedule verb — a propose→confirm flow over the same
        # steering seam. A pending re-echo + a clean confirm emits ``task_rescheduled``; otherwise a
        # reschedule cue resolves the target (honest — ambiguous lists + asks) and re-echoes the
        # FULL new clause in the user's tz (+ a quiet-hours warn) for confirmation.
        if self._reschedule_interpreter is not None and self._task_reader_provider is not None:
            pending_r = _pending_reschedule_event(conversation)
            if pending_r is not None:
                if is_affirmative_confirmation(user_message):
                    if on_event is not None:
                        await on_event(RunEvent.task_rescheduled(**pending_r))  # type: ignore[arg-type]
                    yield _text_chunk(_RESCHEDULE_APPLIED_TEXT)
                    now_r = datetime.now(UTC)
                    conversation.messages.append(
                        ConversationMessage(role="user", content=user_message, created_at=now_r)
                    )
                    conversation.messages.append(
                        ConversationMessage(
                            role="assistant", content=_RESCHEDULE_APPLIED_TEXT, created_at=now_r
                        )
                    )
                    yield _final_chunk(None)
                    return
                # Not a clean confirm → the reschedule proposal lapses, fall through.
            elif detect_reschedule_cue(user_message):
                reader = self._task_reader_provider()
                if reader is not None:
                    from persona.tasks import summarise_task

                    summaries = [summarise_task(t) for t in reader.list_active()]
                    resolution = await self._reschedule_interpreter.interpret(
                        user_message, summaries
                    )
                    if resolution.kind is RescheduleResolutionKind.AMBIGUOUS:
                        goals = "; ".join(f'"{g}"' for g in resolution.candidate_goals)
                        ask = f"Which one do you mean — {goals}?"
                        yield _text_chunk(ask)
                        now_ra = datetime.now(UTC)
                        conversation.messages.append(
                            ConversationMessage(
                                role="user", content=user_message, created_at=now_ra
                            )
                        )
                        conversation.messages.append(
                            ConversationMessage(role="assistant", content=ask, created_at=now_ra)
                        )
                        yield _final_chunk(None)
                        return
                    if (
                        resolution.kind is RescheduleResolutionKind.RESOLVED
                        and resolution.intent is not None
                    ):
                        tz = self._timezone_provider() if self._timezone_provider else "UTC"
                        quiet = self._quiet_hours_provider() if self._quiet_hours_provider else None
                        goal = next(
                            (s.goal for s in summaries if s.task_id == resolution.intent.task_id),
                            "this task",
                        )
                        now_re = datetime.now(UTC)
                        try:
                            resched_echo = assemble_reschedule_echo(
                                resolution.intent,
                                task_goal=goal,
                                timezone=tz,
                                quiet_hours=quiet,
                                now=now_re,
                            )
                        except ScheduleParseError:
                            yield _text_chunk(_RESCHEDULE_DECLINE_TEXT)
                            conversation.messages.append(
                                ConversationMessage(
                                    role="user", content=user_message, created_at=now_re
                                )
                            )
                            conversation.messages.append(
                                ConversationMessage(
                                    role="assistant",
                                    content=_RESCHEDULE_DECLINE_TEXT,
                                    created_at=now_re,
                                )
                            )
                            yield _final_chunk(None)
                            return
                        import json as _json

                        yield _text_chunk(resched_echo.echo_text)
                        conversation.messages.append(
                            ConversationMessage(
                                role="user", content=user_message, created_at=now_re
                            )
                        )
                        conversation.messages.append(
                            ConversationMessage(
                                role="assistant",
                                content=resched_echo.echo_text,
                                created_at=now_re,
                                metadata={
                                    "reschedule_proposal": _json.dumps(resched_echo.event_data)
                                },
                            )
                        )
                        yield _final_chunk(None)
                        return
                # NOT_FOUND / no reader → fall through to the contract gate / chat.

        # Spec A4 (A4-D-X): the contract flow runs BEFORE the proactive gate — a clean
        # confirm reply to a pending proposal must be read as a confirmation, not re-analysed
        # for ambiguity. The gate is inert unless a recognizer is wired (additive). It owns the
        # turn when it fires (echo / ask-once / confirm) and otherwise falls through unchanged.
        if self._standing_recognizer is not None:
            pending = _pending_contract_draft(conversation)
            if pending is not None:
                if is_affirmative_confirmation(user_message):
                    # The one explicit confirmation → emit the create event (the api worker
                    # injects owner_id + the confirm turn's assistant_message_id; the runtime
                    # loop knows neither). runtime ⊥ api: a data-only event crosses the seam.
                    if on_event is not None:
                        await on_event(
                            build_task_originated_event(
                                draft=pending,
                                owner_id="",  # worker injects the tenant from its handle
                                persona_id=persona_id,
                                persona_name=self._persona.identity.name,
                                conversation_id=conversation.conversation_id,
                                assistant_message_id="",  # worker injects the confirm turn id
                            )
                        )
                    confirmation = _CONTRACT_CONFIRMED_TEXT
                    yield _text_chunk(confirmation)
                    now_c = datetime.now(UTC)
                    conversation.messages.append(
                        ConversationMessage(role="user", content=user_message, created_at=now_c)
                    )
                    conversation.messages.append(
                        ConversationMessage(
                            role="assistant", content=confirmation, created_at=now_c
                        )
                    )
                    yield _final_chunk(None)
                    return
                # Not a clean confirmation — try to interpret it as an amendment (A4-T9): a
                # tweak amends the draft, re-echoes the changed clause, and STAYS pending. Only
                # a clean confirm creates; a confirm-with-a-tweak never silently drops the task.
                if self._amendment_interpreter is not None:
                    amended = await self._amendment_interpreter.interpret(user_message, pending)
                    if amended is not None:
                        amended = canonicalize_draft(amended)
                        reecho = _render_amendment(pending, amended)
                        yield _text_chunk(reecho)
                        now_am = datetime.now(UTC)
                        conversation.messages.append(
                            ConversationMessage(
                                role="user", content=user_message, created_at=now_am
                            )
                        )
                        conversation.messages.append(
                            ConversationMessage(
                                role="assistant",
                                content=reecho,
                                created_at=now_am,
                                metadata={"contract_proposal": amended.model_dump_json()},
                            )
                        )
                        yield _final_chunk(None)
                        return
                # Neither a clean confirmation nor an amendment → abandon the proposal and
                # fall through to ordinary chat.
            else:
                contract = await self._standing_recognizer.recognize(
                    user_message, language=self._persona.identity.language_default
                )
                if contract.kind is RecognitionKind.STANDING and contract.draft is not None:
                    echo = render_echo(contract.draft)
                    yield _text_chunk(echo)
                    now_e = datetime.now(UTC)
                    conversation.messages.append(
                        ConversationMessage(role="user", content=user_message, created_at=now_e)
                    )
                    conversation.messages.append(
                        ConversationMessage(
                            role="assistant",
                            content=echo,
                            created_at=now_e,
                            metadata={"contract_proposal": contract.draft.model_dump_json()},
                        )
                    )
                    yield _final_chunk(None)
                    return
                if contract.kind is RecognitionKind.ASK_ONCE and contract.question is not None:
                    cq = contract.question
                    if on_event is not None:
                        await on_event(
                            RunEvent.asking_user(
                                -1,
                                cq.question,
                                options=cq.options,
                                allow_free_form=cq.allow_free_form,
                            )
                        )
                    yield _text_chunk(cq.question)
                    now_a = datetime.now(UTC)
                    conversation.messages.append(
                        ConversationMessage(role="user", content=user_message, created_at=now_a)
                    )
                    conversation.messages.append(
                        ConversationMessage(
                            role="assistant",
                            content=cq.question,
                            created_at=now_a,
                            metadata={"proactive_question": "true"},
                        )
                    )
                    yield _final_chunk(None)
                    return
                # ORDINARY → fall through to the proactive gate + normal generation.

        # Spec 21 T06 (D-21-1): proactive clarifying question decision point —
        # PRE-generation, before routing (D-05-12 ordering). When a question is
        # asked the turn ends here (the answer arrives as the next user turn);
        # when a signal is suppressed/gated a stated-assumption nudge is seeded
        # into the prompt (D-21-18); when nothing fires the turn is unchanged.
        proactive = await self._decide_proactive_question(conversation, user_message, persona_id)
        if proactive is not None and proactive.question is not None:
            q = proactive.question
            if on_event is not None:
                await on_event(
                    RunEvent.asking_user(
                        -1, q.question, options=q.options, allow_free_form=q.allow_free_form
                    )
                )
            yield _text_chunk(q.question)
            now_q = datetime.now(UTC)
            conversation.messages.append(
                ConversationMessage(role="user", content=user_message, created_at=now_q)
            )
            conversation.messages.append(
                ConversationMessage(
                    role="assistant",
                    content=q.question,
                    created_at=now_q,
                    metadata={"proactive_question": "true"},
                )
            )
            yield _final_chunk(None)
            return
        assumption_seed: list[ConversationMessage] = (
            [self._system_message(proactive.assumption_nudge)]
            if proactive is not None and proactive.assumption_nudge is not None
            else []
        )

        # Spec 35 (D-35-4): now that we are committing to compose (past the
        # proactive-question gate), surface the typed-memory recall trace as
        # ``memory_recall`` SSE frames — one per store consulted, in order —
        # ahead of the tier event and the answer stream. The chat renders the
        # named "Recalling from <store> memory" staged state from these.
        if on_event is not None:
            for store, count in recall_trace:
                await on_event(RunEvent.memory_recall(-1, store, count))

        skill_index = render_skill_index(self._scanned_skills)
        # Spec 18 T12: measure the router's OWN decision latency (distinct
        # from the whole-turn latency_ms) for D-18-X-turnlog-extension.
        routing_started = time.perf_counter()
        decision = self._decide_routing(
            user_message=user_message,
            conversation=conversation,
            turn_has_image=turn_has_image,
        )
        routing_latency_ms = (time.perf_counter() - routing_started) * 1000.0
        tier = decision.tier
        if on_event is not None:
            # Spec 31 (D-31-1): carry a concise model-decision summary on the
            # tier event when intelligent model-within-tier selection ran this
            # turn. Absent for rule-based turns ⇒ the bare-tier payload
            # (back-compat). The full score vector stays on the JSONL TurnLog.
            routing_summary = (
                _routing_event_summary(decision) if self._model_selection_active() else None
            )
            await on_event(RunEvent.tier(tier, routing_summary))
        backend = self._tiers.get(tier)
        # Spec 23 T11 seam (D-23-X-seam-shape): when intelligent routing chose a
        # model within this tier, re-wrap the tier backend so the chosen model is
        # primary (rest preserved as fallback). No-op when the feature is off or
        # the choice equals the current primary — backward-compat (criterion 11).
        if self._intelligent_router is not None and self._persona.routing.intelligent.enabled:
            backend = reorder_primary(backend, decision.model)
        max_tokens = _backend_max_tokens(backend)

        # Mutable per-turn state for the generation sub-loop. ``tool_messages``
        # accumulates the tool-result / system messages appended across rounds;
        # the base prompt is rebuilt each round (to pick up newly injected skill
        # content) and these are appended after it.
        rounds = 0
        skill_used: str | None = None
        matched_skill_content: str | None = None
        # Spec 24 (D-24-4): per-turn skill-composition chain (depth cap + cycle
        # detection + shared budget) replaces the old one-skill-per-turn flag.
        composition = SkillCompositionState(budget=self._injector.TOKEN_BUDGET)
        # Spec 24 (D-24-10): full per-skill activation records for the TurnLog.
        skills_invoked: list[SkillInvocation] = []
        tool_call_count = 0
        # Spec 27 T12: the mcp:<server>:<tool> names dispatched this turn.
        mcp_invocations: list[str] = []
        # Spec 25 T22 (§2.4): ORed across the turn's tool dispatches from each
        # ToolResult.metadata["sandbox_session_recreated"] flag (set by the
        # sandbox tool wrapper's auto-recovery, T09).
        session_recreated = False
        usage: TokenUsage | None = None
        assistant_text = ""
        # Turn-resilience buffer: every text delta yielded to the client this
        # turn is appended here, across ALL rounds. ``assistant_text`` holds only
        # the LAST round's text (the normal write-back: a clean agentic turn ends
        # with the synthesized answer). When that final text is empty/whitespace —
        # a tool-heavy turn that ended on tool calls + an empty forced final
        # generation — we fall back to this buffer at write-back so the persisted
        # message matches what the user actually saw, never a blank bubble.
        visible_text = ""
        # N5-A7 (N5-D-1/D-4): the streaming feeling-tag→emoji converter for the
        # DISPLAY stream. One per turn, spanning all rounds; every model text delta
        # is fed through it so a raw ``{{#…}}`` never reaches the user (criterion 3),
        # including tags split across chunks. ``visible_text`` accumulates the
        # CONVERTED text so the write-back fallback also stores what the user saw.
        display_converter = FeelingTagConverter(ConvertMode.EMOJI)
        # Seeded with the spec-21 stated-assumption nudge when a non-asked signal
        # fired (D-21-18); otherwise empty. Carried into every round's prompt.
        tool_messages: list[ConversationMessage] = list(assumption_seed)
        # Spec 20 T12 (D-20-5): accumulate reasoning text across rounds for
        # the TurnLog content hash; raw text is hashed and DISCARDED at
        # write-back — never persisted.
        reasoning_buffer = ""

        # The R1-soft override directive for this turn (from the single classify
        # above). HARD already returned via the bypass, so only SOFT carries a
        # directive here and NONE is ``None`` — byte-identical, R0 floor intact.
        safety_directive = safety_verdict.soft_directive

        # K6 (K6-D-6): resolve the user's name once per turn (our DB the source of
        # truth) so the persona addresses them by name; and (K6-D-4 addendum) sync
        # the SELF node to that name — the runtime prompt path is its creation
        # trigger. Both provider/sync are fail-soft + ``None`` by default (no name
        # line, no self node) → byte-identical for every un-wired caller.
        user_name = self._user_name_provider() if self._user_name_provider is not None else None
        if self._self_node_sync is not None:
            self._self_node_sync(user_name)

        while True:
            prompt_messages = [
                *self._builder.build(
                    self._persona,
                    context,
                    history,
                    skill_index,
                    user_prompt_content,
                    max_tokens=max_tokens,
                    matched_skill_content=matched_skill_content,
                    document_context=document_context,
                    graph_surfacing_guidance=self._graph_surfacing_guidance,
                    safety_directive=safety_directive,
                    user_name=user_name,
                ),
                *tool_messages,
            ]
            outcome = _RoundOutcome()
            # Signal "the model is generating this round" so the chat surface can
            # show a working indicator during the gap BEFORE any text or tool
            # event — notably while the model writes a long ``code_execution``
            # call, whose arguments don't stream as text deltas (without this the
            # UI looks frozen for the whole generation). Mirrors AgenticLoop's
            # per-step thinking emit; cleared on the round's first chunk/tool event.
            if on_event is not None:
                await on_event(RunEvent.thinking(-1))
            async for delta in self._stream_round(backend, prompt_messages, outcome):
                safe = display_converter.feed(delta)
                visible_text += safe
                if safe:
                    yield _text_chunk(safe)
            round_text, round_calls, round_usage = outcome.text, outcome.calls, outcome.usage
            assistant_text = round_text
            reasoning_buffer += outcome.reasoning_text

            at_cap = rounds >= self._max_tool_rounds
            if round_calls and not at_cap:
                # The round's pre-tool narration (e.g. "Astrid is searching…")
                # already streamed delta-by-delta above (architecture §7.2).
                if round_usage is not None:
                    usage = round_usage
                # Surface the round's tool calls (the chat/run-viewer SSE
                # `tool_calling` event) before dispatching them.
                if on_event is not None:
                    # Spec 30 T01 (D-30-1): badge each call by source.
                    await on_event(
                        RunEvent.tool_calling(-1, round_calls, kind_of=self._toolbox.kind_for)
                    )
                # Native tool-calling providers (OpenAI/DeepSeek/Anthropic) require
                # the assistant message that issued the tool_calls to precede the
                # matching tool results in the re-prompt — otherwise the provider
                # 400s ("'tool' must follow a message with 'tool_calls'"). Spec 11
                # soak finding. Shim providers carry the calls as text, so skip.
                if backend.supports_native_tools:
                    tool_messages.append(
                        ConversationMessage(
                            role="assistant",
                            content=round_text,
                            created_at=datetime.now(UTC),
                            tool_calls=round_calls,
                        )
                    )
                # Dispatch each call; feed results back; intercept use_skill.
                for call in round_calls:
                    result = await self._dispatch(call, on_event=on_event)
                    tool_call_count += 1
                    # Spec 27 T12: record MCP tool invocations for TurnLog telemetry.
                    if call.name.startswith("mcp:"):
                        mcp_invocations.append(call.name)
                    # Spec 25 T22 (§2.4): the sandbox wrapper flags an
                    # auto-recovered session in ToolResult.metadata (str
                    # "True"/"False" per the dict[str,str] convention).
                    if result.metadata.get("sandbox_session_recreated") == "True":
                        session_recreated = True
                    if on_event is not None:
                        await on_event(
                            RunEvent.tool_result(
                                -1, call.name, result, kind=self._toolbox.kind_for(call.name)
                            )
                        )
                    tool_messages.append(
                        format_tool_result(call, result, provider_name=backend.provider_name)
                    )
                    if (
                        call.name == "use_skill"
                        and result.data is not None
                        and "skill_name" in result.data
                        and (name := str(result.data["skill_name"])) in self._skills_by_name
                    ):
                        spec = self._skills_by_name[name]
                        params = result.data.get("parameters")
                        composed = await self._compose_skill(
                            spec,
                            composition,
                            matched_skill_content,
                            params if isinstance(params, dict) else None,
                        )
                        if composed.message is not None:
                            tool_messages.append(self._system_message(composed.message))
                        if composed.content is not None:
                            matched_skill_content = composed.content
                            skill_used = skill_used or name
                        if composed.record is not None:
                            skills_invoked.append(composed.record)
                # One round = one re-prompt-after-dispatch (D-05-11), regardless
                # of how many tool calls this round contained. The counter bounds
                # re-generations, not individual dispatches.
                rounds += 1
                continue

            if round_calls and at_cap:
                # Cap reached with the model still calling tools: nudge + one
                # final tool-free generation (D-05-11). Do not dispatch further.
                tool_messages.append(
                    ConversationMessage(
                        role="system",
                        content=_MAX_TOOL_ROUNDS_NUDGE,
                        created_at=datetime.now(UTC),
                    )
                )
                final_prompt = [
                    *self._builder.build(
                        self._persona,
                        context,
                        history,
                        skill_index,
                        user_message,
                        max_tokens=max_tokens,
                        matched_skill_content=matched_skill_content,
                        document_context=document_context,
                        graph_surfacing_guidance=self._graph_surfacing_guidance,
                    ),
                    *tool_messages,
                ]
                final_outcome = _RoundOutcome()
                async for delta in self._stream_round(backend, final_prompt, final_outcome):
                    safe = display_converter.feed(delta)
                    visible_text += safe
                    if safe:
                        yield _text_chunk(safe)
                assistant_text = final_outcome.text
                round_usage = final_outcome.usage
                reasoning_buffer += final_outcome.reasoning_text

            # Normal completion (or post-cap final text) — the text already
            # streamed delta-by-delta above. The single is_final=True chunk is
            # yielded AFTER write-back so a consumer that stops at is_final still
            # triggers the write (D-05-12).
            if round_usage is not None:
                usage = round_usage
            break

        # Spec 25 T21 (§2.9 RISKY half; default-OFF) — refusal auto-retry.
        # If armed AND the model produced NO tool call this turn AND its text
        # refused an AVAILABLE tool, inject ONE corrective system message and
        # re-generate exactly once. The corrected text streams to the consumer
        # (transparent self-correction); the retry's text replaces
        # ``assistant_text`` for write-back. One retry per turn — never loops.
        refusal_retry_engaged = False
        if self._refusal_retry_enabled and tool_call_count == 0:
            refused = detect_tool_refusals(assistant_text, self._toolbox.names())
            if refused:
                refusal_retry_engaged = True
                now_ts = datetime.now(UTC)
                tool_messages.append(
                    ConversationMessage(role="assistant", content=assistant_text, created_at=now_ts)
                )
                tool_messages.append(
                    ConversationMessage(
                        role="system",
                        content=(
                            f"You DO have the {', '.join(refused)} tool(s) available. "
                            "Do not decline — call the appropriate tool to fulfil the "
                            "user's request."
                        ),
                        created_at=now_ts,
                    )
                )
                retry_prompt = [
                    *self._builder.build(
                        self._persona,
                        context,
                        history,
                        skill_index,
                        user_message,
                        max_tokens=max_tokens,
                        matched_skill_content=matched_skill_content,
                        document_context=document_context,
                        graph_surfacing_guidance=self._graph_surfacing_guidance,
                    ),
                    *tool_messages,
                ]
                retry_outcome = _RoundOutcome()
                async for delta in self._stream_round(backend, retry_prompt, retry_outcome):
                    safe = display_converter.feed(delta)
                    visible_text += safe
                    if safe:
                        yield _text_chunk(safe)
                if retry_outcome.text:
                    assistant_text = retry_outcome.text
                if retry_outcome.usage is not None:
                    usage = retry_outcome.usage
                reasoning_buffer += retry_outcome.reasoning_text
                _logger.info(
                    "refusal-retry engaged: corrected a tool refusal tools={tools} tier={tier}",
                    tools=",".join(refused),
                    tier=tier,
                )

        # N5-A7: flush the display converter — a trailing partial tag left by the
        # last delta is resolved now (unclosed → stripped, literal braces → kept;
        # N5-D-2) and emitted before the final chunk, so nothing is held hostage.
        display_tail = display_converter.flush()
        if display_tail:
            visible_text += display_tail
            yield _text_chunk(display_tail)

        # Turn-resilience fallback (the "vanishes on refresh" bug): a tool-heavy
        # turn that ends on tool calls + an empty forced final generation leaves
        # ``assistant_text`` blank, even though substantial text streamed to the
        # user this turn. Persisting that blank produces an empty assistant row
        # (content=''). When the final round's text is empty/whitespace, fall back
        # to the full visible text the user actually saw; when even that is empty
        # (a turn that produced no text at all — only tool calls), persist a short
        # honest marker rather than a blank bubble. Normal turns are unaffected:
        # ``assistant_text`` is the final synthesized answer and wins.
        if not assistant_text.strip():
            assistant_text = visible_text.strip() or _NO_VISIBLE_TEXT_MARKER
        # N5-A7 (N5-D-4): history stores what the user SAW — convert feeling-tags to
        # emojis in the persisted text. The display stream already converted per
        # delta; this whole-string pass converts ``assistant_text`` (the last round's
        # text, which bypassed the display buffer). Idempotent when the fallback above
        # already supplied the converted ``visible_text``.
        assistant_text = convert_text(assistant_text)

        # Spec 26 T10 (D-26-4): runtime tool-gap detection — AFTER generation
        # (the mirror of Spec 25's refusal detector above). If the model said it
        # can't do something a known-tool-catalog tool would enable AND the
        # persona's allow-list lacks that tool, we offer one-tap consent via a
        # Spec-21 ProactiveQuestion (emitted below, after write-back). The
        # pre-generation Spec 21 hook is untouched. One offer per turn.
        gap_signal = detect_tool_gap(assistant_text, self._toolbox.names())
        tool_gap_detected = [gap_signal.tool_name] if gap_signal is not None else []

        # Spec 27 T11 (D-27-7): runtime MCP-gap detection — the MCP-layer mirror
        # of the tool-gap hook above. Mutually exclusive with it (one offer per
        # turn): only considered when no builtin-tool gap fired. If the model said
        # it can't do something a catalog MCP server would enable AND the persona
        # has no mcp:<server>: tool, we offer one-tap consent (emitted below).
        mcp_gap_signal = (
            detect_mcp_gap(assistant_text, self._toolbox.names()) if gap_signal is None else None
        )
        mcp_unavailable_requested = (
            [mcp_gap_signal.server_name] if mcp_gap_signal is not None else []
        )

        # 7. Post-generation write-back — the LAST step before the final chunk
        #    (D-05-12). An early consumer-exit mid-stream never reaches here
        #    because the generator stays suspended at an earlier yield.
        latency_ms = (time.perf_counter() - started) * 1000.0
        self._write_episodic(persona_id, user_message, assistant_text)
        self._write_turn_log(
            conversation=conversation,
            tier=tier,
            backend=backend,
            usage=usage,
            latency_ms=latency_ms,
            tool_calls=tool_call_count,
            refusal_retry_engaged=refusal_retry_engaged,
            session_recreated=session_recreated,
            skill_used=skill_used,
            skills_invoked=skills_invoked,
            skill_budget_exceeded=composition.budget_exceeded,
            compacted=compacted,
            decision=decision,
            routing_latency_ms=routing_latency_ms,
            reasoning_text=reasoning_buffer,
            assistant_text=assistant_text,
            tool_gap_detected=tool_gap_detected,
            tool_consent_granted=consent_granted_tools or [],
            mcp_invocations=mcp_invocations,
            mcp_unavailable_requested=mcp_unavailable_requested,
        )
        now = datetime.now(UTC)
        # Persist the multimodal content when the turn carried images (so the
        # stored turn matches what the model saw); the API persistence boundary
        # collapses the list back to its text body + the images JSONB column.
        conversation.messages.append(
            ConversationMessage(role="user", content=user_prompt_content, created_at=now)
        )
        conversation.messages.append(
            ConversationMessage(role="assistant", content=assistant_text, created_at=now)
        )

        # Spec 26 T10: surface the tool-gap consent offer as a follow-up to the
        # answer (appended after the assistant's reply so the offer reads as a
        # post-script). The user's pick is applied by the API consent path (T11).
        if gap_signal is not None:
            gap_question = build_tool_gap_question(gap_signal)
            if on_event is not None:
                await on_event(
                    RunEvent.asking_user(
                        -1,
                        gap_question.question,
                        options=gap_question.options,
                        allow_free_form=gap_question.allow_free_form,
                        # Spec 30 (D-30-2): the rail's accept→grant→retry descriptor.
                        proposal=(
                            gap_question.proposal.payload()
                            if gap_question.proposal is not None
                            else None
                        ),
                    )
                )
            yield _text_chunk("\n\n" + gap_question.question)
            conversation.messages.append(
                ConversationMessage(
                    role="assistant",
                    content=gap_question.question,
                    created_at=datetime.now(UTC),
                    metadata={"tool_gap_offer": gap_signal.tool_name},
                )
            )
        # Spec 27 T11: the MCP-gap offer (mutually exclusive with the tool-gap
        # offer above — `mcp_gap_signal` is None whenever a tool gap fired).
        elif mcp_gap_signal is not None:
            mcp_question = build_mcp_gap_question(mcp_gap_signal)
            if on_event is not None:
                await on_event(
                    RunEvent.asking_user(
                        -1,
                        mcp_question.question,
                        options=mcp_question.options,
                        allow_free_form=mcp_question.allow_free_form,
                        # Spec 30 (D-30-2): the MCP-gap accept→grant→retry descriptor.
                        proposal=(
                            mcp_question.proposal.payload()
                            if mcp_question.proposal is not None
                            else None
                        ),
                    )
                )
            yield _text_chunk("\n\n" + mcp_question.question)
            conversation.messages.append(
                ConversationMessage(
                    role="assistant",
                    content=mcp_question.question,
                    created_at=datetime.now(UTC),
                    metadata={"mcp_gap_offer": mcp_gap_signal.server_name},
                )
            )

        # 8. The authoritative end of the turn — yielded last, after write-back.
        yield _final_chunk(usage)

    # ----- internals -------------------------------------------------------

    def _require_persona_id(self) -> str:
        pid = self._persona.persona_id
        if pid is None:
            msg = "persona_id is required for the conversation loop"
            raise ValueError(msg)
        return pid

    # ----- proactive question (spec 21 T06) --------------------------------

    async def _decide_proactive_question(
        self, conversation: Conversation, user_message: str, persona_id: str
    ) -> _ProactiveDecision | None:
        """Decide whether to ask a clarifying question this turn (D-21-1).

        Returns ``None`` when no ambiguity fired (the turn is unchanged). When a
        signal fires: ask it (``question`` set) if the autonomy policy gates it
        in, it is not a per-conversation duplicate (D-21-6), and the per-turn cap
        (1) is free; otherwise return a stated-assumption nudge (D-21-18). The
        ask-rate / suppression telemetry is logged either way (D-21-20).
        """
        ctx = self._detection_context(conversation, self._persona.identity.language_default)
        signal = detect_ambiguity(user_message, ctx)
        if signal is None:
            return None

        level = resolve_autonomy(
            self._persona,
            self._stores["self_facts"].get_all(persona_id, include_superseded=True),
        )
        policy = policy_for(level)
        if not should_ask(signal, policy):
            _logger.info(
                "proactive question suppressed class={cls} pattern={pat} autonomy={lvl}",
                cls=str(signal.signal_class),
                pat=signal.pattern_id,
                lvl=level,
            )
            return _ProactiveDecision(assumption_nudge=self._assumption_nudge(signal))

        question = await self._question_author.author(
            user_message, signal, language=self._persona.identity.language_default
        )
        registry = self._build_question_registry(conversation)
        if registry.seen(question.question):
            # Already asked an equivalent question this conversation (D-21-6) —
            # do not re-ask; proceed (the prior answer is in history).
            _logger.info("proactive question deduped pattern={pat}", pat=signal.pattern_id)
            return None
        _logger.info(
            "proactive question asked class={cls} pattern={pat} autonomy={lvl}",
            cls=str(signal.signal_class),
            pat=signal.pattern_id,
            lvl=level,
        )
        return _ProactiveDecision(question=question)

    @staticmethod
    def _detection_context(conversation: Conversation, language: str) -> DetectionContext:
        """Build the detector context from conversation state (D-21-6 suppressors)."""
        messages = conversation.messages
        prev_was_question = bool(messages) and (
            messages[-1].role == "assistant"
            and messages[-1].metadata.get("proactive_question") == "true"
        )
        return DetectionContext(
            prev_turn_was_question=prev_was_question,
            has_prior_context=bool(messages),
            language=language,
        )

    @staticmethod
    def _build_question_registry(conversation: Conversation) -> QuestionRegistry:
        """Reconstruct the per-conversation dedup registry from tagged turns (D-21-6)."""
        registry = QuestionRegistry()
        for message in conversation.messages:
            if (
                message.role == "assistant"
                and message.metadata.get("proactive_question") == "true"
                and isinstance(message.content, str)
            ):
                registry.record(message.content)
        return registry

    @staticmethod
    def _assumption_nudge(signal: object) -> str:
        """The stated-assumption system instruction for a suppressed signal (D-21-18)."""
        missing = getattr(signal, "missing_element", "detail")
        return (
            "The user's request is somewhat under-specified "
            f"(unclear: {missing}). Make the most reasonable assumption, state that "
            "assumption explicitly in one short sentence at the start of your reply, "
            "then proceed — do not ask a clarifying question."
        )

    @staticmethod
    def _system_message(text: str) -> ConversationMessage:
        return ConversationMessage(role="system", content=text, created_at=datetime.now(UTC))

    def _emit_skill_audit(self, spec: SkillSpec, action: AuditAction, consent_state: str) -> None:
        """Emit exactly one skill AuditEvent (S1-D-7); ``None`` sink ⇒ no-op.

        Best-effort + inert: a failing audit sink must never perturb the
        injection path or the guard, so emission is swallow-and-log.
        """
        if self._audit is None:
            return
        try:
            self._audit.emit(
                skill_audit_event(
                    persona_id=self._persona.persona_id or "",
                    spec=spec,
                    action=action,
                    consent_state=consent_state,
                    timestamp=datetime.now(UTC),
                )
            )
        except Exception:  # noqa: BLE001 — audit must never break the turn
            _logger.warning("skill audit emit failed skill={s}", s=spec.name)

    def _consent_ok(self, spec: SkillSpec) -> bool:
        """Whether ``spec`` may be injected under the enable-time consent gate (S1-D-6).

        ``builtin``/``vetted`` skip the gate; above-threshold skills must be
        consent-enabled at the current ``content_hash``.
        """
        if not spec.trust.requires_consent:
            return True
        content_hash = spec.provenance.content_hash if spec.provenance else ""
        return self._skill_consent.is_enabled(
            self._persona.persona_id or "", spec.name, content_hash or ""
        )

    async def _compose_skill(
        self,
        spec: SkillSpec,
        state: SkillCompositionState,
        current_content: str | None,
        parameters: dict[str, Any] | None,
    ) -> _ComposedSkill:
        """Admit ``spec`` into the composition chain and return what to inject.

        Applies the shared depth/cycle/budget discipline (D-24-4 /
        D-24-X-budget-exhaustion-policy): cycle/depth are refused with an
        informative system message (the turn proceeds); a composed skill that
        would overflow the remaining shared budget is skipped whole (never
        truncated). The first skill goes through the per-skill injector; a
        composed skill (already shown to fit) is appended verbatim.

        Spec S1 (S1-D-6/D-7): the enable-time consent gate runs FIRST — an
        above-threshold skill without granted consent is refused (audited
        ``SKILL_REFUSED``, the turn proceeds) and never touches the composition
        budget. A successful injection is audited ``SKILL_INJECTED``.
        """
        if not self._consent_ok(spec):
            self._emit_skill_audit(spec, AuditAction.SKILL_REFUSED, "refused_no_consent")
            _logger.info("skill activation refused: no consent skill={s}", s=spec.name)
            return _ComposedSkill(
                None,
                f"Skill '{spec.name}' was not activated: it is an untrusted skill that has "
                "not been approved for this persona.",
                None,
            )
        try:
            admission = state.admit(spec.name, content_tokens=spec.content_token_count)
        except (SkillCycleError, SkillCompositionDepthError) as exc:
            _logger.info("skill composition refused reason={r}", r=type(exc).__name__)
            return _ComposedSkill(None, f"Skill '{spec.name}' was not activated: {exc}.", None)
        if admission is AdmissionResult.SKIPPED_BUDGET:
            return _ComposedSkill(
                None,
                f"Skill '{spec.name}' was not activated: the per-turn skill budget is "
                "exhausted. Continue with the instructions already loaded.",
                None,
            )
        content = await self._injector.inject(spec) if state.depth == 1 else spec.content
        injected_tokens = count_tokens(content)
        state.record_injected(injected_tokens)
        self.deferred_input_files.extend(collect_skill_supplements(spec))
        # Spec S1 (S1-D-1/D-2): every skill — depth-1 injector output AND a
        # composed (verbatim) skill — enters the prompt through the subordination
        # guard. The envelope (nonce-delimited, tier-labelled) is applied AFTER
        # budget accounting so the 2000-token cap stays on skill content, not the
        # framing. The one-time authority preamble is emitted once above this
        # region by the prompt builder (PromptBuilder._render_system).
        guarded = subordinate(content, trust=spec.trust, nonce=self._nonce_source())
        self._emit_skill_audit(
            spec, AuditAction.SKILL_INJECTED, injection_consent_state(spec.trust)
        )
        merged = guarded if current_content is None else f"{current_content}\n\n{guarded}"
        record = SkillInvocation(
            name=spec.name, parameters=parameters, content_tokens=injected_tokens
        )
        return _ComposedSkill(merged, None, record)

    def _retrieve(
        self,
        persona_id: str,
        user_message: str,
        *,
        history_turns: int,
        on_recall: Callable[[str, int], None] | None = None,
    ) -> RetrievedContext:
        """Retrieve per-turn context using the real store signatures (§4.1).

        Delegates to the shared :func:`persona_runtime.retrieval.retrieve_context`
        (extracted spec V5 D-V5-6 so the voice turn shares the *same*
        conditioning retrieval — never reimplemented). ``history_turns`` is the
        conversation-progression signal that drives the dynamic per-turn budget
        and recency-augmented episodic recall. ``on_recall`` (Spec 35 D-35-4) is
        forwarded so the chat turn can surface the per-store "thinking /
        remembering" state; ``None`` (the voice turn) keeps retrieval silent.
        """
        return retrieve_context(
            self._stores,
            persona_id,
            user_message,
            history_turns=history_turns,
            on_recall=on_recall,
            graph_retrieval=self._graph_retrieval,
        )

    async def _manage_history(
        self, conversation: Conversation
    ) -> tuple[list[ConversationMessage], bool]:
        """Run history management with the pre-computed-summary bridge (D-05-X).

        Returns the prompt-ready history and whether compaction fired this turn.
        """
        compacted = self._will_compact(conversation)
        precomputed: str | None = None
        if compacted:
            boundary = conversation.turn_count - self._history.keep_recent
            new_range = list(conversation.messages[conversation.compacted_up_to : boundary])
            precomputed = await self._summarise(new_range)

        def _assembler(_messages: list[ConversationMessage]) -> str:
            # manage() only calls this when compacting; precomputed is set then.
            return precomputed or ""

        history = self._history.manage(conversation, summariser=_assembler)
        return history, compacted

    def _will_compact(self, conversation: Conversation) -> bool:
        """Replica of manage()'s 'will I summarise this turn?' decision (D-05-X).

        Cross-checked against the real manager in test_loop.py across K-1/K/K+1.
        """
        turn_count = conversation.turn_count
        if turn_count <= self._history.compact_every:
            return False
        boundary = turn_count - self._history.keep_recent
        return boundary > conversation.compacted_up_to

    async def _summarise(self, messages: list[ConversationMessage]) -> str:
        """Summarise an excerpt on the small tier (the one async summary call)."""
        backend = self._tiers.get("small")
        rendered = "\n".join(f"{m.role}: {m.content}" for m in messages)
        prompt = [
            ConversationMessage(
                role="system", content=_SUMMARISE_INSTRUCTION, created_at=datetime.now(UTC)
            ),
            ConversationMessage(role="user", content=rendered, created_at=datetime.now(UTC)),
        ]
        response = await backend.chat(prompt)
        return response.content.strip()

    def _decide_routing(
        self,
        *,
        user_message: str,
        conversation: Conversation,
        turn_has_image: bool,
    ) -> RoutingDecision:
        """Compute the routing decision for this turn (Spec 18 T06).

        Composition-root responsibility: applies the persona override
        short-circuit (Spec 05 rule 1, preserved per D-18-X-strangler-fig-alias-shape),
        pre-classifies the message signals (boilerplate / identity-sensitive)
        per :mod:`persona_runtime.routing.classifiers`, builds the
        :class:`RoutingContext`, and dispatches to the injected
        :class:`Router` Protocol implementation.

        Args:
            user_message: This turn's user message.
            conversation: The live conversation (for the first-turn signal).
            turn_has_image: Whether this turn carries any
                :class:`~persona.schema.content.ImageContent` — drives Layer 1.

        Returns:
            The :class:`RoutingDecision` carrying the chosen tier + model +
            rationale + observability metadata.
        """
        override = self._persona.routing.tier_for_generation
        if override != "auto":
            # Composition root handles the override — the router never sees
            # this case (Spec 05 rule 1 preserved). Build a minimal decision
            # so the TurnLog records the override path for observability.
            decision = RoutingDecision(
                tier=override,
                model=self._tiers.model_name_for(override),
                rationale=f"persona_override → {override}",
                candidates_considered=(override,),
            )
            # Spec 23 T11: a tier override pins the TIER; intelligent routing may
            # still pick the best MODEL within it (orthogonal). Off-path is a
            # no-op (criterion 11) — the override context is built only when active.
            if self._model_selection_active():
                ctx = self._build_routing_context(user_message, conversation, turn_has_image)
                decision = self._enrich_with_model_selection(decision, ctx)
            return decision

        routing_context = self._build_routing_context(user_message, conversation, turn_has_image)
        decision = self._router.route(routing_context)
        # Spec 23 T11 (D-23-X-seam-shape): enrich the rule-based tier decision
        # with the metadata-driven model choice. No-op when the feature is off —
        # the decision is byte-identical to v0.1 (criterion 11).
        if self._model_selection_active():
            decision = self._enrich_with_model_selection(decision, routing_context)
        return decision

    def _build_routing_context(
        self, user_message: str, conversation: Conversation, turn_has_image: bool
    ) -> RoutingContext:
        """Build the turn's :class:`RoutingContext` (extracted, Spec 18 T06)."""
        return RoutingContext(
            requires_vision=turn_has_image,
            estimated_input_tokens=len(user_message) // 4,  # v0.1 cheap estimate
            requires_strong_tools=False,
            is_first_turn=(conversation.turn_count == 0),
            is_identity_sensitive=classifiers.is_persona_critical(user_message, self._persona),
            is_boilerplate=classifiers.is_boilerplate(user_message),
            conversation_phase="opening" if conversation.turn_count == 0 else "middle",
            profile="text_default",
        )

    @property
    def session_spent_cents(self) -> float:
        """Total estimated cost (cents) accrued this session, incl. the last turn.

        Read-only (CQS): the per-turn cost is folded into the running tally in
        :meth:`_write_turn_log` (after generation), so by the time a caller has
        finished draining :meth:`turn` this reflects the just-completed turn.
        The API reads it at ``done``-build time for the Spec 31 budget indicator
        (D-31-X-session-spend-property) so the meter includes the current turn.
        """
        return self._session_spent_cents

    def budget_snapshot(self) -> dict[str, float] | None:
        """Per-session budget snapshot for the Spec 31 budget indicator (D-31-2).

        ``None`` when intelligent routing is off or no cap is configured (the UI
        shows no indicator). Otherwise carries ``session_spent_cents`` (incl. the
        last turn, read post-``turn``) plus whichever caps are set. ``per_day`` is
        included when configured so the UI can surface 23's fail-loud honestly.
        Read-only (CQS).
        """
        if not self._model_selection_active():
            return None
        budget = self._persona.routing.budget
        caps: dict[str, float | None] = {
            "max_cents_per_turn": budget.max_cents_per_turn,
            "max_cents_per_session": budget.max_cents_per_session,
            "max_cents_per_day": budget.max_cents_per_day,
        }
        if all(value is None for value in caps.values()):
            return None
        snapshot: dict[str, float] = {"session_spent_cents": self._session_spent_cents}
        for key, value in caps.items():
            if value is not None:
                snapshot[key] = value
        return snapshot

    def _model_selection_active(self) -> bool:
        """Whether Spec 23 model-within-tier selection should run this turn."""
        return self._intelligent_router is not None and self._persona.routing.intelligent.enabled

    def _enrich_with_model_selection(
        self, decision: RoutingDecision, context: RoutingContext
    ) -> RoutingDecision:
        """Overlay the IntelligentRouter's model choice onto ``decision`` (Spec 23 T11).

        Raises:
            BudgetExceededError: per-turn hard cap admits no candidate (criterion
                7) — propagates out of the turn (fail-loud).
        """
        if self._intelligent_router is None:  # defensive — callers gate on _model_selection_active
            return decision
        sel = self._intelligent_router.select_model(
            decision.tier,
            context,
            intelligent=self._persona.routing.intelligent,
            budget=self._persona.routing.budget,
            session_spent_cents=self._session_spent_cents,
            # Spec R7 (R7-D-1 discharge): real cross-session per-day spend from
            # ``turn_logs`` via the injected provider; fail-soft to 0.0 (no provider
            # wired ⇒ pre-R7 behaviour). Feeds the SOFT per-day cost-bias ramp only —
            # the HARD per-day guard is R7's credits day-cap (``book_day_spend``).
            day_spent_cents=(
                self._day_spent_cents_provider() if self._day_spent_cents_provider else 0.0
            ),
        )
        return decision.model_copy(
            update={
                "model": sel.model or decision.model,
                "model_candidates": sel.model_candidates,
                "score_vector": sel.score_vector,
                "weights_used": sel.weights_used,
                "model_fallback_engaged": sel.fallback_engaged,
                "model_fallback_reason": sel.fallback_reason,
            }
        )

    async def _stream_round(
        self,
        backend: ChatBackend,
        prompt_messages: list[ConversationMessage],
        outcome: _RoundOutcome,
    ) -> AsyncIterator[str]:
        """Stream one model round, **yielding each text delta as it arrives** so the
        turn streams char-by-char (acceptance §6 #2) — while accumulating the full
        text, reconstructed tool calls, and usage into ``outcome`` for the tool
        sub-loop + episodic write-back (D-05-13).

        Tool-call deltas are accumulated by ``call_id``; args JSON is parsed at
        stream end (malformed → empty dict, fail-safe).

        Spec 18 T06 (D-18-X-first-token-measurement-impl): when a
        :class:`FirstTokenLatencyTracker` is wired into the loop, the FIRST
        non-empty ``chunk.delta`` records ``(perf_counter() - t_start) * 1000``
        as that backend's first-token latency. V5 reads the registry field;
        :class:`UnifiedRouter` reads it for Layer 2 latency scoring.
        """
        names: dict[str, str] = {}
        args_json: dict[str, str] = {}
        order: list[str] = []
        t_start = time.perf_counter()
        first_token_recorded = False
        async for chunk in backend.chat_stream(prompt_messages, tools=self._toolbox.get_specs()):
            if chunk.delta:
                if not first_token_recorded and self._latency_tracker is not None:
                    first_token_ms = (time.perf_counter() - t_start) * 1000.0
                    self._latency_tracker.record(backend.model_name, first_token_ms)
                    first_token_recorded = True
                outcome.text += chunk.delta
                yield chunk.delta
            # Spec 20 T12 (D-20-5): buffer reasoning deltas for the TurnLog
            # content hash. str arm only — list[ReasoningBlock] is collapsed
            # via reasoning_as_text so structured Anthropic blocks contribute
            # plaintext to the hash without round-tripping signatures here
            # (the AnthropicBackend list arm is reachable from the boundary
            # type but not yet emitted by openai_compat — follow-up).
            if chunk.reasoning is not None:
                collapsed = reasoning_as_text(chunk.reasoning)
                if collapsed:
                    outcome.reasoning_text += collapsed
            if chunk.usage is not None:
                outcome.usage = chunk.usage
            delta = chunk.tool_call_delta
            if delta is not None:
                if delta.call_id not in names:
                    order.append(delta.call_id)
                    names[delta.call_id] = ""
                    args_json[delta.call_id] = ""
                names[delta.call_id] += delta.name_delta
                args_json[delta.call_id] += delta.arguments_delta
        outcome.calls = [self._build_call(cid, names[cid], args_json[cid]) for cid in order]

    async def _dispatch(
        self,
        call: ToolCall,
        *,
        on_event: Callable[[RunEvent], Awaitable[None]] | None = None,
    ) -> ToolResult:
        """Dispatch a tool call, converting structural failures to is_error results.

        A hallucinated, empty, or not-allowed tool name raises ``ToolNotAllowedError``
        (spec 03, D-03-8); a registered tool that blows up raises
        ``ToolExecutionError``. Both are caught and turned into
        ``ToolResult(is_error=True, ...)`` so the model can self-correct on the
        next round instead of the error escaping the turn generator and crashing
        the SSE mid-stream ("response already started"). Mirrors the agentic
        loop's ``_dispatch`` (one tool-failure discipline across both loops).
        A tool that runs but fails already returns ``is_error=True`` unchanged.

        P2: dispatch routes through :func:`dispatch_with_activity` so each call emits a
        paired ``activity_start``/``activity_end`` when ``on_event`` is present (the live
        "using <X>…" state + trail). Chat turns are run-level, so ``step=-1`` (mirrors the
        ``tool_result`` emission below). The existing ``tool_result`` keeps emitting
        additively during the migration (P2-D-3 keep-both).
        """
        from persona.errors import ToolExecutionError, ToolNotAllowedError

        # The provider truncated the call mid-JSON (finish_reason="length" or
        # unparseable arguments). Dispatching with empty args yields the cryptic
        # "Field required" and an identical-retry loop; return actionable
        # guidance so the model shortens/splits instead.
        if call.truncated:
            return ToolResult(
                tool_name=call.name,
                call_id=call.call_id,
                is_error=True,
                content=truncated_tool_call_message(call.name),
            )

        try:
            return await dispatch_with_activity(self._toolbox, call, on_event=on_event, step=-1)
        except ToolNotAllowedError:
            available = ", ".join(self._toolbox.names())
            return ToolResult(
                tool_name=call.name,
                call_id=call.call_id,
                is_error=True,
                content=f"Tool '{call.name}' is not available. Available tools: {available}",
            )
        except ToolExecutionError as exc:
            return ToolResult(
                tool_name=call.name,
                call_id=call.call_id,
                is_error=True,
                content=f"Tool '{call.name}' failed: {exc}",
            )

    @staticmethod
    def _build_call(call_id: str, name: str, raw_args: str) -> ToolCall:
        truncated = False
        try:
            args = json.loads(raw_args) if raw_args else {}
        except json.JSONDecodeError:
            # Streamed ``arguments`` fragments never assembled into valid JSON —
            # the provider cut the response off mid-payload (D-05-13). Do NOT
            # dispatch empty args; mark truncated so ``_dispatch`` returns
            # actionable "shorten it" guidance instead of the cryptic
            # "Field required" that triggers an identical-retry loop.
            args = {}
            truncated = True
        if not isinstance(args, dict):
            args = {}
            truncated = True
        return ToolCall(name=name, args=args, call_id=call_id, truncated=truncated)

    def _write_episodic(self, persona_id: str, user_text: str, assistant_text: str) -> None:
        """Write one combined episodic chunk per turn (D-05-12; mirrors the CLI)."""
        store = self._stores["episodic"]
        index = len(store.get_all(persona_id, include_superseded=True))
        chunk_id = make_chunk_id(persona_id, "episodic", index)
        now = datetime.now(UTC)
        store.write(
            persona_id,
            [
                PersonaChunk(
                    id=chunk_id,
                    text=f"USER: {user_text}\nASSISTANT: {assistant_text}",
                    metadata={"importance": "0.5"},
                    created_at=now,
                    provenance=ChunkProvenance(
                        source=WriteSource.SYSTEM,
                        logical_id=chunk_id,
                        version=1,
                        written_at=now,
                        written_by="runtime.loop",
                    ),
                ),
            ],
            source=WriteSource.SYSTEM,
            written_by="runtime.loop",
        )

    def _write_turn_log(
        self,
        *,
        conversation: Conversation,
        tier: str,
        backend: ChatBackend,
        usage: TokenUsage | None,
        latency_ms: float,
        tool_calls: int,
        skill_used: str | None,
        skills_invoked: list[SkillInvocation],
        skill_budget_exceeded: bool,
        compacted: bool,
        decision: RoutingDecision,
        routing_latency_ms: float,
        reasoning_text: str = "",
        assistant_text: str = "",
        refusal_retry_engaged: bool = False,
        session_recreated: bool = False,
        tool_gap_detected: list[str] | None = None,
        tool_consent_granted: list[str] | None = None,
        mcp_invocations: list[str] | None = None,
        mcp_unavailable_requested: list[str] | None = None,
    ) -> None:
        prompt_tokens = usage.prompt_tokens if usage is not None else 0
        completion_tokens = usage.completion_tokens if usage is not None else 0
        cost = estimate_cost_cents(
            backend.provider_name, backend.model_name, prompt_tokens, completion_tokens
        )
        # Spec 23 T11 (D-23-7): accumulate this turn's cost into the loop-owned
        # per-session tally that feeds the soft per-session budget ramp on the
        # NEXT turn. Loop-owned, never on the stateless router/backend.
        self._session_spent_cents += cost
        # Spec 25 T13 (§2.6 / D-25-7): surface how ``cost`` was derived so
        # operators can tell provider-listed rates from verify-at-deploy
        # shadow-price estimates (e.g. NVIDIA).
        cost_basis = cost_basis_for(backend.provider_name, backend.model_name)
        # Spec 25 T11 (§2.9): observability-only refusal detection — flag any
        # AVAILABLE tool the assistant text refused to use. No correction here.
        tool_refusal_detected = detect_tool_refusals(assistant_text, self._toolbox.names())
        # Spec 20 T12 (D-20-5): content-hash-only — hash the raw reasoning
        # text and DISCARD it. Token-count approximation from whitespace
        # split is a coarse v0.1 estimate (providers don't surface separate
        # reasoning_tokens on the str arm); refine once
        # ``CompletionUsage.completion_tokens_details`` lands per provider.
        reasoning_text_hash: str | None = None
        reasoning_total_tokens: int | None = None
        if reasoning_text:
            reasoning_text_hash = hashlib.sha256(reasoning_text.encode("utf-8")).hexdigest()
            reasoning_total_tokens = len(reasoning_text.split())
        # Spec 20 T19 (D-20-9): populate the multi-model fallback fields.
        # The wrapper's per-call attempt ledger lives on ``last_attempts``;
        # single-backend (non-wrapper) callers safely return an empty list
        # via ``getattr(..., None) or []``, yielding the zero-fallback
        # default shape (backward compat).
        fallback_kwargs = _compute_fallback_fields(backend)
        # Spec 25 T12 (§2.1 / D-25-5/6): feed this turn's fallback signal into
        # the rolling window and resolve the alert state (hysteresis + logging
        # handled in the helper).
        fallback_rate_alert = self._update_fallback_window(
            engaged=fallback_kwargs["fallback_engaged"],
            conversation_id=conversation.conversation_id,
            provider=backend.provider_name,
            model=backend.model_name,
        )
        self._turn_log_writer.write(
            TurnLog(
                conversation_id=conversation.conversation_id,
                turn_index=conversation.turn_count,
                tier_used=tier,
                model_name=backend.model_name,
                provider=backend.provider_name,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                cost_cents=cost,
                cost_basis=cost_basis,
                tool_calls=tool_calls,
                skill_used=skill_used,
                skills_invoked=skills_invoked,
                skill_budget_exceeded=skill_budget_exceeded,
                history_compacted=compacted,
                timestamp=datetime.now(UTC),
                routing_decision=decision,
                routing_latency_ms=routing_latency_ms,
                routing_fallback_triggered=decision.fallback_triggered,
                routing_fallback_reason=decision.fallback_reason,
                reasoning_total_tokens=reasoning_total_tokens,
                reasoning_text_hash=reasoning_text_hash,
                tool_refusal_detected=tool_refusal_detected,
                fallback_rate_alert=fallback_rate_alert,
                refusal_retry_engaged=refusal_retry_engaged,
                sandbox_session_recreated=session_recreated,
                tool_gap_detected=tool_gap_detected or [],
                tool_consent_granted=tool_consent_granted or [],
                mcp_invocations=mcp_invocations or [],
                mcp_unavailable_requested=mcp_unavailable_requested or [],
                **fallback_kwargs,
            )
        )

    def _update_fallback_window(
        self,
        *,
        engaged: bool,
        conversation_id: str,
        provider: str,
        model: str,
    ) -> bool:
        """Push this turn's fallback signal and resolve the alert state.

        Spec 25 §2.1 / D-25-5 / D-25-6. Count-based rolling window (turns
        arrive irregularly, so a time-window false-pages in low-traffic
        regimes per the SRE precedent). Strict ``>30%`` entry threshold
        (``≥4/10`` on a full window) with a min-sample guard (``≥5`` turns AND
        ``≥2`` fallbacks, so 1-in-2 noise can't trip it); hysteresis clears at
        ``≤20%`` (``≤2/10``). Edge-triggered: exactly one ERROR per breach
        episode at the OK→ALERTING edge, one INFO at recovery. Returns the
        current ALERTING state for the TurnLog ``fallback_rate_alert`` field.
        """
        self._fallback_window.append(engaged)
        n = len(self._fallback_window)
        count = sum(self._fallback_window)
        rate = count / n if n else 0.0
        if not self._fallback_alerting:
            if n >= 5 and count >= 2 and rate > 0.30:
                self._fallback_alerting = True
                _logger.error(
                    "multi_model fallback-rate alert: {count}/{n} turns "
                    "({rate:.0%}) used fallback (>30% over rolling window); "
                    "primary provider={provider} model={model} conversation={conv}",
                    count=count,
                    n=n,
                    rate=rate,
                    provider=provider,
                    model=model,
                    conv=conversation_id,
                )
        elif rate <= 0.20:
            self._fallback_alerting = False
            _logger.info(
                "multi_model fallback-rate recovered: {count}/{n} turns "
                "({rate:.0%}) ≤20%; conversation={conv}",
                count=count,
                n=n,
                rate=rate,
                conv=conversation_id,
            )
        return self._fallback_alerting


class _FallbackFields(TypedDict):
    """Typed shape of the T19 fallback-instrumentation projection.

    Matches the six additive ``tier_*`` / ``fallback_engaged`` fields on
    :class:`TurnLog` so the helper's return value type-checks at the
    spread-into-constructor call site under ``mypy --strict``.
    """

    tier_model_chosen: str | None
    tier_provider_used: str | None
    tier_fallback_count: int
    tier_fallback_reasons: list[str]
    tier_fallback_providers: list[str]
    fallback_engaged: bool


def _compute_fallback_fields(backend: ChatBackend) -> _FallbackFields:
    """Project ``MultiModelChatBackend.last_attempts`` into TurnLog T19 fields.

    Per D-20-9 privacy: only class names reach the log (NEVER error
    messages or context dict values). Per D-20-7 the operator-dashboard
    fallback-rate trigger consumes ``tier_fallback_count`` /
    ``fallback_engaged`` directly.

    Single-backend (non-wrapper) callers — backends that do not expose a
    ``last_attempts`` accessor — get the zero-fallback default shape
    (``tier_model_chosen`` + ``tier_provider_used`` carry the actually-used
    primary backend; counts are zero; lists are empty). This is the
    backward-compat path for legacy callers and the degenerate length-1
    chain that ``TierRegistry`` short-circuits past the wrapper entirely
    (D-20-17 case (a)).

    For :class:`MultiModelChatBackend` callers, the winner is the backend
    at index ``len(last_attempts)`` in the ``backends`` list: every prior
    backend fell through (one ``AttemptRecord`` each); the next one
    served the turn. The wrapper's own ``provider_name`` / ``model_name``
    properties report the PRIMARY backend's identity (not the active
    one), so we must reach into ``backends[len(attempts)]`` to record the
    *actually-used* model on the TurnLog.

    The ``AllModelsFailedError`` exhaustion case never reaches this
    function — the loop's caller catches the error before write-back —
    but we defensively guard against an over-length attempt ledger by
    falling back to the wrapper's reported identity.
    """
    attempts = getattr(backend, "last_attempts", None) or []
    fallback_reasons: list[str] = [a.last_error_class for a in attempts]
    fallback_providers: list[str] = [a.provider for a in attempts]
    count = len(fallback_reasons)
    # Resolve the actually-used backend's identity when this is the
    # multi-model wrapper. ``getattr(..., "backends", None)`` returns the
    # ordered chain on the wrapper, ``None`` on bare backends.
    wrapper_backends = getattr(backend, "backends", None)
    if wrapper_backends is not None and 0 <= count < len(wrapper_backends):
        winner = wrapper_backends[count]
        chosen_model: str | None = winner.model_name
        chosen_provider: str | None = winner.provider_name
    else:
        chosen_model = backend.model_name
        chosen_provider = backend.provider_name
    return _FallbackFields(
        tier_model_chosen=chosen_model,
        tier_provider_used=chosen_provider,
        tier_fallback_count=count,
        tier_fallback_reasons=fallback_reasons,
        tier_fallback_providers=fallback_providers,
        fallback_engaged=count > 0,
    )
