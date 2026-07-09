"""MultiModelImageBackend — cross-provider ordered fallback wrapper (Spec 20 T16).

Symmetric to :class:`persona.backends.multi_model.MultiModelChatBackend` but
for the Spec 15 :class:`persona.imagegen.protocol.ImageBackend` Protocol.
Composes N concrete :class:`ImageBackend` instances (e.g.
:class:`persona.imagegen.openai_image.OpenAIImageBackend`,
:class:`persona.imagegen.fal_image.FalImageBackend`, and the T10 NVIDIA
backend) and dispatches in declared order, falling back per the D-20-9
three-bucket error categorization adapted for the Spec 15 error hierarchy.

Key invariants:

* **D-20-9 / Spec 15 invariant** — :class:`ContentRejectedError` SURFACES
  immediately and is **never** cross-provider laundered. A category-policy
  refusal at provider A reflects on the prompt (not on provider A's
  capacity); fallback would mask the refusal across vendors. This is the
  decisive Spec-15-specific divergence from the chat wrapper.
* **D-20-10** — N=1 same-model retry on the RETRY-THEN-FALLBACK bucket
  with a 200ms + jittered ±50% sleep. Underlying SDK retries are disabled
  one layer down (each concrete backend constructs its SDK client with
  ``max_retries=0`` per Spec 02 D-02-* discipline).
* **D-20-12** — Cross-provider :class:`AuthenticationError` →
  FALLBACK-NO-RETRY with a structured WARNING (provider A's bad key
  tells nothing about provider B's key).
* **D-20-14** — :meth:`MultiModelImageBackend.generate` is atomic: it
  returns a complete :class:`GenerationResult` from the first successful
  backend OR raises :class:`AllModelsFailedError` once every backend has
  been tried. No partial-progress splicing across providers (NVIDIA NIM
  is one-shot per R-20-7; fal.ai is one-shot; OpenAI's ``partial_images``
  preview frames are unsuitable as final).
* **D-20-15** — A ``ProviderCredentialMissingError`` raised at call time
  (rare — these are normally caught at TierRegistry construction) is
  treated as FALLBACK-NO-RETRY.

References:
    docs/specs/phase2/spec_20/decisions.md D-20-9 / D-20-10 / D-20-12 /
    D-20-14 / D-20-15 / D-20-16; docs/specs/phase2/spec_20/research.md
    §R-20-7 (atomic generate rationale).
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Final

from persona.backends.errors import AllModelsFailedError  # Spec 20 D-20-16 — shared with T15
from persona.errors import AuthenticationError, PersonaError
from persona.imagegen.errors import (
    ContentRejectedError,
    ImageGenUnavailableError,
    ImageProviderError,
)
from persona.logging import get_logger, redact_secrets

if TYPE_CHECKING:
    from persona.imagegen.protocol import ImageBackend
    from persona.imagegen.result import GeneratedImage, GenerationResult, ImageGenOptions

__all__ = ["AllModelsFailedError", "AttemptRecord", "MultiModelImageBackend"]


_LOG = get_logger("persona.imagegen.multi_model_image")

# D-20-10 lock — one bounded same-model retry then fallback.
_DEFAULT_MAX_RETRIES_PER_BACKEND: Final[int] = 1
# D-20-9 row 3 — Retry-After > 2s skips N=1 and falls back immediately.
_RATE_LIMIT_RETRY_AFTER_CUTOFF_S: Final[float] = 2.0
# D-20-10 — 200ms base sleep with ±50% jitter on the same-model retry.
_RETRY_BASE_SLEEP_S: Final[float] = 0.2
_RETRY_JITTER_FRAC: Final[float] = 0.5

# Spec 15 ``ImageProviderError`` reasons that map to FALLBACK-NO-RETRY
# per D-20-9 row 4 (the "exhausted / structurally-bad" reasons — retry
# would re-hit the same wall on the same backend).
_FALLBACK_NO_RETRY_REASONS: frozenset[str] = frozenset(
    {
        "quota_exhausted",
        "credits_expired",
        "insufficient_quota",
        "model_not_found",
        "unsupported_model",
        "unsupported_option",
        "non_commercial_license",
        "bad_request",
    }
)

# Reasons that map to RETRY-THEN-FALLBACK per D-20-9 row 2. Anything else
# in the ``ImageProviderError`` ``reason`` discriminator falls back to
# FALLBACK-NO-RETRY (fail-closed: we never invent a retry for an
# unrecognised reason; that's safer than a silent retry loop).
_RETRY_THEN_FALLBACK_REASONS: frozenset[str] = frozenset(
    {
        "rate_limit",
        "timeout",
        "transient",
    }
)


@dataclass(frozen=True)
class AttemptRecord:
    """Per-backend attempt outcome for :class:`AllModelsFailedError` context.

    Mirrors the chat wrapper's :class:`AttemptRecord` (Spec 20 T15) so a
    future cross-wrapper consolidation slots in without redesign. T17 will
    decide whether to keep two structurally-identical classes or unify in
    a shared module.

    Attributes:
        provider: Concrete backend's ``provider_name``.
        model: Concrete backend's ``model_name``.
        last_error_class: ``type(exc).__name__`` of the failing exception.
        last_error_reason: ``exc.context.get("reason", "")`` — the
            Spec 15 ``ImageProviderError`` discriminator the classifier
            branched on (empty for non-:class:`PersonaError` failures).
        last_error_message: ``redact_secrets(str(exc))`` — the provider's
            own phrasing of *why* the backend failed (R9-015), so the
            dead-letter ``last_error`` distinguishes a bad model id from an
            auth failure from a rate-limit. Credential-scrubbed at the
            record boundary; never carries a raw key.
        retried_same_model: Whether a same-model retry was attempted
            before fallback (D-20-10 N=1 instrumentation).
    """

    provider: str
    model: str
    last_error_class: str
    last_error_reason: str
    last_error_message: str
    retried_same_model: bool


# Note: AllModelsFailedError lives in persona.backends.errors (T15) and is
# imported above. Per D-20-16 the wrapper-layer error class is shared between
# MultiModelChatBackend and MultiModelImageBackend — both compose backends
# and a cross-backend exhaustion has identical semantics regardless of which
# Protocol the wrapper implements. T16's image-gen AttemptRecord carries an
# image-specific ``last_error_reason`` discriminator (e.g., "credits_expired",
# "non_commercial_license") absent on T15's chat AttemptRecord.


class MultiModelImageBackend:
    """Ordered cross-provider fallback wrapper over N :class:`ImageBackend` instances.

    Implements :class:`persona.imagegen.protocol.ImageBackend` verbatim —
    callers don't know about the wrapper. The Spec 15 :meth:`edit`
    method is reserved per D-15-X-edit-protocol-reservation; the wrapper
    delegates to the Protocol default which raises
    :class:`NotImplementedError` (no v1 backend overrides ``edit``, so
    there's nothing to fall back across).

    Construction invariant: ``backends`` length >= 1.

    Error policy at call time per D-20-9 (Spec 15 adaptation):

    * :class:`ContentRejectedError` → **SURFACE** (Spec 15 invariant;
      NEVER falls back). A category refusal at provider A is a refusal
      against the prompt, not against provider A's availability —
      laundering across vendors would mask content-policy violations.
    * :class:`ImageProviderError` with ``reason ∈ {rate_limit, timeout,
      transient}`` → **RETRY-THEN-FALLBACK** (N=1 same-model with
      jittered sleep per D-20-10, then advance).
    * :class:`ImageProviderError` with ``reason ∈ {quota_exhausted,
      credits_expired, insufficient_quota, model_not_found,
      unsupported_model, unsupported_option, non_commercial_license,
      bad_request}`` → **FALLBACK-NO-RETRY** (retrying re-hits the same
      structural wall).
    * :class:`AuthenticationError` /
      :class:`ImageGenUnavailableError` → **FALLBACK-NO-RETRY** with a
      structured WARNING (D-20-12 — explicitly opposite of the
      LiteLLM/Portkey surface-immediately default; BYOK shape).
    * ``ProviderCredentialMissingError`` (raised at call time) →
      **FALLBACK-NO-RETRY** (D-20-15 — normally caught at construction;
      defensive at runtime).
    * Anything else (non-:class:`PersonaError`) → **SURFACE** (programmer
      bug; never silently fall back through a bug).

    Per D-20-9 row 3, an :class:`ImageProviderError` ``reason="rate_limit"``
    carrying ``context["retry_after_s"]`` > 2s skips the N=1 retry and
    falls back immediately. The cutoff is conservative — interactive UX
    cannot afford a 2s+ pause when another provider could already be
    serving.

    Per D-20-14, :meth:`generate` is atomic. The wrapper never returns a
    partial :class:`GenerationResult` — it either returns the complete
    result from backend ``k`` or raises :class:`AllModelsFailedError`
    after every backend has been tried.
    """

    def __init__(
        self,
        backends: list[ImageBackend],
        *,
        tier_name: str | None = None,
        max_retries_per_backend: int = _DEFAULT_MAX_RETRIES_PER_BACKEND,
    ) -> None:
        """Construct the wrapper over N concrete :class:`ImageBackend` instances.

        Args:
            backends: Ordered list of concrete backends. The wrapper tries
                them left-to-right on every :meth:`generate` call. Must
                contain at least one backend.
            tier_name: Optional name surfaced in
                :class:`AllModelsFailedError` ``context["tier"]`` and
                fallback WARNING logs. Defaults to ``"imagegen"`` when
                rendered if omitted.
            max_retries_per_backend: D-20-10 knob. Defaults to N=1.
                Setting to 0 disables same-model retry entirely (matches
                OpenRouter/aisuite default); setting >1 doubles
                worst-case latency on hard failures and is discouraged.

        Raises:
            ValueError: ``backends`` is empty.
        """
        if not backends:
            raise ValueError("MultiModelImageBackend requires at least one backend")
        self._backends: list[ImageBackend] = list(backends)
        self._tier_name: str | None = tier_name
        self._max_retries: int = max_retries_per_backend

    # ------------------------------------------------------------------
    # ImageBackend Protocol — properties
    # ------------------------------------------------------------------

    @property
    def tier_name(self) -> str | None:
        """Configured tier label passed at construction (read-only).

        T17 wiring uses this to surface the tier in operator logs / TurnLog
        ``tier_fallback_*`` fields without reaching into private state.
        """
        return self._tier_name

    async def edit(
        self,
        input_image: GeneratedImage,
        instructions: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        """Reserved per Spec 15 D-15-X-edit-protocol-reservation.

        No v1 backend overrides ``edit``; nothing to fall back across, so
        the wrapper raises :class:`NotImplementedError` directly. If a
        future ImageBackend implements ``edit``, this wrapper grows the
        same fallback discipline as :meth:`generate`.
        """
        msg = (
            "edit() is reserved for v1.x per D-15-X-edit-protocol-reservation; "
            "no v1 ImageBackend overrides it, so MultiModelImageBackend has "
            "nothing to compose a fallback chain over."
        )
        raise NotImplementedError(msg)

    @property
    def backends(self) -> list[ImageBackend]:
        """Composed backends in fallback order (read-only view).

        T17 wiring + tests read this to introspect the chain; returns the
        live list (callers MUST NOT mutate — wrap in tuple if needed).
        """
        return self._backends

    @property
    def provider_name(self) -> str:
        """Provider name of the *primary* backend.

        The wrapper does not invent a new identifier; the head of the
        chain is what the caller asked for, and observability layers
        report the *actually-used* provider via the
        :class:`GenerationResult.provider` field (which the active
        concrete backend populates).
        """
        return self._backends[0].provider_name

    @property
    def model_name(self) -> str:
        """Model name of the primary backend (same rationale as :attr:`provider_name`)."""
        return self._backends[0].model_name

    # ------------------------------------------------------------------
    # ImageBackend Protocol — methods
    # ------------------------------------------------------------------

    async def generate(
        self,
        prompt: str,
        *,
        options: ImageGenOptions | None = None,
    ) -> GenerationResult:
        """Atomic cross-provider image generation per D-20-14.

        Walks the configured backends left-to-right. Each backend is
        given one bounded same-model retry on the RETRY-THEN-FALLBACK
        bucket per D-20-10, then the wrapper advances. A successful call
        returns the concrete backend's :class:`GenerationResult` verbatim.

        Args:
            prompt: Already-merged prompt text (the Spec 15 T11 visual-
                style merge happens upstream).
            options: Neutral generation knobs; ``None`` means a default
                :class:`ImageGenOptions`.

        Returns:
            A complete :class:`GenerationResult` from the first backend
            that succeeded. Never partial (D-20-14).

        Raises:
            ContentRejectedError: Any backend's provider moderation
                refused the prompt or generated output. NEVER cross-
                provider laundered (Spec 15 invariant + D-20-9 SURFACE
                bucket).
            AllModelsFailedError: Every backend has been tried and none
                produced a result.
        """
        attempts: list[AttemptRecord] = []
        for backend in self._backends:
            outcome = await self._try_backend(backend, prompt, options, attempts)
            if outcome is not None:
                return outcome
        # Every backend exhausted — raise structured aggregate. Per R9-015
        # the aggregate MUST carry the per-backend causes (class + redacted
        # message) so the dead-letter ``last_error`` is diagnosable — an
        # empty "everything failed" is what left the owner blind. The
        # ``attempts`` list (asdict) now includes ``last_error_message``;
        # ``final_error_class`` / ``final_error`` hoist the terminal cause.
        final_class = attempts[-1].last_error_class if attempts else ""
        final_error = attempts[-1].last_error_message if attempts else ""
        raise AllModelsFailedError(
            "every backend in MultiModelImageBackend exhausted",
            context={
                "tier": self._tier_name or "imagegen",
                "attempt_count": str(len(attempts)),
                "attempts": str([asdict(a) for a in attempts]),
                "final_error_class": final_class,
                "final_error": final_error,
            },
        )

    # ------------------------------------------------------------------
    # Internal — per-backend dispatch loop
    # ------------------------------------------------------------------

    async def _try_backend(
        self,
        backend: ImageBackend,
        prompt: str,
        options: ImageGenOptions | None,
        attempts: list[AttemptRecord],
    ) -> GenerationResult | None:
        """Try a single backend with bounded same-model retry per D-20-10.

        Returns the :class:`GenerationResult` on success, ``None`` when
        the classifier said FALLBACK (caller advances), and re-raises
        when the classifier said SURFACE.
        """
        retries_left = self._max_retries
        retried = False
        while True:
            try:
                return await backend.generate(prompt, options=options)
            except ContentRejectedError as exc:
                # Spec 15 invariant — never fall back through a content
                # rejection. SURFACE bucket per D-20-9.
                self._record_attempt(backend, exc, retried, attempts)
                raise
            except Exception as exc:  # noqa: BLE001 — adapter boundary classifier
                action = self._classify(exc)
                if action == "SURFACE":
                    self._record_attempt(backend, exc, retried, attempts)
                    raise
                if action == "RETRY-THEN-FALLBACK" and retries_left > 0:
                    retries_left -= 1
                    retried = True
                    sleep_s = self._compute_retry_sleep(exc)
                    _LOG.debug(
                        "multi_model_image retry",
                        provider=backend.provider_name,
                        model=backend.model_name,
                        error_class=type(exc).__name__,
                        sleep_s=sleep_s,
                        tier=self._tier_name or "imagegen",
                    )
                    await asyncio.sleep(sleep_s)
                    continue
                # FALLBACK (with or without exhausted retry) — log, record, advance.
                self._log_fallback(backend, exc, action)
                self._record_attempt(backend, exc, retried, attempts)
                return None

    # ------------------------------------------------------------------
    # Internal — classifier
    # ------------------------------------------------------------------

    def _classify(self, exc: Exception) -> str:
        """Bucket an exception per D-20-9 adapted for Spec 15 error hierarchy.

        ``ContentRejectedError`` is intentionally handled *before* this
        method is called (see :meth:`_try_backend`) so the SURFACE
        invariant is enforced at the call site — the classifier here is
        a defensive net.

        Returns one of:

        * ``"SURFACE"`` — caller re-raises (non-PersonaError programmer
          bugs, or :class:`ContentRejectedError` defensively).
        * ``"RETRY-THEN-FALLBACK"`` — caller honours D-20-10 N=1 retry.
        * ``"FALLBACK-NO-RETRY"`` — caller advances to next backend.
        """
        # Defensive: ContentRejectedError ALWAYS surfaces. The call site
        # already handles this in a dedicated except; the duplicate guard
        # here ensures a future refactor cannot accidentally relax the
        # invariant by removing the dedicated except.
        if isinstance(exc, ContentRejectedError):
            return "SURFACE"

        # D-20-12: cross-provider AuthenticationError → FALLBACK-NO-RETRY.
        # Spec 15's concrete backends map provider 401/403 to
        # ImageGenUnavailableError (see openai_image.py:419); also
        # accept the core AuthenticationError type for symmetry with the
        # chat wrapper (Spec 02 backends raise that one directly).
        if isinstance(exc, AuthenticationError | ImageGenUnavailableError):
            return "FALLBACK-NO-RETRY"

        # D-20-15: ProviderCredentialMissingError (T11) at call time →
        # FALLBACK-NO-RETRY. Detected by class name to avoid a hard
        # import dependency on the parallel-task module (T17 wires this
        # up; this method picks the class up automatically once it
        # lands in the import graph).
        if isinstance(exc, PersonaError) and type(exc).__name__ == "ProviderCredentialMissingError":
            return "FALLBACK-NO-RETRY"

        if isinstance(exc, ImageProviderError):
            reason = exc.context.get("reason", "")
            if reason in _RETRY_THEN_FALLBACK_REASONS:
                # D-20-9 row 3 — Retry-After > 2s skips N=1 retry.
                if reason == "rate_limit":
                    retry_after = _parse_retry_after_s(exc.context.get("retry_after_s"))
                    if retry_after is not None and retry_after > _RATE_LIMIT_RETRY_AFTER_CUTOFF_S:
                        return "FALLBACK-NO-RETRY"
                return "RETRY-THEN-FALLBACK"
            if reason in _FALLBACK_NO_RETRY_REASONS:
                return "FALLBACK-NO-RETRY"
            # Unrecognised reason: fail-closed to FALLBACK-NO-RETRY. We
            # never invent a retry for a reason we did not anticipate;
            # the safer default is "let the next backend try".
            return "FALLBACK-NO-RETRY"

        # Any other PersonaError — fall back (something domain-shaped
        # went wrong; advance rather than silently retry).
        if isinstance(exc, PersonaError):
            return "FALLBACK-NO-RETRY"

        # Non-PersonaError → SURFACE. A bare ValueError / TypeError from
        # below the boundary is a programmer bug; silently falling back
        # would mask it.
        return "SURFACE"

    # ------------------------------------------------------------------
    # Internal — logging + record + sleep
    # ------------------------------------------------------------------

    def _log_fallback(
        self,
        backend: ImageBackend,
        exc: Exception,
        action: str,
    ) -> None:
        """Emit a structured WARNING when a backend is skipped per D-20-12 shape.

        Carries the redacted provider error MESSAGE (R9-015) so a bad model
        id / auth failure / endpoint error / rate-limit are distinguishable in
        the log — ``error_class`` + ``reason`` alone rendered them identical.
        The message is credential-scrubbed via :func:`redact_secrets`.
        """
        context = exc.context if isinstance(exc, PersonaError) else {}
        _LOG.warning(
            "multi_model_image fallback",
            provider=backend.provider_name,
            model=backend.model_name,
            error_class=type(exc).__name__,
            reason=context.get("reason", ""),
            error=redact_secrets(str(exc)),
            action=action,
            tier=self._tier_name or "imagegen",
        )

    def _record_attempt(
        self,
        backend: ImageBackend,
        exc: Exception,
        retried: bool,
        attempts: list[AttemptRecord],
    ) -> None:
        """Append an :class:`AttemptRecord` for ``AllModelsFailedError`` context."""
        reason = ""
        if isinstance(exc, PersonaError):
            reason = exc.context.get("reason", "")
        attempts.append(
            AttemptRecord(
                provider=backend.provider_name,
                model=backend.model_name,
                last_error_class=type(exc).__name__,
                last_error_reason=reason,
                # R9-015: capture the redacted provider message so the
                # aggregated AllModelsFailedError (→ dead-letter last_error)
                # carries *why* each backend failed, not just its class.
                last_error_message=redact_secrets(str(exc)),
                retried_same_model=retried,
            )
        )

    def _compute_retry_sleep(self, exc: Exception) -> float:
        """Return the D-20-10 sleep duration with ±50% jitter.

        Honours ``ImageProviderError(reason="rate_limit",
        retry_after_s=<value>)`` when present and within the cutoff: the
        wire-provided value wins over the synthetic base. Otherwise
        falls back to ``_RETRY_BASE_SLEEP_S`` with jitter.
        """
        if isinstance(exc, ImageProviderError) and exc.context.get("reason") == "rate_limit":
            retry_after = _parse_retry_after_s(exc.context.get("retry_after_s"))
            if retry_after is not None and 0.0 < retry_after <= _RATE_LIMIT_RETRY_AFTER_CUTOFF_S:
                return retry_after
        jitter = random.uniform(-_RETRY_JITTER_FRAC, _RETRY_JITTER_FRAC)
        return _RETRY_BASE_SLEEP_S * (1.0 + jitter)


def _parse_retry_after_s(value: str | None) -> float | None:
    """Parse a ``retry_after_s`` context value into a float, tolerantly.

    Provider adapters store the header as a string per D-02-8 (we never
    invent a default). The classifier needs a number to compare against
    the cutoff — this helper does the safe parse.
    """
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None
