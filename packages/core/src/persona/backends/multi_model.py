"""MultiModelChatBackend — cross-provider ordered fallback wrapper (Spec 20).

Composes N concrete :class:`ChatBackend` instances into a single
Protocol-compatible backend per D-20-9 / D-20-10 / D-20-12 / D-20-15:

* **D-20-9** — three-bucket classifier:
  RETRY-THEN-FALLBACK (transient: timeouts, 5xx, 409, short rate-limits) /
  FALLBACK-NO-RETRY (semi-permanent: auth, model-missing, model-unavailable
  (R9-073a — 403 plan/entitlement denial), credit-out, long rate-limits) /
  SURFACE (semantic: bad-request, content-policy).
* **D-20-10** — N=1 same-model retry with a 200ms ± jittered ±50% sleep
  before falling through. Wrapper is the SOLE retry decider; the SDK-level
  retry loops MUST be disabled (``max_retries=0`` on openai/anthropic SDK
  constructors) so this budget is enforced verbatim.
* **D-20-12** — cross-provider AuthenticationError → SKIP-AND-FALLBACK with
  a structured WARNING log; the next backend has a DIFFERENT key under a
  DIFFERENT env var, so silent-mask risk is acknowledged via the WARNING.
* **D-20-15** — runtime :class:`ProviderCredentialMissingError` (the
  resolver did not catch the slot at construction) → FALLBACK-NO-RETRY.
* **D-20-16** — :class:`AllModelsFailedError` is a :class:`PersonaError`
  (wrapper-layer), NOT a :class:`ProviderError`.

The wrapper implements the :class:`ChatBackend` Protocol verbatim so
callers (ConversationLoop, AgenticLoop) cannot distinguish it from a bare
single-backend.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Final

from persona.backends.errors import (
    AllModelsFailedError,
    AuthenticationError,
    BackendTimeoutError,
    BackendVisionNotSupportedError,
    EmptyCompletionError,
    ModelNotFoundError,
    ModelUnavailableError,
    NoVisionCapableModelError,
    ProviderCredentialMissingError,
    ProviderError,
    RateLimitError,
)
from persona.errors import PersonaError
from persona.logging import get_logger
from persona.schema.content import ImageContent

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends.protocol import ChatBackend
    from persona.backends.types import ChatResponse, StreamChunk, ToolSpec
    from persona.schema.conversation import ConversationMessage

__all__ = ["AllModelsFailedError", "AttemptRecord", "MultiModelChatBackend"]

_LOG = get_logger("backends.multi_model")

# D-20-10 lock — single same-model retry budget per backend.
_DEFAULT_MAX_RETRIES_PER_BACKEND: Final[int] = 1
# D-20-9 row 3 cutoff — Retry-After above this triggers FALLBACK-NO-RETRY.
_RATE_LIMIT_RETRY_AFTER_CUTOFF_S: Final[float] = 2.0
# D-20-10 jittered retry sleep — 200ms baseline, ±50% jitter.
_RETRY_BASE_SLEEP_S: Final[float] = 0.2
_RETRY_JITTER_FRAC: Final[float] = 0.5

# Classifier bucket labels (kept as strings — caller-facing only via logs).
_RETRY_THEN_FALLBACK: Final[str] = "RETRY-THEN-FALLBACK"
_FALLBACK_NO_RETRY: Final[str] = "FALLBACK-NO-RETRY"
_SURFACE: Final[str] = "SURFACE"

# HTTP status codes in the transient bucket (D-20-9). 409 covers concurrent
# session/conflict cases observed on Anthropic; 529 is Anthropic's "overloaded".
_TRANSIENT_STATUS_CODES: Final[frozenset[int]] = frozenset({409, 500, 502, 503, 504, 529})


@dataclass(frozen=True)
class AttemptRecord:
    """Per-backend attempt outcome for :class:`AllModelsFailedError` context.

    One record is appended per backend the wrapper has tried (whether the
    backend fell through or surfaced); the records ride inside the final
    ``AllModelsFailedError`` so operators can see the full fallback walk in
    one log line.

    Attributes:
        provider: ``backend.provider_name`` at attempt time.
        model: ``backend.model_name`` at attempt time.
        last_error_class: Class name of the exception that caused fallback
            (or the surface-bucket raise).
        last_error_status_code: HTTP status code if the exception was a
            :class:`ProviderError` carrying ``status_code`` in its context
            (parsed best-effort), else ``None``.
        retried_same_model: ``True`` iff the wrapper consumed at least one
            same-model retry on this backend before giving up.
    """

    provider: str
    model: str
    last_error_class: str
    last_error_status_code: int | None
    retried_same_model: bool


class MultiModelChatBackend:
    """Ordered cross-provider fallback wrapper over N ChatBackend instances.

    Implements :class:`persona.backends.protocol.ChatBackend` verbatim;
    callers depend on the Protocol, not on this concrete type. Construction
    invariant: ``backends`` length ≥ 1 (a single-entry list is the
    degenerate single-backend pass-through path).

    Args:
        backends: Ordered list of concrete :class:`ChatBackend` instances.
            Position 0 is the primary; position N-1 is the last-resort.
        tier_name: Configured tier label (``"frontier"``, ``"mid"``, ...);
            used only to thread through into :class:`AllModelsFailedError`
            context and WARNING-log records.
        max_retries_per_backend: D-20-10 budget. Default 1; set to 0 to
            disable retries entirely (operator escape hatch, e.g. tests).
    """

    def __init__(
        self,
        backends: list[ChatBackend],
        *,
        tier_name: str | None = None,
        max_retries_per_backend: int = _DEFAULT_MAX_RETRIES_PER_BACKEND,
    ) -> None:
        if not backends:
            msg = "MultiModelChatBackend requires at least one backend"
            raise ValueError(msg)
        self._backends: list[ChatBackend] = backends
        self._tier_name: str | None = tier_name
        self._max_retries: int = max_retries_per_backend
        # Spec 20 T19 (D-20-9) — per-call attempt ledger surfaced to the
        # ConversationLoop write-site so TurnLog can capture which backends
        # fell through to reach the successful winner. RESET at the START of
        # every chat() / chat_stream() call so concurrent / sequential calls
        # never bleed state. Class-name-only privacy is enforced by
        # ``_record_attempt`` which captures ``type(exc).__name__`` (never the
        # exception message).
        self._last_attempts: list[AttemptRecord] = []

    # ------------------------------------------------------------------ #
    # ChatBackend Protocol properties — delegate to PRIMARY backend.
    # ------------------------------------------------------------------ #

    @property
    def tier_name(self) -> str | None:
        """Configured tier label passed at construction (read-only).

        T17 wiring + TurnLog instrumentation read this to surface the tier
        in fallback-chain audit fields.
        """
        return self._tier_name

    @property
    def backends(self) -> list[ChatBackend]:
        """Composed backends in fallback order (read-only view)."""
        return self._backends

    @property
    def last_attempts(self) -> list[AttemptRecord]:
        """Per-call attempt ledger from the most recent chat / chat_stream call.

        Read by the ConversationLoop write-site (Spec 20 T19; D-20-9) to
        populate the TurnLog ``tier_fallback_*`` fields. Returns the live
        list (callers MUST NOT mutate — copy via ``list(...)`` if needed).

        On a clean primary-success call the ledger is empty (no fallback
        engaged). On a successful fallback the ledger holds one
        :class:`AttemptRecord` per backend that fell through. On
        :class:`AllModelsFailedError` the ledger holds one record per
        backend tried (all failed).

        Cleared at the START of every chat / chat_stream call.
        """
        return self._last_attempts

    @property
    def provider_name(self) -> str:
        """Primary backend's provider; not the currently-active one."""
        return self._backends[0].provider_name

    @property
    def model_name(self) -> str:
        """Primary backend's model; not the currently-active one."""
        return self._backends[0].model_name

    @property
    def supports_native_tools(self) -> bool:
        """``True`` iff ANY backend in the chain supports native tools.

        Permissive ceiling: at call time the wrapper dispatches to whichever
        backend serves the request (primary on success; any of the secondaries
        if fallback fires). If ANY backend supports native tools, the wrapper
        can deliver native tool_calls — so the runtime must treat the wrapper
        as native-capable and append the ``assistant`` message carrying
        ``tool_calls`` to history per the OpenAI/Anthropic protocol.

        Earlier ``all(...)`` semantics surfaced a production bug 2026-06-10
        19:44 UTC: in a mixed chain (e.g., nvidia + deepseek + openai where
        one specific model wasn't in the native-tools allow-list), the wrapper
        reported False; the runtime skipped the ``assistant_with_tool_calls``
        append; ``format_tool_result`` still emitted ``role="tool"``; the next
        round's history had an orphaned tool message → DeepSeek 400
        ("Messages with role 'tool' must be a response to a preceding message
        with 'tool_calls'"). The active backend's own ``supports_native_tools``
        still gates whether THAT backend uses native vs shim for the call;
        the wrapper just promises "at least one of us can".
        """
        return any(getattr(b, "supports_native_tools", False) for b in self._backends)

    @property
    def supports_vision(self) -> bool:
        """``True`` iff ANY backend in the chain supports vision.

        Same permissive-ceiling rationale as :attr:`supports_native_tools` —
        the wrapper dispatches to whichever backend serves the request; if any
        one supports vision, the wrapper can serve a vision-bearing turn (via
        fallback if the primary doesn't). Callers requesting vision still get
        a fail-loud :class:`BackendVisionNotSupportedError` from the active
        backend if NONE of the chain supports it.
        """
        return any(getattr(b, "supports_vision", False) for b in self._backends)

    # ------------------------------------------------------------------ #
    # ChatBackend Protocol — non-streaming chat.
    # ------------------------------------------------------------------ #

    async def chat(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: list[str] | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> ChatResponse:
        """Walk backends per D-20-9 / D-20-10 / D-20-12; return first success.

        ``top_p`` / ``top_k`` are forwarded verbatim to each wrapped backend
        (each backend applies its own provider-support rules).

        Raises:
            AllModelsFailedError: every backend exhausted its per-bucket path.
            Any SURFACE-bucket exception: short-circuits the walk.
        """
        # T19 — reset per-call ledger so :attr:`last_attempts` reflects only
        # this invocation (the ConversationLoop reads it post-call).
        self._last_attempts = []
        attempts: list[AttemptRecord] = self._last_attempts
        # Image-workspace cascade: when the request carries an ImageContent
        # block, restrict + order the candidate walk to vision-capable
        # backends. Raises NoVisionCapableModelError if none qualify (honest
        # failure — never a silent text-only dispatch).
        candidates = self._vision_aware_candidates(messages)
        for backend in candidates:
            outcome = await self._try_backend_chat(
                backend,
                messages,
                tools=tools,
                temperature=temperature,
                max_tokens=max_tokens,
                stop=stop,
                top_p=top_p,
                top_k=top_k,
                attempts=attempts,
            )
            if outcome is not None:
                return outcome
        raise self._build_exhausted_error(attempts)

    # ------------------------------------------------------------------ #
    # ChatBackend Protocol — streaming chat.
    # ------------------------------------------------------------------ #

    def chat_stream(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.0,
        max_tokens: int = 4096,
        stop: list[str] | None = None,
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        """Walk backends per D-20-9 / D-20-10 / D-20-12; relay chunks.

        ``top_p`` / ``top_k`` are forwarded verbatim to each wrapped backend.

        Two-phase semantics (R9-033 refinement): once a backend's stream
        emits its first PAYLOAD chunk (non-whitespace ``delta`` or a
        ``tool_call_delta``) the wrapper is committed to that backend —
        partial output cannot be unstreamed. Errors raised AFTER that
        commit surface directly to the caller (no fallback). Errors raised
        BEFORE it follow the D-20-9 classifier (retry/fallback/surface),
        and a stream that completes cleanly WITHOUT ever emitting a payload
        chunk is an empty completion — a provider failure that engages the
        same retry-then-fallback walk (never a silent empty reply).
        """
        return self._chat_stream_walk(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            top_p=top_p,
            top_k=top_k,
        )

    async def _chat_stream_walk(
        self,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolSpec] | None,
        temperature: float,
        max_tokens: int,
        stop: list[str] | None,
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> AsyncIterator[StreamChunk]:
        # T19 — reset per-call ledger (see chat() for rationale).
        self._last_attempts = []
        attempts: list[AttemptRecord] = self._last_attempts
        # Image-workspace cascade — same vision-aware candidate restriction as
        # chat(); raises NoVisionCapableModelError BEFORE any stream opens.
        candidates = self._vision_aware_candidates(messages)
        for backend in candidates:
            retries_left = self._max_retries
            retried = False
            while True:
                # R9-033: commitment is now keyed on the first PAYLOAD chunk
                # (non-whitespace delta or a tool-call delta), not on the first
                # chunk of any kind. Payload-less chunks (usage-only finals,
                # reasoning-only deltas, whitespace) are held back until a
                # payload chunk commits the stream; a stream that completes
                # cleanly WITHOUT ever committing is an empty completion — a
                # provider failure that walks the same retry-then-fallback
                # path as any other transient ProviderError (the held-back
                # chunks are discarded: they carried no user-visible reply).
                committed = False
                pending: list[StreamChunk] = []
                try:
                    async for chunk in backend.chat_stream(
                        messages,
                        tools=tools,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        stop=stop,
                        top_p=top_p,
                        top_k=top_k,
                    ):
                        if not committed:
                            if not self._chunk_has_payload(chunk):
                                pending.append(chunk)
                                continue
                            committed = True
                            for held in pending:
                                yield held
                            pending.clear()
                        yield chunk
                    if committed:
                        # Stream completed cleanly — done with the whole wrapper.
                        return
                    raise EmptyCompletionError(
                        "backend stream completed with no content and no tool calls",
                        context={
                            "provider": backend.provider_name,
                            "model": backend.model_name,
                        },
                    )
                except Exception as exc:  # noqa: BLE001 — classifier branches below
                    if committed:
                        # Past the point of no return — surface verbatim.
                        self._record_attempt(backend, exc, retried, attempts)
                        raise
                    action = self._classify(exc)
                    if action == _SURFACE:
                        self._record_attempt(backend, exc, retried, attempts)
                        raise
                    if action == _RETRY_THEN_FALLBACK and retries_left > 0:
                        retries_left -= 1
                        retried = True
                        await asyncio.sleep(self._compute_retry_sleep(exc))
                        continue
                    self._log_fallback(backend, exc, action)
                    self._record_attempt(backend, exc, retried, attempts)
                    break  # advance to next backend
        raise self._build_exhausted_error(attempts)

    @staticmethod
    def _chunk_has_payload(chunk: StreamChunk) -> bool:
        """``True`` iff the chunk carries part of an actual reply (R9-033).

        Payload = non-whitespace text delta OR a tool-call delta. Usage-only
        finals, reasoning-only deltas, and whitespace do NOT commit the
        stream — a completion made solely of those is an empty completion.
        """
        return bool(chunk.delta.strip()) or chunk.tool_call_delta is not None

    # ------------------------------------------------------------------ #
    # Single-backend attempt loop (non-streaming).
    # ------------------------------------------------------------------ #

    async def _try_backend_chat(
        self,
        backend: ChatBackend,
        messages: list[ConversationMessage],
        *,
        tools: list[ToolSpec] | None,
        temperature: float,
        max_tokens: int,
        stop: list[str] | None,
        attempts: list[AttemptRecord],
        top_p: float | None = None,
        top_k: int | None = None,
    ) -> ChatResponse | None:
        """Try a single backend with D-20-10 bounded same-model retry.

        Returns:
            :class:`ChatResponse` on success; ``None`` when the wrapper
            should fall through to the next backend.

        Raises:
            The original exception: when the classifier bucketed it as
                SURFACE (no more backends tried).
        """
        retries_left = self._max_retries
        retried = False
        while True:
            try:
                response = await backend.chat(
                    messages,
                    tools=tools,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    stop=stop,
                    top_p=top_p,
                    top_k=top_k,
                )
                # R9-033: nothing-at-all is not a reply. A completion with no
                # non-whitespace text AND no tool calls is a provider failure —
                # raise into the classifier below so it walks the same
                # retry-then-fallback path as any other transient ProviderError.
                if not response.content.strip() and not response.tool_calls:
                    raise EmptyCompletionError(
                        "backend returned a completion with no content and no tool calls",
                        context={
                            "provider": backend.provider_name,
                            "model": backend.model_name,
                        },
                    )
                return response
            except Exception as exc:  # noqa: BLE001 — classifier branches below
                action = self._classify(exc)
                if action == _SURFACE:
                    self._record_attempt(backend, exc, retried, attempts)
                    raise
                if action == _RETRY_THEN_FALLBACK and retries_left > 0:
                    retries_left -= 1
                    retried = True
                    await asyncio.sleep(self._compute_retry_sleep(exc))
                    continue
                # FALLBACK-NO-RETRY, or RETRY-THEN-FALLBACK with budget gone.
                self._log_fallback(backend, exc, action)
                self._record_attempt(backend, exc, retried, attempts)
                return None

    # ------------------------------------------------------------------ #
    # Vision-aware candidate selection (image-workspace cascade).
    # ------------------------------------------------------------------ #

    @staticmethod
    def _request_has_image(messages: list[ConversationMessage]) -> bool:
        """Return ``True`` iff any message carries an :class:`ImageContent` block.

        A turn is a "vision request" when at least one message's list-form
        ``content`` holds an image reference. The text-only path (every
        message ``content`` is ``str``) returns ``False`` and skips all
        vision reordering — back-compat by construction.
        """
        return any(
            isinstance(m.content, list) and any(isinstance(b, ImageContent) for b in m.content)
            for m in messages
        )

    def _vision_aware_candidates(self, messages: list[ConversationMessage]) -> list[ChatBackend]:
        """Order the candidate walk for the request's modality.

        Text-only requests keep the configured order verbatim (criterion:
        first-candidate fallback is unchanged). Image-bearing requests are
        restricted to vision-capable candidates (in configured order) so an
        image turn on a ``[non-vision, vision]`` tier dispatches to the
        vision backend — and never silently lands text-only on a non-vision
        primary.

        Args:
            messages: The request messages about to be dispatched.

        Returns:
            The ordered list of candidate backends to walk.

        Raises:
            NoVisionCapableModelError: The request carries an image but no
                composed candidate reports ``supports_vision`` — the honest
                failure the runtime surfaces to the user.
        """
        if not self._request_has_image(messages):
            return self._backends
        vision_capable = [b for b in self._backends if bool(getattr(b, "supports_vision", False))]
        if not vision_capable:
            image_count = sum(
                sum(1 for b in m.content if isinstance(b, ImageContent))
                for m in messages
                if isinstance(m.content, list)
            )
            raise NoVisionCapableModelError(
                "no vision-capable model is configured in this tier",
                context={
                    "tier": self._tier_name or "",
                    "image_count": str(image_count),
                    "candidate_count": str(len(self._backends)),
                },
            )
        return vision_capable

    # ------------------------------------------------------------------ #
    # Classifier (D-20-9).
    # ------------------------------------------------------------------ #

    def _classify(self, exc: Exception) -> str:
        """Bucket an exception per the D-20-9 lock table.

        Maps to one of ``RETRY-THEN-FALLBACK`` / ``FALLBACK-NO-RETRY`` /
        ``SURFACE``. Non-:class:`PersonaError` exceptions (programmer bugs)
        always SURFACE — the wrapper never papers over them with fallback.
        """
        # Transient timeouts always retry-then-fallback.
        if isinstance(exc, BackendTimeoutError):
            return _RETRY_THEN_FALLBACK

        # R9-033: an empty completion (no non-whitespace text, no tool calls)
        # is a provider flake — retry the same model once, then fall through.
        # Never SURFACE: nothing-at-all must not flow onward as a reply.
        if isinstance(exc, EmptyCompletionError):
            return _RETRY_THEN_FALLBACK

        # Rate limits split by Retry-After cutoff + monetary-reason context.
        if isinstance(exc, RateLimitError):
            return self._classify_rate_limit(exc)

        # Image-workspace cascade: a vision-flagged candidate that still
        # REFUSES the image (e.g. missing workspace_root, or a model the
        # matrix marks vision but the provider rejects this image) must FALL
        # THROUGH to the next vision-capable candidate — never SURFACE as a
        # hard error that drops the turn. The candidate walk was already
        # restricted to vision-capable backends (_vision_aware_candidates);
        # if they all refuse, the wrapper exhausts to AllModelsFailedError.
        if isinstance(exc, BackendVisionNotSupportedError):
            return _FALLBACK_NO_RETRY

        # Auth / model-missing / model-unavailable / runtime cred-missing all
        # FALLBACK-NO-RETRY. (Auth covers D-20-12 cross-provider
        # SKIP-AND-FALLBACK case.) R9-073a: ModelUnavailableError (403
        # plan/entitlement denial, e.g. Cloudflare "not available on the
        # Workers Free plan") is permanent for THIS model — retrying it is
        # pointless — but the whole point of a multi-model tier is that the
        # operator listed alternates for exactly this case, so it skips
        # straight to the next backend rather than surfacing and killing the
        # turn.
        if isinstance(exc, AuthenticationError | ModelNotFoundError | ModelUnavailableError):
            return _FALLBACK_NO_RETRY
        if isinstance(exc, ProviderCredentialMissingError):
            return _FALLBACK_NO_RETRY

        # Generic ProviderError — split by HTTP status when known.
        if isinstance(exc, ProviderError):
            return self._classify_provider_error(exc)

        # Anything else (non-PersonaError programmer bug) → surface verbatim.
        return _SURFACE

    @staticmethod
    def _classify_rate_limit(exc: RateLimitError) -> str:
        """D-20-9 row 3 — Retry-After > 2s OR monetary reason → FALLBACK-NO-RETRY."""
        ctx = exc.context
        reason = ctx.get("reason", "")
        if reason in {"insufficient_quota", "credits_expired"}:
            return _FALLBACK_NO_RETRY
        retry_after_raw = ctx.get("retry_after_s", "")
        if retry_after_raw:
            try:
                if float(retry_after_raw) > _RATE_LIMIT_RETRY_AFTER_CUTOFF_S:
                    return _FALLBACK_NO_RETRY
            except ValueError:
                # Malformed header — treat as missing; default to retry path.
                pass
        return _RETRY_THEN_FALLBACK

    @staticmethod
    def _classify_provider_error(exc: ProviderError) -> str:
        """D-20-9 ProviderError split — status / content-policy aware."""
        ctx = exc.context
        # SURFACE-bucket overrides: explicit content policy & 422 validation.
        if ctx.get("reason") == "content_policy_violation":
            return _SURFACE
        status_raw = ctx.get("status_code", "")
        if status_raw:
            try:
                status = int(status_raw)
            except ValueError:
                return _SURFACE
            if status in _TRANSIENT_STATUS_CODES:
                return _RETRY_THEN_FALLBACK
            # 400 generic / 413 payload-too-large / 422 validation → SURFACE.
            return _SURFACE
        # ProviderError with no status_code context — conservative SURFACE.
        return _SURFACE

    # ------------------------------------------------------------------ #
    # Logging + bookkeeping.
    # ------------------------------------------------------------------ #

    def _log_fallback(self, backend: ChatBackend, exc: Exception, action: str) -> None:
        """Emit a structured WARNING per D-20-12 (auth) + D-20-15 (cred-missing).

        All fallback events log at WARNING — the silent-mask risk is the
        whole reason D-20-12 chose SKIP-AND-FALLBACK over surface-immediately.
        """
        _LOG.warning(
            "multi_model fallback engaged",
            tier=self._tier_name or "",
            provider=backend.provider_name,
            model=backend.model_name,
            action=action,
            error_class=type(exc).__name__,
            error_message=str(exc),
        )

    @staticmethod
    def _record_attempt(
        backend: ChatBackend,
        exc: Exception,
        retried: bool,
        attempts: list[AttemptRecord],
    ) -> None:
        """Append a frozen :class:`AttemptRecord` summarising this backend's outcome."""
        status: int | None = None
        if isinstance(exc, PersonaError):
            raw = exc.context.get("status_code", "")
            if raw:
                try:
                    status = int(raw)
                except ValueError:
                    status = None
        attempts.append(
            AttemptRecord(
                provider=backend.provider_name,
                model=backend.model_name,
                last_error_class=type(exc).__name__,
                last_error_status_code=status,
                retried_same_model=retried,
            )
        )

    def _build_exhausted_error(self, attempts: list[AttemptRecord]) -> AllModelsFailedError:
        """Construct the terminal :class:`AllModelsFailedError` (D-20-16)."""
        final_cls = attempts[-1].last_error_class if attempts else ""
        return AllModelsFailedError(
            "every backend in MultiModelChatBackend exhausted",
            context={
                "tier": self._tier_name or "",
                "attempt_count": str(len(attempts)),
                "attempts_json": str([asdict(a) for a in attempts]),
                "final_error_class": final_cls,
            },
        )

    # ------------------------------------------------------------------ #
    # Retry sleep computation (D-20-10).
    # ------------------------------------------------------------------ #

    @staticmethod
    def _compute_retry_sleep(exc: Exception) -> float:
        """D-20-10 — 200ms baseline + jittered ±50%; honour short Retry-After.

        If the exception is a :class:`RateLimitError` carrying a
        ``retry_after_s`` ≤ the cutoff, the provider's hint is honoured
        verbatim (still jittered ±50% to avoid thundering-herd alignment
        across colocated workers). Otherwise the baseline applies.
        """
        base = _RETRY_BASE_SLEEP_S
        if isinstance(exc, RateLimitError):
            raw = exc.context.get("retry_after_s", "")
            if raw:
                try:
                    hinted = float(raw)
                    if 0.0 < hinted <= _RATE_LIMIT_RETRY_AFTER_CUTOFF_S:
                        base = hinted
                except ValueError:
                    pass
        jitter = base * _RETRY_JITTER_FRAC * (2.0 * random.random() - 1.0)  # noqa: S311 — not crypto
        return max(0.0, base + jitter)
