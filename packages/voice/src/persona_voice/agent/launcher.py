"""The dev agent launcher — spawns one :func:`run_agent_session` per call.

This is the **dev/operator-pass-grade** trigger that gets the agent into the
Room (D-V6-X-agent-worker scope guardrail). When the browser calls
``POST /v1/voice/token`` (the user about to join ``persona:{session_id}``), the
token endpoint — if the launcher is configured (``PERSONA_VOICE_AGENT_INPROCESS=
true``) — calls :meth:`InProcessAgentLauncher.launch`, which spawns the agent
session that joins the *same* Room and becomes the persona on the call.

**This is NOT a production supervisor.** It spawns one fire-and-forget task per
call, isolates failures (a crashed agent never takes down the token endpoint),
and shares the two expensive app-scoped singletons (the bge embedder + the tier
registry) across calls — exactly the persona-api ``RuntimeFactory`` precedent.
It does NOT pool, queue, autoscale, or match dispatch. Production worker-ops are
an explicit forward-item; crossing into them is the scope line.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.logging import get_logger
from persona_runtime.tier import tier_registry_from_env

from persona_voice.agent.runner import run_agent_session

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from persona.stores.embedder import Embedder
    from persona_runtime.crisis_encoder import CrisisEncoder
    from persona_runtime.tier import TierRegistry

    from persona_voice.config import VoiceConfig
    from persona_voice.transport.broadcast import DataChannelBroadcaster
    from persona_voice.transport.room import VoiceRoom

__all__ = ["AgentLauncher", "InProcessAgentLauncher"]

_logger = get_logger("agent.launcher")

_BGE_MODEL = "BAAI/bge-small-en-v1.5"


@runtime_checkable
class AgentLauncher(Protocol):
    """Launches the persona's agent into a call's Room (fire-and-forget)."""

    def launch(
        self, *, session_id: str, user_id: str, persona_id: str, conversation_id: str
    ) -> None: ...


class InProcessAgentLauncher:
    """Spawns one :func:`run_agent_session` task per call, in this process.

    Shares the app-scoped embedder + tier registry across calls (built lazily in
    the first spawned task so the token request never blocks on bge load).
    Failures are caught + logged — a failed agent session must never propagate
    into the token endpoint that triggered it.
    """

    def __init__(
        self,
        config: VoiceConfig,
        *,
        runner: Callable[..., Awaitable[None]] = run_agent_session,
        broadcaster_factory: Callable[[VoiceRoom], DataChannelBroadcaster] | None = None,
    ) -> None:
        self._config = config
        self._runner = runner
        self._broadcaster_factory = broadcaster_factory
        self._embedder: Embedder | None = None
        self._tier_registry: TierRegistry | None = None
        # R6 (T8): the process-shared crisis encoder + its off-loop warm-up task, built
        # once like the embedder and injected into every call's VoiceTurnContext so the
        # voice safety gate composes the encoder (euphemistic/non-English recall).
        self._crisis_encoder: CrisisEncoder | None = None
        self._crisis_warmup: asyncio.Task[None] | None = None
        self._tasks: set[asyncio.Task[None]] = set()
        self._singletons_lock = asyncio.Lock()

    def launch(
        self, *, session_id: str, user_id: str, persona_id: str, conversation_id: str
    ) -> None:
        """Spawn the agent session for this call (returns immediately)."""
        task: asyncio.Task[None] = asyncio.create_task(
            self._run_guarded(
                session_id=session_id,
                user_id=user_id,
                persona_id=persona_id,
                conversation_id=conversation_id,
            ),
            name=f"voice-agent-{session_id}",
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _run_guarded(
        self, *, session_id: str, user_id: str, persona_id: str, conversation_id: str
    ) -> None:
        try:
            await self._ensure_singletons()
            await self._runner(
                session_id=session_id,
                user_id=user_id,
                persona_id=persona_id,
                conversation_id=conversation_id,
                config=self._config,
                embedder=self._embedder,
                tier_registry=self._tier_registry,
                crisis_encoder=self._crisis_encoder,
                broadcaster_factory=self._broadcaster_factory,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # A failed agent session must never escape into the token endpoint.
            _logger.exception(
                "voice agent session failed (session={session_id})",
                session_id=session_id,
            )

    async def _ensure_singletons(self) -> None:
        """Build the shared embedder + tier registry once (lazy, off the request)."""
        async with self._singletons_lock:
            if self._embedder is None:
                from persona.stores import SentenceTransformerEmbedder

                # CPU for the voice path (see runner.build_agent_session): the
                # off-loop warm-up loads on a worker thread, which raises a
                # meta-tensor error on Apple MPS. The bge-small CPU cold-load is
                # SLOW on some machines (~27s on an M1) — :meth:`warm` pays it at
                # server startup so no call's first turn ever blocks on it.
                self._embedder = SentenceTransformerEmbedder(model_name=_BGE_MODEL, device="cpu")
            if self._tier_registry is None:
                self._tier_registry = tier_registry_from_env()
            if self._crisis_encoder is None:
                # R6 (T8): lazy — the model loads at :meth:`warm` (off-loop), never on a
                # call's first turn. Built only when enabled; disabled ⇒ lexical-only (V11).
                from persona_runtime.crisis_encoder import build_crisis_encoder
                from persona_runtime.safety_intercept import SafetyInterceptSettings

                if SafetyInterceptSettings().encoder_enabled:
                    self._crisis_encoder = build_crisis_encoder()

    async def warm(self) -> None:
        """Build + warm the shared singletons at SERVER STARTUP (BLOCKING).

        The embedder's first ``encode()`` materialises the bge model + CPU
        kernels synchronously (~tens of seconds on some machines). Paying that
        lazily on the first call blocks that call's first turn — the persona
        cannot recall memory, so the reply is empty and the call hangs. Called
        from the app's lifespan startup, this **awaits** the cold load off the
        event loop, so the server is "ready" only once the embedder is warm.
        uvicorn holds incoming requests until lifespan-startup completes, so the
        first call simply waits a few seconds, then runs warm — no hang, ever.
        Best-effort: a failed warm is swallowed (a turn pays the cold load then).
        """
        from persona_voice.agent.warmup import start_embedder_warmup

        await self._ensure_singletons()
        if self._embedder is not None:
            await start_embedder_warmup(self._embedder)
        # R6 (T8): warm the crisis encoder in the BACKGROUND (non-blocking) — unlike the
        # embedder (which gates turn-0 recall, so it's awaited), a not-yet-warm crisis
        # encoder is fail-soft: a turn's score times out (the 2 s hang-guard) and runs
        # lexical-only, so the ~40 s crisis load must NOT add to the blocking voice-startup
        # window. The task is held on ``self`` so it is not GC'd.
        if self._crisis_encoder is not None:
            from persona_runtime.crisis_encoder import start_crisis_encoder_warmup

            self._crisis_warmup = start_crisis_encoder_warmup(self._crisis_encoder)

    async def aclose(self) -> None:
        """Cancel any in-flight sessions + dispose the shared tier registry."""
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:  # noqa: SIM105 — suppress would import contextlib for one line
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._tasks.clear()
        if self._tier_registry is not None:
            await self._tier_registry.aclose()
            self._tier_registry = None
