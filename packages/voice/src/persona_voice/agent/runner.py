"""The voice agent-worker — the composition-root runner (spec V6 A0).

V1–V5 built every voice component + seam, but never the deployable process that
**is the persona on a call**: ``wire_orchestrated_loop`` / ``build_voice_room``
were referenced only by tests (fakes at the transport boundary), and the V5
operator pass explicitly DEFER-NOTE'd the WebRTC mic→speaker leg "to the live-
audio leg" — i.e. to V6. This module is that missing glue: it assembles the
already-built collaborators into a running process that joins a real LiveKit
Room, runs the streaming loop, and tears down on disconnect.

**Scope (D-V6-X-agent-worker, binding).** This is the **minimal, single-session,
dev/operator-pass-grade** runner — enough to make one real browser call work
against the dev LiveKit sidecar with real keys. Production worker-ops (a
multi-session supervisor, autoscaling, room-agent dispatch) and the prod worker
deploy are explicit forward-items, the same tier as the prod-LiveKit-deploy
forward-item. This module spawns no pool, matches no dispatch, scales nothing —
one call, one session, one ``run()``.

**Layering.** persona-voice → persona-runtime → persona-core. This runner imports
``persona.*`` + ``persona_runtime.*`` directly (the V5 workspace edge) and never
``persona_api.*`` — the persona is loaded from the DB with a raw RLS-scoped
``SELECT`` (the persona-api ``RuntimeFactory._load_persona`` shape, reproduced
here so no api dependency is taken).

**The testable seam.** :func:`build_agent_session` does the heavy real assembly
(STT/TTS/model/stores/room); :class:`AgentSession` owns the connect→run→teardown
lifecycle. The lifecycle is unit-tested with fully-faked parts (so STT/TTS/DB
internals — already covered by V2–V5 — are not re-exercised); the real assembly
is exercised live by the V6 operator pass (D2).
"""

from __future__ import annotations

import asyncio
import contextlib
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, cast

from livekit import api
from persona.audit import JSONLAuditLogger
from persona.billing import BillingConfig, CoreCreditsLedger
from persona.config import PersonaCoreConfig
from persona.errors import PersonaNotFoundError
from persona.history import ConversationHistoryManager
from persona.logging import get_logger
from persona.schema.conversation import Conversation
from persona.schema.persona import Persona
from persona.stores import (
    EpisodicStore,
    IdentityStore,
    SelfFactsStore,
    WorldviewStore,
)
from persona.stores.postgres import PostgresBackend
from persona.tools import build_default_toolbox
from persona_runtime.prompt import PromptBuilder
from persona_runtime.routing import FirstTokenLatencyTracker, PolicyRouter
from persona_runtime.tier import tier_registry_from_env
from sqlalchemy import text

from persona_voice.agent.language import (
    maybe_apply_stt_route,
    maybe_apply_tts_route,
    resolve_call_languages,
    tts_is_utterance_level,
)
from persona_voice.agent.warmup import start_embedder_warmup
from persona_voice.billing import VoiceExhaustionCutoff, VoiceTurnBillingMeter
from persona_voice.billing.topup_enqueue import enqueue_auto_topup
from persona_voice.model import (
    AsyncArtifactLane,
    VoiceHistoryCompactor,
    VoiceModelReplyProducer,
    VoiceToolPolicy,
    VoiceTurnContext,
    VoiceTurnRecorder,
    make_small_tier_summariser,
)
from persona_voice.model.expressivity import VoiceExpressivityChannel
from persona_voice.model.origination_gate import (
    DelegatedTurnIntent,
    GateCommitListener,
    VoiceOriginationGate,
)
from persona_voice.model.transcript import VoiceTranscriptWriter
from persona_voice.session.call_record import CallRecorder, EndReason
from persona_voice.session.delegation_dispatch import DelegationDispatcher
from persona_voice.session.delegation_handback import DelegationHandbackPoller
from persona_voice.session.state_machine import SessionStateMachine, make_session_rls_engine
from persona_voice.stt import StreamingSTTConfig, load_streaming_stt
from persona_voice.stt.cost_gate import DEFAULT_REOPEN_PREROLL_MS, IdleAwareGate
from persona_voice.stt.seam_adapter import V1STTStreamSeamAdapter
from persona_voice.stt.vad_silero import SileroVADAdapter
from persona_voice.tokens.issuer import mint_room_access_token
from persona_voice.transport.broadcast import DataChannelBroadcaster
from persona_voice.transport.room import VoiceRoom, build_voice_room
from persona_voice.tts._factory import load_streaming_tts
from persona_voice.tts.config import StreamingTTSConfig
from persona_voice.tts.seam_adapter import build_seam_adapter
from persona_voice.turn_taking.bridge import CompositeTurnTranscriptListener

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Any

    from persona.schema.tools import ToolCall
    from persona.stores.embedder import Embedder
    from persona.stores.protocol import MemoryStore
    from persona.tools.mcp.client import MCPClient
    from persona_runtime.agentic.events import RunEvent
    from persona_runtime.crisis_encoder import CrisisScorer
    from persona_runtime.prompt import GraphContext, GraphRecency
    from persona_runtime.tier import TierRegistry
    from persona_runtime.unified_recall import UnifiedProjection
    from sqlalchemy import Engine

    from persona_voice.config import VoiceConfig
    from persona_voice.loop.streaming import StreamingLoop, Transcript
    from persona_voice.turn_taking.heard_words import TurnTranscriptListener

__all__ = [
    "AgentSession",
    "build_agent_session",
    "run_agent_session",
]

_logger = get_logger("agent.runner")

# A ``state_listener_factory`` that, given the session's VoiceRoom, returns the
# V6 state-broadcast listener (A1 ``DataChannelBroadcastListener``) is threaded
# through optionally — A0 is testable + runnable on its own before A1 lands, and
# A1 injects the real broadcast over the room's data channel. None ⇒ no state
# broadcast (the loop still runs).

_BGE_MODEL = "BAAI/bge-small-en-v1.5"

# The synthetic turn-0 prompt (Spec 32 A3). It rides the normal producer path as
# the opening "user" message; the persona's reply is the greeting, generated in
# the declared language (B5). The persona answers the phone — no user input.
#
# R9-002: the nudge is memory-aware. The K9 core-memory block (read ONCE at
# session setup — ``_load_core_block``) rides the assembled prompt, so the model
# MAY open with genuine continuity — but only through the guarded branch below:
# the model decides from what it sees in context; the code only decides what
# reaches the context. No history in context ⇒ the plain-hello branch (today's
# behaviour). The instruction-level sensitive-topic guard is defence in depth on
# top of the K4-composed upstream content: an UNPROMPTED first utterance must
# never raise a heavy topic.
_GREETING_NUDGE = (
    "(The voice call has just connected. Greet the person warmly in one or two "
    "short sentences to open the conversation, in character, and do not wait for "
    "them to speak first. If your context includes shared history with this "
    "person, greet them as a returning acquaintance and you may touch on at most "
    "one recent, light, neutral open thread from it. Never raise sensitive "
    "topics - health, crisis, relationships, finances, or legal matters - "
    "unprompted; when in doubt, keep it to a plain warm hello. If you have no "
    "shared history in your context, simply give one short warm hello.)"
)


def _greeting_transcript() -> Transcript:
    """The synthetic turn-0 transcript the greeting kickoff feeds the producer.

    Marked ``synthetic=True`` (R9-001): the nudge is an internal instruction, not
    the caller's speech, so the persistence boundary (``VoiceTurnRecorder``) keeps
    it out of the user-facing transcript — only the persona's greeting reply
    persists. The flag is structural (carried on the Transcript itself), so the
    nudge wording can change freely without regressing the suppression.
    """
    from persona_voice.loop.streaming import Transcript  # deferred, like the session build

    return Transcript(is_final=True, text=_GREETING_NUDGE, confidence=1.0, synthetic=True)


def _load_persona(engine: Engine, persona_id: str) -> Persona:
    """Load + validate the persona's YAML from the RLS-scoped ``personas`` row.

    Reproduces ``persona_api.services.runtime_factory.RuntimeFactory._load_persona``
    against the session RLS engine — a raw ``SELECT`` keeps persona-voice free of
    a persona-api dependency (the layering line). The engine is RLS-scoped to the
    call's owner, so a persona the caller does not own is invisible (→ not found).
    """
    import yaml

    with engine.begin() as conn:
        row = (
            conn.execute(text("SELECT yaml FROM personas WHERE id = :pid"), {"pid": persona_id})
            .mappings()
            .first()
        )
    if row is None:
        raise PersonaNotFoundError("persona not found", context={"id": persona_id})
    raw = yaml.safe_load(str(row["yaml"]))
    if isinstance(raw, dict):
        raw["persona_id"] = persona_id
    return Persona.model_validate(raw)


def _load_user_name(engine: Engine, user_id: str) -> str | None:
    """Resolve the caller's display name from OUR ``users`` table (Spec K6, K6-D-6).

    A raw ``SELECT`` (keeps persona-voice free of a persona-api dependency, the
    layering line — mirrors :func:`_load_persona`), resolved ONCE here at session
    setup — off the per-utterance path, so no DB read ever runs on the realtime
    turn loop. Fail-soft: no row, no name, or any error ⇒ ``None`` ⇒ the voice
    prompt omits the name line (byte-identical, null-safe). The name is a nicety
    and must never break a call. ``users`` is not RLS-scoped, but the read is by
    the caller's own id, so it returns only the caller's own name.
    """
    try:
        with engine.begin() as conn:
            row = (
                conn.execute(
                    text("SELECT first_name, last_name FROM users WHERE id = :uid"),
                    {"uid": user_id},
                )
                .mappings()
                .first()
            )
    except Exception:  # noqa: BLE001 — the name is a nicety; never break a call
        _logger.opt(exception=True).warning("voice user-name resolution failed (non-fatal)")
        return None
    if row is None:
        return None
    parts = [p for p in (row["first_name"], row["last_name"]) if p]
    return " ".join(parts) or None


def _load_plan_code(engine: Engine, user_id: str) -> str:
    """The caller's subscription ``plan_code`` (Spec M4, T5c) — raw SELECT, fail-safe to 'free'.

    A raw ``SELECT`` on the ``subscription`` table (keeps persona-voice free of a persona-api
    dependency — the layering line, mirrors :func:`_load_user_name`), resolved ONCE at session
    setup (off the per-utterance path). Fail-SAFE: no row / any error ⇒ ``'free'`` (the
    restrictive set), so a lookup miss can never open the paid tiers to a free caller.
    """
    try:
        with engine.begin() as conn:
            row = conn.execute(
                text("SELECT plan_code FROM subscription WHERE user_id = :uid"),
                {"uid": user_id},
            ).first()
    except Exception:  # noqa: BLE001 — a plan read must never break a call; default free (safe)
        _logger.opt(exception=True).warning("voice: plan read failed; defaulting free (fail-safe)")
        return "free"
    if row is not None and row[0]:
        return str(row[0])
    return "free"


def _select_voice_tier_registry(
    *,
    config: VoiceConfig,
    tier_registry: TierRegistry,
    free_tier_registry: TierRegistry | None,
    rls_engine: Engine,
    user_id: str,
) -> TierRegistry:
    """Plan-select the voice tier registry (Spec M4, T5c — the chat ``_plan_tier_selection`` twin).

    A FREE caller (cloud + ``subscription.plan_code == 'free'``) resolves the FREE-ONLY
    registry: voice wires no ``preferred_backend_provider``, so swapping the registry makes the
    WHOLE voice LLM chain (generation ``mid`` + summariser/gate ``small``) free-only — no paid
    fallback anywhere in the walk. Community (``free_tier_registry`` None) / paid → the injected
    paid registry, byte-identical. Fail-closed: an empty free registry raises when a tier is
    got (the call fails rather than reaching a paid model).
    """
    if free_tier_registry is None or not config.is_cloud:
        return tier_registry  # gating off (community / not configured) → paid tiers, unchanged
    if _load_plan_code(rls_engine, user_id) == "free":
        return free_tier_registry
    return tier_registry  # paid plan → full paid tiers


def _load_core_block(
    engine: Engine, embedder: Embedder, persona_id: str, audit_root: Path
) -> str | None:
    """Read the persona's K9 core-memory block ONCE at session setup (R9-002).

    The greeting head start: the compact user+persona summary (K9-D-10, background-
    refreshed — never built here) is read at session setup, alongside
    :func:`_load_persona` / :func:`_load_user_name` — the established off-turn-loop
    pattern, so NO fetch ever runs on the connect→first-word path (the provider
    replays this one read for the whole call). One cheap RLS-scoped SELECT
    (``CoreMemoryStore.current`` — no model call, no embedding); gated by the SAME
    ``RecallSettings.core_enabled`` the chat path's provider uses (parity, never a
    voice fork). Fail-soft: gate off, no block, or any error ⇒ ``None`` ⇒ no head
    start ⇒ the plain-hello branch — the head start is a nicety and must never
    break a call.
    """
    from persona.recall.config import RecallSettings
    from persona.recall.core_memory import read_core_block
    from persona.stores.core_memory import CoreMemoryStore

    try:
        if not RecallSettings().core_enabled:
            return None
        store = CoreMemoryStore(
            backend=PostgresBackend(engine=engine, embedder=embedder),
            audit_logger=JSONLAuditLogger(audit_root),
        )
        block = read_core_block(store, persona_id)
    except Exception:  # noqa: BLE001 — the head start is a nicety; never break a call
        _logger.opt(exception=True).warning("voice core-block read failed (non-fatal)")
        return None
    return block.text if block is not None else None


def _build_stores(engine: Engine, embedder: Embedder, audit_root: Path) -> dict[str, MemoryStore]:
    """The four typed stores over ``PostgresBackend`` (RLS-scoped session engine).

    Identical shape to the persona-api ``RuntimeFactory._build_stores`` so voice
    memory is the **same** unified episodic store the text path writes — a voice
    turn's memory is recalled in text and vice versa.
    """
    backend = PostgresBackend(engine=engine, embedder=embedder)
    audit = JSONLAuditLogger(audit_root)
    return {
        "identity": IdentityStore(backend=backend, audit_logger=audit),
        "self_facts": SelfFactsStore(backend=backend, audit_logger=audit),
        "worldview": WorldviewStore(backend=backend, audit_logger=audit),
        "episodic": EpisodicStore(backend=backend, audit_logger=audit),
    }


def _build_origination_gate(
    config: VoiceConfig, persona: Persona, tier_registry: TierRegistry
) -> VoiceOriginationGate | None:
    """Compose the A9 origination gate when delegation is enabled (A9-D-1), else ``None``.

    Reuses A4's grammar VERBATIM: the two-step :class:`StandingIntentRecognizer` (the cheap cue
    net gates the small-tier :class:`ModelStandingIntentJudge`) + the ``ModelAmendmentInterpreter``,
    over the ``small`` tier (boilerplate work — the same tier the text origination path uses; the
    judge runs off the live path behind the cue gate, so it never starves the loop). ``None``
    ⇒ a byte-identical voice turn (the gate is never consulted). The gate holds no DB/graph handle —
    execution is the chat pipeline's (delegated off this loop).
    """
    if not config.delegation_enabled:
        return None
    from persona_runtime.task_origination import (
        ModelAmendmentInterpreter,
        ModelStandingIntentJudge,
        StandingIntentRecognizer,
    )

    backend = tier_registry.get("small")
    recognizer = StandingIntentRecognizer(ModelStandingIntentJudge(backend=backend))
    return VoiceOriginationGate(
        recognizer=recognizer,
        amendment_interpreter=ModelAmendmentInterpreter(backend=backend),
        language=persona.identity.language_default,
    )


class AgentSession:
    """One voice call's running session — connect, run the loop, tear down.

    Holds the assembled collaborators and owns ONLY the lifecycle (the heavy
    assembly lives in :func:`build_agent_session`). :meth:`run` connects the
    :class:`VoiceRoom` to the LiveKit Room with the agent token, marks the
    session active, starts the streaming pipeline, waits until the user
    disconnects (the room ``disconnected`` event ends the session + sets
    :attr:`_ended`), then tears every resource down in a ``finally`` so a crash
    mid-call still releases the mic track, the RLS engine, and the MCP clients.
    """

    def __init__(
        self,
        *,
        voice_room: VoiceRoom,
        loop: StreamingLoop,
        stt_seam: V1STTStreamSeamAdapter,
        tts_seam: object,
        session: SessionStateMachine,
        mcp_clients: list[MCPClient],
        livekit_url: str,
        agent_token: str,
        ended: asyncio.Event,
        embedder_warmup: asyncio.Task[None] | None = None,
        greet: Callable[[], Coroutine[Any, Any, None]] | None = None,
        call_recorder: CallRecorder | None = None,
        call_record_engine: Engine | None = None,
        async_lane: AsyncArtifactLane | None = None,
        handback_poller: DelegationHandbackPoller | None = None,
        delegation_dispatcher: DelegationDispatcher | None = None,
        on_call_complete: Callable[[], None] | None = None,
        turn_billing_meter: VoiceTurnBillingMeter | None = None,
    ) -> None:
        self._voice_room = voice_room
        self._loop = loop
        self._stt_seam = stt_seam
        self._tts_seam = tts_seam
        self._session = session
        self._mcp_clients = mcp_clients
        # V10 (T4): the off-turn async-artifact production lane — cancelled at
        # teardown so no production task outlives the call (V10-D-4/5).
        self._async_lane = async_lane
        # A9 (T6): the delegation hand-back poller — cancelled at teardown so no poll
        # task outlives the call (the durable result self-heals — nothing is lost).
        self._handback_poller = handback_poller
        # A9 (T10): the delegation dispatcher — cancelled at teardown so no in-flight enqueue
        # outlives the call.
        self._delegation_dispatcher = delegation_dispatcher
        self._livekit_url = livekit_url
        self._agent_token = agent_token
        self._ended = ended
        # V9 (V9-D-5): the durable call-record writer + its DEDICATED RLS engine.
        # The recorder's engine is SEPARATE from the session engine on purpose —
        # ``session.end()`` (fired by the clean-hangup room-disconnect path BEFORE
        # ``_teardown``) disposes the session engine, so the recorder needs its own
        # live engine to finalize the record at teardown. Owned + disposed here.
        self._call_recorder = call_recorder
        self._call_record_engine = call_record_engine
        # Set to 'error' if run() exits via a real exception (crash); a clean
        # disconnect / cancellation stays 'disconnect' (V9-D-5).
        self._end_reason: EndReason = "disconnect"
        # Held so the off-loop warm-up isn't garbage-collected mid-flight; the
        # turn-0 path gates on it (D-32-X-warmup-gates-turn0, wired in A3).
        self._embedder_warmup = embedder_warmup
        self._greet = greet
        self._greet_task: asyncio.Task[None] | None = None
        # V13 (V13-T5): the post-call synthesis enqueue — fired once at teardown, off
        # the loop, best-effort. Accumulation rides the K2 background seam; it is NOT
        # gated by the graph-memory kill-switch (D-6 governs read/surfacing only).
        self._on_call_complete = on_call_complete
        # Spec M3 (T6b-1): the per-turn owner-billing meter (fed by the producer,
        # fired by the recorder). Held here so teardown can bill the call's LiveKit
        # infra (per-min) once at end, off-loop + best-effort. ``None`` ⇒ unmetered.
        self._turn_billing_meter = turn_billing_meter

    async def run(self) -> None:
        """Join the Room, run the loop until disconnect, then tear down."""
        try:
            # Prewarm the Silero ONNX session before the first frame
            # (D-V2-X-silero pillar #3 — never first-frame).
            await self._stt_seam.load()
            await self._voice_room.connect(self._livekit_url, self._agent_token)
            await self._session.mark_active()
            await self._loop.start_pipeline()
            # V9 (V9-D-5): open the durable call-record now the call is genuinely
            # active (best-effort — never breaks the call).
            if self._call_recorder is not None:
                self._call_recorder.open()
            # Greet-first (Spec 32 A3): kick turn 0 off the run() path so the
            # session immediately awaits disconnect while the persona greets.
            if self._greet is not None:
                self._greet_task = asyncio.create_task(self._greet(), name="greet-on-connect")
            _logger.info(
                "voice agent session active; awaiting disconnect (session={session_id})",
                session_id=self._session.session.session_id,
            )
            await self._ended.wait()
        except Exception:
            # A real crash (not a clean disconnect / CancelledError, which is a
            # BaseException and passes through) → record end_reason='error'.
            self._end_reason = "error"
            raise
        finally:
            await self._teardown()

    async def _teardown(self) -> None:
        """Release every resource (idempotent + best-effort).

        Order: stop the pipeline (no further model invokes) → close the STT seam
        (cancel VAD/provider drainers, close the provider socket) → cancel TTS →
        disconnect MCP clients → leave the Room → end the session (disposes the
        RLS engine + releases the advisory lock). Every step is suppressed so one
        failing teardown never strands the others.
        """
        # V10 (V10-D-4/5): cancel any in-flight async-artifact production FIRST,
        # so no off-turn task outlives the call (and none narrates into a torn-down
        # orchestrator). Best-effort, like every teardown step.
        if self._async_lane is not None:
            with contextlib.suppress(Exception):
                await self._async_lane.shutdown()
        # A9 (T6/T10): stop the hand-back poll loop + the dispatcher — the durable outcome heals.
        if self._delegation_dispatcher is not None:
            with contextlib.suppress(Exception):
                await self._delegation_dispatcher.shutdown()
        if self._handback_poller is not None:
            with contextlib.suppress(Exception):
                await self._handback_poller.shutdown()
        for step in (
            self._loop.stop(),
            self._stt_seam.close(),
            self._tts_seam.cancel(),  # type: ignore[attr-defined]
        ):
            with contextlib.suppress(Exception):
                await step
        # V13 (V13-T5): the loop has stopped, so the conversation is final — enqueue
        # post-call graph synthesis now, OFF the loop (a short DB INSERT on its own
        # engine), best-effort. A queue-absent / DB-down failure never strands the
        # rest of teardown. The api worker claims + processes it later (D-4-amended).
        if self._on_call_complete is not None:
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._on_call_complete)
        for client in self._mcp_clients:
            with contextlib.suppress(Exception):
                await client.disconnect()
        with contextlib.suppress(Exception):
            await self._voice_room.disconnect()
        # Idempotent — already ended if the user hung up; disposes the engine
        # otherwise (the crash / cancellation path).
        with contextlib.suppress(Exception):
            await self._session.end()
        # V9 (V9-D-5): finalize the durable call-record (ended_at / duration_s /
        # end_reason), then dispose its DEDICATED engine. Runs AFTER session.end()
        # safely — the recorder's engine is separate, so the session-engine
        # disposal above never strands this write. close() is itself best-effort.
        call_duration_s: int | None = None
        if self._call_recorder is not None:
            call_duration_s = self._call_recorder.close(end_reason=self._end_reason)
        # Spec M3 (T6b-1): bill the call's LiveKit infra (per-min) ONCE at end, off
        # the loop + best-effort + idempotent (keyed per call, so a re-run of
        # teardown is a clean no-op). Runs after the record is finalized so the
        # duration is known; a billing hiccup never strands engine disposal below.
        if self._turn_billing_meter is not None and call_duration_s is not None:
            with contextlib.suppress(Exception):
                await self._turn_billing_meter.bill_call_infra(call_duration_s)
        if self._call_record_engine is not None:
            with contextlib.suppress(Exception):
                self._call_record_engine.dispose()


async def build_agent_session(
    *,
    session_id: str,
    user_id: str,
    persona_id: str,
    conversation_id: str,
    config: VoiceConfig,
    embedder: Embedder | None = None,
    tier_registry: TierRegistry | None = None,
    free_tier_registry: TierRegistry | None = None,
    crisis_encoder: CrisisScorer | None = None,
    core_config: PersonaCoreConfig | None = None,
    stt_config: StreamingSTTConfig | None = None,
    tts_config: StreamingTTSConfig | None = None,
    audit_root: Path | None = None,
    room_factory: Callable[[], VoiceRoom] = build_voice_room,
    broadcaster_factory: Callable[[VoiceRoom], DataChannelBroadcaster] | None = None,
) -> AgentSession:
    """Assemble the real streaming voice session for one call (the heavy root).

    Loads the persona from the RLS-scoped DB row, builds the four unified stores,
    the V5 persona-conditioned producer, the real V2 STT + V3 TTS seams, and the
    V1 :class:`VoiceRoom`, wires them through :func:`wire_orchestrated_loop`, and
    mints the agent's own LiveKit token for the call's Room. Returns an
    :class:`AgentSession` ready to :meth:`~AgentSession.run`.

    ``embedder`` / ``tier_registry`` are injectable so a launcher can share these
    app-scoped, expensive singletons across calls (the persona-api
    ``RuntimeFactory`` precedent) rather than reloading bge + the tier backends
    per call. ``state_listener_factory`` injects the A1 data-channel broadcast.
    """
    # Lazy + deferred imports of orchestration wiring (kept off module import so
    # the agent package stays cheap to import for tests that only touch the
    # lifecycle). The orchestrator import would otherwise pull the full turn-
    # taking subpackage.
    from persona_voice.loop.streaming import StreamingLoop, Transcript
    from persona_voice.turn_taking.bridge import wire_orchestrated_loop
    from persona_voice.turn_taking.states import ConversationalState

    core_config = core_config or PersonaCoreConfig()
    stt_config = stt_config or StreamingSTTConfig()
    tts_config = tts_config or StreamingTTSConfig()
    audit_root = audit_root or (Path(tempfile.gettempdir()) / "persona-voice-agent-audit")
    if embedder is None:
        from persona.stores import SentenceTransformerEmbedder

        # Pin to CPU: bge-small encodes in <10 ms on CPU, and the off-loop
        # warm-up (A1) loads the model on a worker thread — on Apple MPS a
        # threaded device-move raises "Cannot copy out of meta tensor", so the
        # warm-up fails and turn 0 pays the cold load. CPU is robust + fast
        # enough for one recall per turn.
        embedder = SentenceTransformerEmbedder(model_name=_BGE_MODEL, device="cpu")
    if tier_registry is None:
        tier_registry = tier_registry_from_env()

    # Warm the shared embedder OFF the loop now (A1) so turn 0's first recall is
    # not blocked by the synchronous cold model load — the root fix for the
    # first-turn truncation. The turn-0 generation path gates on this task's
    # completion, bounded by the ring degrade ladder (D-32-X-warmup-gates-turn0).
    embedder_warmup = start_embedder_warmup(embedder)

    # --- session RLS engine + persona + stores (tenant-isolated) ---
    rls_engine = make_session_rls_engine(config.database_url, user_id=user_id)
    # Spec M4 (T5c): free-tier no-paid-fallback — a FREE caller's voice LLM resolves the
    # free-only registry (voice wires no preferred override, so the registry swap covers the
    # whole generation + summariser/gate chain). Paid / community byte-identical. Resolved
    # here (off the per-utterance loop) on the session RLS engine.
    tier_registry = _select_voice_tier_registry(
        config=config,
        tier_registry=tier_registry,
        free_tier_registry=free_tier_registry,
        rls_engine=rls_engine,
        user_id=user_id,
    )
    persona = _load_persona(rls_engine, persona_id)
    stores = _build_stores(rls_engine, embedder, audit_root)
    # K6 (K6-D-6): resolve the caller's name ONCE at session setup (off the
    # per-utterance turn loop) so the persona speaks it in the call, exactly as in
    # chat — one persona, one user, coherent across channels. ``None`` ⇒ nameless ⇒
    # byte-identical voice prompt.
    user_name = _load_user_name(rls_engine, user_id)
    # R9-002: the greeting head start — read the K9 core-memory block ONCE here at
    # session setup (the ``_load_user_name`` pattern: off the per-utterance path,
    # fail-soft). The session-constant text is served through the EXISTING
    # ``core_block_provider`` prompt seam on every turn (chat parity, K9-D-10/11),
    # so turn 0's greeting sees the shared history with ZERO added connect→
    # first-word latency: the provider is a closure over this pre-fetched value —
    # no fetch ever runs on the turn path. ``None`` (gate off / no block / error)
    # ⇒ provider stays ``None`` ⇒ byte-identical prompt ⇒ the plain-hello branch.
    core_block_text = _load_core_block(rls_engine, embedder, persona_id, audit_root)
    core_block_provider: Callable[[], str | None] | None = (
        (lambda: core_block_text) if core_block_text is not None else None
    )

    # --- V13 (V13-D-1/D-5/D-6): the K4-gated graph-memory read shell ---
    # Compose the owner-scoped graph store + the K4-gated retrieval (allowlist
    # subtraction + recent-window lift + surfacing + recency, mirrored from chat's
    # ``_build_graph_retrieval`` — never a voice-local variant), ONLY when the
    # kill-switch is ON (D-6). OFF (default) ⇒ both stay ``None`` ⇒ ``VoiceTurnContext``
    # runs graph-off, byte-identical to today. The store is built on the SAME session
    # RLS engine (owner-scoped) with the SAME embedder already warming off-loop above,
    # so the first recall pays no cold load. The retrieval callable runs off the loop
    # (``graph_voice`` overlap-or-skip); this only wires it.
    graph_retrieval: Callable[[str], GraphContext] | None = None
    graph_surfacing_guidance: Callable[[str, GraphRecency], str | None] | None = None
    unified_recall: Callable[[str], UnifiedProjection] | None = None
    if config.graph_memory_enabled:
        from persona.graph import build_graph_store
        from persona.recall.config import RecallSettings

        graph_store = build_graph_store(
            engine=rls_engine,
            embedder=embedder,
            audit_logger=JSONLAuditLogger(audit_root),
        )
        if RecallSettings().unified_enabled:
            # K9 (T9): the unified recall REPLACES the separate graph shell (fuse-don't-route,
            # K9-D-1). Same K4 gate + surfacing (never a voice fork, V13-D-5); env-gated OFF
            # until the T10 operator flip. ``graph_retrieval`` stays ``None`` (the separate
            # graph_task never runs — the unified path carries the graph into ``context.graph``).
            from persona_voice.model.graph import build_voice_unified_recall

            composition = build_voice_unified_recall(
                graph_store,
                cast("EpisodicStore", stores["episodic"]),
                owner_id=user_id,
                persona_id=persona_id,
            )
            unified_recall = composition.retrieval
            graph_surfacing_guidance = composition.surfacing_guidance
        else:
            from persona_voice.model.graph import build_voice_graph_retrieval

            graph_composition = build_voice_graph_retrieval(graph_store, owner_id=user_id)
            graph_retrieval = graph_composition.retrieval
            graph_surfacing_guidance = graph_composition.surfacing_guidance

    # --- per-call language plan (Spec 32 B2) ---
    # Resolve the persona's declared language ONCE into the STT route (B3), the
    # TTS route (B4), and the reply language (B5). Fail-soft never raises; it
    # records fallback events we log + emit here (the typed-event path,
    # D-32-X-typed-event) so an unserved language degrades to English loudly.
    language_plan = resolve_call_languages(persona.identity.language_default)
    for fallback in language_plan.fallbacks:
        _logger.warning(
            "declared voice language not served; falling back to English "
            "(declared={declared} provider={provider} reason={reason})",
            declared=fallback.declared,
            provider=fallback.provider.value,
            reason=fallback.reason,
        )

    # The persona's conservative voice toolbox (VoiceToolPolicy narrows the
    # offered set at generation time — D-V5-4). build_default_toolbox is async,
    # which is why this assembly root is async; its MCP clients are tracked for
    # teardown.
    toolbox, mcp_clients = await build_default_toolbox(core_config, persona)

    # --- A9 voice task-origination gate (A9-D-1) — OFF by default (byte-identical turn) ---
    # Composed only when delegation is enabled: the A4 recognizer (cue net + small-tier judge)
    # + the amendment interpreter, reused VERBATIM (never forked), behind the VOICE echo/confirm.
    # A recognized spoken ask is echoed + confirmed for the ear and DELEGATED to the chat pipeline
    # (the create runs off this loop — A9-D-5/D-7). ``None`` ⇒ the reply producer skips the gate.
    origination_gate = _build_origination_gate(config, persona, tier_registry)

    # --- V5 persona-conditioned, streaming, cancellable producer ---
    conversation = Conversation(conversation_id=conversation_id, persona_id=persona_id)
    tracker = FirstTokenLatencyTracker()
    ctx = VoiceTurnContext(
        persona=persona,
        stores=stores,
        conversation=conversation,
        prompt_builder=PromptBuilder(),
        # Spec P9 (P9-D-1/D-3): voice resolves the latency tier via the policy
        # (no turn-1 frontier — the model hop must fit the 800ms voice budget).
        router=PolicyRouter(tier_registry=tier_registry),
        tier_registry=tier_registry,
        history_manager=ConversationHistoryManager(),
        latency_tracker=tracker,
        toolbox=toolbox,
        language=language_plan,
        # Spec V14 (D-V14-12): under an utterance-level TTS provider (ElevenLabs),
        # select the B5 MIRROR directive so the persona replies in the user's
        # language (defaulting to its own) — each reply speaks its own language on
        # one voice. The provider field is unaffected by apply_tts_route (applied
        # later, only the language code), so reading it here is correct.
        reply_language_mirror=tts_is_utterance_level(tts_config.provider),
        # R6 (T8): the process-shared crisis encoder (launcher-injected). ``None`` ⇒ the
        # voice safety gate is lexical-only (V11) — e.g. a standalone session without a
        # launcher. The reply producer runs the composed classify off the loop.
        crisis_encoder=crisis_encoder,
        user_name=user_name,
        graph_retrieval=graph_retrieval,
        graph_surfacing_guidance=graph_surfacing_guidance,
        unified_recall=unified_recall,
        core_block_provider=core_block_provider,
        origination_gate=origination_gate,
    )
    recorder = VoiceTurnRecorder(
        ctx,
        compactor=VoiceHistoryCompactor(ctx.history_manager),
        summariser=make_small_tier_summariser(tier_registry),
        # V9 (V9-D-1/D-2): persist each committed turn to the durable ``messages``
        # transcript over the SESSION engine (turns commit while the call is live —
        # the engine is alive; no dedicated engine needed, unlike the call-record).
        transcript_writer=VoiceTranscriptWriter(engine=rls_engine, conversation_id=conversation_id),
    )
    # V10 (T4): the rich-output sink + the async-artifact lane are wired into the
    # producer here, but both depend on objects built further down — the
    # broadcaster (needs the room) and the lane (needs the orchestrator's
    # notify_artifact_ready). Bind them late through holders (the same pattern as
    # ``orch_holder`` below): the producer never fires a turn before ``run()``, by
    # which point both holders are populated.
    broadcaster_holder: list[DataChannelBroadcaster] = []
    lane_holder: list[AsyncArtifactLane] = []
    # A9 (T6): the hand-back poller, late-bound (needs the orchestrator's floor-gated narration,
    # built further down) — the same holder pattern.
    poller_holder: list[DelegationHandbackPoller] = []
    # A9 (T10): the delegation dispatcher, late-bound (needs the poller + orchestrator narration).
    dispatcher_holder: list[DelegationDispatcher] = []

    async def _emit_run_event(event: RunEvent) -> None:
        if broadcaster_holder:
            await broadcaster_holder[0].on_run_event(event)

    def _submit_async_artifact(call: ToolCall) -> None:
        if lane_holder:
            lane_holder[0].submit(call)

    # A9 (A9-D-5/D-7/T10): the delegation sink — LIVE when the gate is wired. On a confirmed spoken
    # ask the reply producer hands the VERBATIM ask here; the dispatcher enqueues the durable
    # ``delegated_turn`` job OFF the loop, registers it with the hand-back poller on success, and
    # FAILS SOFT (a spoken "couldn't set it up") if the enqueue fails — no half-created state (voice
    # never creates; the create is the worker's, keyed idempotently). ``None`` gate ⇒ ``None``
    # listener (the gate is inert, nothing delegated).
    def _delegate_turn(intent: DelegatedTurnIntent) -> None:
        if dispatcher_holder:
            dispatcher_holder[0].dispatch(intent)

    delegation_listener = _delegate_turn if origination_gate is not None else None

    # V12 (V12-D-4): the per-session expressivity hand-off — the producer publishes the
    # persona's stance, the Cartesia backend reads it to drive generation_config. Created
    # here (per session, never global) and injected into exactly one producer + one backend.
    expressivity_channel = VoiceExpressivityChannel()

    producer = VoiceModelReplyProducer(
        ctx,
        tool_policy=VoiceToolPolicy(),
        turn_recorder=recorder,
        # T1 seam: inline tool dispatch emits activity_*/tool_result over the data
        # channel. T3 seam: an ASYNC_ARTIFACT call (generate_image) is handed to
        # the off-turn lane (render-when-ready + floor-gated narration).
        on_event=_emit_run_event,
        async_artifact_listener=_submit_async_artifact,
        # V12: publish the resolved per-utterance expressivity to the TTS backend.
        expressivity_listener=expressivity_channel.publish,
        # A9 (A9-D-5/D-7): the LIVE delegation sink — enqueues the durable ``delegated_turn`` job
        # off-loop on a confirmed spoken ask (``None`` when the gate is unwired / delegation OFF).
        delegation_listener=delegation_listener,
    )

    # --- session state machine ---
    session = SessionStateMachine(
        session_id=session_id,
        user_id=user_id,
        persona_id=persona_id,
        conversation_id=conversation_id,
        rls_engine=rls_engine,
    )

    # --- real V2 STT seam (Deepgram + Silero); echo-mute reads the orchestrator ---
    # The VAD's TTS-mute provider needs the orchestrator's ``is_agent_speaking``,
    # but the orchestrator is built after the loop — bind it late via a holder.
    orch_holder: list[object] = []

    def _agent_speaking() -> bool:
        return bool(orch_holder) and bool(orch_holder[0].is_agent_speaking())  # type: ignore[attr-defined]

    # Pin the Deepgram model + language code to the persona's declared language
    # (Spec 32 B3) — nova-3 + ``no`` for Norwegian, overriding the global env
    # hint (D-32-X-deepgram-no-nova3). This is what stops the websocket 400 on
    # ``nb`` and the force-decode of Norwegian speech as English.
    # Spec V14 (D-V14-5): an utterance-level STT provider (Gladia code-switching)
    # derives language from the audio itself — pinning would re-narrow it to one
    # declared language and defeat D-V14-1. ``maybe_apply_stt_route`` skips the
    # route for it; the incumbent (Deepgram) path is byte-identical. Rollback =
    # flip the provider back.
    stt_config = maybe_apply_stt_route(stt_config, language_plan.stt)
    stt_backend = load_streaming_stt(stt_config)
    # TTS-mute-window is opt-in (default OFF — D-V8-X-bargein-during-speech-fix,
    # operator-pass 2026-06-23). A hard mute blocks a *real* barge-in onset from
    # reaching the orchestrator, so the persona can't be interrupted while speaking
    # and the user's barge-in audio is withheld from the billed stream until the
    # persona finishes. Browser/transport AEC removes the persona echo and the
    # orchestrator's confidence + confirm-window rejects the rest, so the mute is
    # only needed on a proven-no-AEC deployment.
    mute_provider = _agent_speaking if stt_config.silero_echo_mute_while_speaking else None
    vad = SileroVADAdapter(stt_config, session_state_provider=mute_provider)
    # Spec V8: the cost gate + the ring-buffer-on-reopen (D-V8-X-measure-stop-verdict).
    # The ring buffers the pre-reopen audio so barge-in / post-idle first words
    # survive the gated→open transition; the gate (IdleAwareGate, wired below once
    # the orchestrator exists) streams only the user's turn.
    stt_seam = V1STTStreamSeamAdapter(
        backend=stt_backend, vad=vad, reopen_preroll_ms=DEFAULT_REOPEN_PREROLL_MS
    )

    # --- real V3 TTS seam bound to THIS persona's voice ---
    # Pin the Cartesia synthesis language to the persona's declared language
    # (Spec 32 B4) — the missing ``language`` param that fixes Norwegian text
    # being read with English phonetics. Voices are multilingual, so this is a
    # language code, not a voice constraint.
    # Spec V14 (D-V14-5): an utterance-level TTS provider (ElevenLabs) speaks the
    # language the reply TEXT is written in — pinning a language would fight the
    # per-reply auto-follow (D-V14-1). ``maybe_apply_tts_route`` skips the route
    # for it; the incumbent (Cartesia) path is byte-identical.
    tts_config = maybe_apply_tts_route(tts_config, language_plan.tts)
    # V12: bind the same per-session channel so the backend reads what the producer publishes.
    tts_backend = load_streaming_tts(tts_config, expressivity_channel=expressivity_channel)
    tts_seam = build_seam_adapter(
        backend=tts_backend,
        config=tts_config,
        voice_spec=persona.identity.voice,
    )

    # Spec M3 (T6b-1): the per-turn owner-billing meter. Built HERE (not at the
    # recorder/producer above) because it prices the ACTUALLY-SERVED STT/TTS
    # providers — read off the live backends AFTER V14 language routing has picked
    # them — and reads the V8 real-streamed-seconds off the STT seam. Injected into
    # the already-constructed producer (feeds it TTS chars + LLM usage per turn) and
    # recorder (fires the off-loop deduct on commit) via their late-binding setters,
    # before run() fires the first turn. The deduct opens its own fresh short-lived
    # RLS engine per turn (the ``_on_call_complete`` idiom) — never the audio loop.
    # Spec M5 (B5, D-M5-22): the auto-top-up trigger, gated at CONSTRUCTION.
    #
    # Non-``None`` only on a cloud edition, so community is PROVABLY inert: the
    # collaborator is absent, so no code path exists, rather than a branch evaluated on
    # every turn. NB this is the FIRST edition check on the voice deduct path (D-M5-23) —
    # community voice really does meter into its local DB today, so this gate is
    # load-bearing on its own and cannot lean on an upstream one.
    #
    # Voice enqueues rather than charging: an auto-top-up needs Stripe credentials this
    # process must never hold (D-M5-15). The api worker evaluates the crossing.
    def _enqueue_topup(old_balance: int, new_balance: int, turn_seq: int) -> None:
        engine = make_session_rls_engine(config.database_url, user_id=user_id)
        try:
            enqueue_auto_topup(
                engine,
                owner_id=user_id,
                old_balance=old_balance,
                new_balance=new_balance,
                call_id=session_id,
                turn_seq=turn_seq,
            )
        finally:
            engine.dispose()

    turn_billing_meter = VoiceTurnBillingMeter(
        ledger=CoreCreditsLedger(),
        billing_config=BillingConfig(),
        engine_factory=lambda: make_session_rls_engine(config.database_url, user_id=user_id),
        enqueue_topup=_enqueue_topup if config.is_cloud else None,
        user_id=user_id,
        call_id=session_id,  # per-CALL identity (unique per connection). NOT conversation_id:
        # a voice conversation persists across separate calls, so conversation_id would collide
        # the per-turn billing_key (voice:{call_id}:{turn_seq}) + the :livekit infra charge
        # across calls — under-billing repeat calls and (pre cb3d156) tripping a false ~37s
        # exhaustion cutoff on the stale-key idempotent no-op. session_id is per-call-unique
        # (R9-056; runner linkage covered by the V6 voice operator pass, meter by turn_meter tests).
        stt_provider=stt_backend.provider_name,
        stt_model=stt_backend.model_name,
        tts_provider=tts_backend.provider_name,
        tts_model=tts_backend.model_name,
        streamed_seconds_reader=lambda: stt_seam.streamed_seconds,
    )
    producer.set_turn_meter(turn_billing_meter)
    recorder.set_billing_meter(turn_billing_meter)

    # --- transport + loop + orchestrator ---
    voice_room = room_factory()
    loop = StreamingLoop(
        voice_room=voice_room,
        session=session,
        stt=stt_seam,
        tts=tts_seam,
        model=producer,
        first_audio_timeout_s=config.turn_first_audio_timeout_s,
    )
    # The A1 data-channel broadcaster implements BOTH the V4 state-listener seam
    # (orb) AND the V6 caption-listener seam (captions) over one room+topic, so it
    # wires into both. Default-built over this call's room; injectable for tests.
    broadcaster = (
        broadcaster_factory(voice_room)
        if broadcaster_factory
        else (DataChannelBroadcaster(voice_room))
    )
    # A9 (A9-D-3): when the gate is wired, its barge-aware commit hook observes each turn's
    # commit alongside the V5 memory recorder — a barged (truncated) echo does not arm a
    # confirmable proposal. Fan the single loop transcript-listener slot to both; ``None`` gate ⇒
    # just the recorder (byte-identical to today).
    turn_listener: TurnTranscriptListener = recorder
    if origination_gate is not None:
        turn_listener = CompositeTurnTranscriptListener(
            [recorder, GateCommitListener(origination_gate)]
        )
    # Greet-first (Spec 32 A3): the orchestrator opens in PREPARING so the
    # persona generates turn 0 (the greeting) before any user input.
    orchestrator = wire_orchestrated_loop(
        loop=loop,
        session=session,
        state_listener=broadcaster,
        turn_transcript_listener=turn_listener,
        initial_state=ConversationalState.PREPARING,
    )
    loop.caption_listener = broadcaster
    orch_holder.append(orchestrator)
    # V10 (T4): complete the late binding — the producer's rich-output sink is this
    # broadcaster, and the async-artifact lane produces off-turn (its render frames
    # flow over the same broadcaster) and narrates render-when-ready through the
    # orchestrator's floor-gated ``notify_artifact_ready`` (V10-D-2/3). The lane is
    # cancelled at teardown (V10-D-4/5).
    broadcaster_holder.append(broadcaster)
    async_lane = AsyncArtifactLane(
        toolbox=toolbox,
        on_ready=orchestrator.notify_artifact_ready,
        on_event=broadcaster.on_run_event,
    )
    lane_holder.append(async_lane)
    # A9 (T6): the delegation hand-back poller — LIVE only when the gate is wired. It watches each
    # delegated turn's terminal job row and speaks the grounded result at the next idle floor via
    # the SAME floor-gated narration the async-artifact lane uses (never over the user). The
    # delegation listener above registers each key with it; it is cancelled at teardown (the durable
    # result self-heals). ``None`` gate ⇒ no poller (nothing is delegated).
    handback_poller: DelegationHandbackPoller | None = None
    delegation_dispatcher: DelegationDispatcher | None = None
    if origination_gate is not None:
        handback_poller = DelegationHandbackPoller(
            engine=rls_engine,
            owner_id=user_id,
            conversation_id=conversation_id,
            on_handback=orchestrator.notify_artifact_ready,
        )
        poller_holder.append(handback_poller)

        async def _speak_delegation_failed() -> None:
            """Fail-soft narration (T10): the enqueue failed, so tell the user it couldn't be set.

            Spoken at the next idle floor through the same floor-gated seam the hand-back uses — no
            half-created state (nothing was created; the durable job never landed)."""
            await orchestrator.notify_artifact_ready(
                Transcript(
                    is_final=True,
                    text=(
                        "A task the user just asked for by voice could not be set up. In one "
                        "short, kind sentence, let them know you weren't able to set it up."
                    ),
                    confidence=1.0,
                )
            )

        delegation_dispatcher = DelegationDispatcher(
            engine=rls_engine,
            owner_id=user_id,
            poller=handback_poller,
            on_failed=_speak_delegation_failed,
        )
        dispatcher_holder.append(delegation_dispatcher)

    async def _greet() -> None:
        """Run turn 0 — gate on the warm-up, then have the persona greet first."""
        await orchestrator.begin_greeting(
            _greeting_transcript(),
            warmup=embedder_warmup,
            warmup_timeout_s=config.greet_warmup_timeout_s,
            greet_timeout_s=config.greet_timeout_s,
        )

    # The STT seam dispatches speech-activity events to its listener — the
    # orchestrator (so VAD onset/offset drive the conversational state machine).
    stt_seam.listener = orchestrator

    # Spec V8 cost gate (D-V8-X-measure-stop-verdict): the SHIPPED idle-gate —
    # stream only the user's turn (USER_SPEAKING/PROCESSING); withhold the billed
    # Deepgram leg during PERSONA_SPEAKING + LISTENING idle + PREPARING (~85% saving
    # on a listen-heavy call). It supersets #1's state-gate. The split-tee (D-V8-1)
    # keeps feeding Silero so barge-in/onset still fire locally → the orchestrator
    # leaves the gated state → the gate reopens, and the seam adapter's
    # ring-buffer-on-reopen flushes the pre-reopen audio so no first word is clipped.
    # Bound directly (the gate is read per-frame); pre-V8 behaviour holds wherever
    # the seam is built without a gate (default open).
    stt_seam.gate = IdleAwareGate(source=orchestrator)

    # --- room disconnect → end the session + release the run() awaiter ---
    ended = asyncio.Event()

    async def _on_room_disconnected() -> None:
        await session.end()
        ended.set()

    voice_room.set_disconnect_handler(_on_room_disconnected)

    # --- the agent's own LiveKit token for THIS call's Room ---
    # Distinct identity from the user (LiveKit rejects duplicate identities in a
    # Room); same deterministic room name (``persona:{session_id}``); the grants
    # already include can_subscribe (user mic) + can_publish (persona audio) +
    # can_publish_data (the A1 state/transcript broadcast).
    agent_token = mint_room_access_token(
        api_key=config.livekit_api_key.get_secret_value(),
        api_secret=config.livekit_api_secret.get_secret_value(),
        livekit_url=config.livekit_url,
        session_id=session_id,
        user_id=f"agent:{session_id}",
        persona_id=persona_id,
        conversation_id=conversation_id,
        ttl_s=config.livekit_token_ttl_s,
    )

    # Spec M3 (T6b-2): the mid-call cutoff — wired into the per-turn meter's
    # ``on_exhausted``. On the FIRST turn whose real deduct exhausts the balance it
    # speaks one brief grounded notice (bounded), then deletes the room (both
    # parties disconnect → the agent's room-disconnect handler drives the normal
    # teardown: the call-record finalizes + the LiveKit infra tick bills). On a
    # delete_room failure it falls back to the agent-leave path (``ended.set`` →
    # run() unblocks → ``_teardown``). Reached only through the REAL deduct chain.
    _cutoff_room_name = agent_token.room_name

    async def _delete_room_on_exhaustion() -> None:
        lk = api.LiveKitAPI(
            config.livekit_url,
            config.livekit_api_key.get_secret_value(),
            config.livekit_api_secret.get_secret_value(),
        )
        try:
            await lk.room.delete_room(api.DeleteRoomRequest(room=_cutoff_room_name))
        finally:
            await lk.aclose()  # type: ignore[no-untyped-call]  # livekit SDK's aclose() is untyped (R9-057)

    async def _speak_exhaustion_notice() -> None:
        # The floor-gated narration seam (the same one delegation/artifact narration
        # uses) — a grounded one-liner, never a raw canned string.
        await orchestrator.notify_artifact_ready(
            Transcript(
                is_final=True,
                text=(
                    "The user has run out of voice credits, so the call must end now. In "
                    "one short, warm sentence, let them know you're out of credits and "
                    "have to say goodbye."
                ),
                confidence=1.0,
            )
        )

    # The mid-call cutoff is a CLOUD enforcement mechanism, so community never
    # arms it. Leaving ``on_exhausted`` unset is the meter's own documented
    # "metering-only" shape, so community keeps its local spend VISIBLE while
    # nothing can ever act on it.
    #
    # Why this gate exists: community voice does meter. It bypasses the api's
    # ``UnlimitedCreditsPolicy`` edition seam entirely and calls the core ledger
    # directly, which is what D-M3-core-seam authorises for latency, so
    # D-M4-community-noop ("community is unmetered") describes the api's shape
    # and not this path. UnGated, a self-hosted user drew down the 100_000
    # credits ``ensure_balance`` seeds and then hit the cutoff, which deletes the
    # room and ends the call — with no Stripe, no top-up and no paywall in
    # community to resolve it. A large buffer followed by local voice breaking
    # permanently, with no remedy. The buffer is why it was not urgent; it is not
    # why it was harmless.
    #
    # Deliberately NOT changing the deduct itself: metering is useful local
    # visibility for a self-hoster, and rewriting the ledger path would change
    # M3 billing behaviour well beyond this fix. Enforcement is the harm; the
    # bookkeeping is not.
    if config.is_cloud:
        turn_billing_meter.set_on_exhausted(
            VoiceExhaustionCutoff(
                delete_room=_delete_room_on_exhaustion,
                speak_notice=_speak_exhaustion_notice,
                on_fallback=ended.set,
            ).trigger
        )

    # --- V9 (V9-D-5): the durable call-record writer over a DEDICATED RLS engine ---
    # Separate from the session engine on purpose: the clean-hangup path fires
    # ``session.end()`` (disposing the session engine) BEFORE ``_teardown``, so the
    # recorder needs its own live engine to finalize the record at teardown.
    # API-free — writes the api-owned ``calls`` table via core's ``_calls`` view
    # (the ``memory_chunks`` P2 precedent), no persona-api import.
    call_record_engine = make_session_rls_engine(config.database_url, user_id=user_id)
    call_recorder = CallRecorder(
        engine=call_record_engine,
        call_id=f"call_{uuid.uuid4().hex}",
        conversation_id=conversation_id,
        persona_id=persona_id,
        owner_id=user_id,
    )

    # V13 (V13-T5) + R9-028: post-call graph synthesis AND the transcript-title
    # refresh. At session-end the conversation is complete, so enqueue ONE durable
    # job of each kind (post-call batch, D-4) over a FRESH short-lived session RLS
    # engine — the session engine is disposed by ``session.end()`` before teardown
    # (the V9 recorder pattern), and a fresh engine keeps these INSERTs owner-scoped
    # without holding a connection for the whole call. Each twin raw-INSERT writer
    # builds the SAME core payload its api counterpart builds. The two enqueues are
    # independently best-effort (own suppress each) — a DB hiccup on one must never
    # skip the other; ``_teardown``'s outer suppress is only the final backstop.
    def _on_call_complete() -> None:
        from persona_voice.session.synthesis_enqueue import enqueue_voice_synthesis
        from persona_voice.session.title_enqueue import enqueue_voice_title_refresh

        message_count = len(conversation.messages)
        eng = make_session_rls_engine(config.database_url, user_id=user_id)
        try:
            with contextlib.suppress(Exception):
                enqueue_voice_synthesis(
                    eng,
                    owner_id=user_id,
                    conversation_id=conversation_id,
                    persona_id=persona_id,
                    message_count=message_count,
                )
            # R9-028: the web trigger's {4,10,24,50,100} thresholds under-fire for
            # short calls — this fires ONCE, unconditionally, regardless of count.
            # Keyed on the final count, so a re-run of teardown is a safe no-op.
            with contextlib.suppress(Exception):
                enqueue_voice_title_refresh(
                    eng,
                    owner_id=user_id,
                    conversation_id=conversation_id,
                    message_count=message_count,
                )
        finally:
            eng.dispose()

    # mcp_clients accumulated by build_default_toolbox are closed at teardown.
    return AgentSession(
        voice_room=voice_room,
        loop=loop,
        stt_seam=stt_seam,
        tts_seam=tts_seam,
        session=session,
        mcp_clients=mcp_clients,
        livekit_url=agent_token.livekit_url,
        agent_token=agent_token.token,
        ended=ended,
        embedder_warmup=embedder_warmup,
        greet=_greet,
        call_recorder=call_recorder,
        call_record_engine=call_record_engine,
        async_lane=async_lane,
        handback_poller=handback_poller,
        delegation_dispatcher=delegation_dispatcher,
        on_call_complete=_on_call_complete,
        turn_billing_meter=turn_billing_meter,
    )


async def run_agent_session(
    *,
    session_id: str,
    user_id: str,
    persona_id: str,
    conversation_id: str,
    config: VoiceConfig,
    embedder: Embedder | None = None,
    tier_registry: TierRegistry | None = None,
    free_tier_registry: TierRegistry | None = None,
    crisis_encoder: CrisisScorer | None = None,
    core_config: PersonaCoreConfig | None = None,
    broadcaster_factory: Callable[[VoiceRoom], DataChannelBroadcaster] | None = None,
) -> None:
    """Build + run one voice agent session to completion (the convenience entry).

    The single coroutine the dev launcher (:mod:`persona_voice.agent.launcher`)
    spawns per call: assemble the real session, then run its connect→loop→teardown
    lifecycle. Any exception propagates to the launcher, which logs it (a failed
    agent must never take down the token endpoint).
    """
    session = await build_agent_session(
        session_id=session_id,
        user_id=user_id,
        persona_id=persona_id,
        conversation_id=conversation_id,
        config=config,
        embedder=embedder,
        free_tier_registry=free_tier_registry,
        tier_registry=tier_registry,
        crisis_encoder=crisis_encoder,
        core_config=core_config,
        broadcaster_factory=broadcaster_factory,
    )
    await session.run()
