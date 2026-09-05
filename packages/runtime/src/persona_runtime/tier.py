"""Tier configuration and the backend registry (T03; D-05-3, D-05-4).

The router (T04) picks a tier name — ``"frontier"``, ``"mid"``, or
``"small"`` — and the :class:`TierRegistry` turns that into a concrete
:class:`persona.backends.ChatBackend`. Backends are instantiated lazily (on
first :meth:`TierRegistry.get`) and cached, because each holds a client
(``httpx`` for Ollama, an SDK client for hosted providers) that we don't want
to open for tiers a persona never uses.

Fallback (D-05-3): an unconfigured tier falls back ``small → mid → frontier``.
If no tiers are configured at all, the registry serves a single backend built
from the default ``PERSONA_*`` env (spec-02 ``BackendConfig`` defaults) for
every tier name. If nothing resolves, :class:`TierNotConfiguredError`.

Lifecycle (D-05-4): :meth:`aclose` disconnects every *cached* backend. The
**composition root** (the API in spec 08, or the integration-test fixtures)
owns this — the :class:`~persona_runtime.loop.ConversationLoop` uses the
registry but never closes it.

Spec 18 (T04; D-18-3, D-18-X-protocol-location, D-18-X-latency-measurement-source):
adds optional per-tier :class:`TierMetadata` (cost / first-token latency /
throughput / context window / tool strength) at the **registry layer** —
NOT on the :class:`ChatBackend` Protocol. The router (Spec 18 ``UnifiedRouter``)
reads it; the backend never does. Existing :class:`TierConfig` constructions
stay valid (the metadata field defaults to ``None``); existing callers of
:func:`tier_registry_from_env` stay green (metadata population is opt-in via
new env vars).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Literal

from persona.backends import BackendConfig, load_backend, vision_supported
from persona.backends.credentials import (
    ProviderCredentialResolver,
    TierResolution,
    filter_openrouter_free_mode,
    parse_models_list,
    resolve_tier_config,
)
from persona.backends.errors import (
    LocalProviderInModelsListError,
    MalformedTierModelsError,
    ProviderCredentialMissingError,
)
from persona.backends.errors import (
    TierNotConfiguredError as ModelsListTierNotConfiguredError,
)
from persona.backends.multi_model import MultiModelChatBackend
from persona.backends.retired import filter_retired_models
from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict, Field

from persona_runtime.errors import TierNotConfiguredError

if TYPE_CHECKING:
    from persona.backends import ChatBackend
    from persona.backends.openrouter_catalog import OpenRouterSubscriptionMode

__all__ = [
    "TierConfig",
    "TierMetadata",
    "TierRegistry",
    "free_tier_registry_from_env",
    "tier_metadata_from_env",
    "tier_registry_from_env",
]

_logger = get_logger("runtime.tier")

# Resolution order for fallback when a requested tier is unconfigured (D-05-3).
_FALLBACK_ORDER: tuple[str, ...] = ("small", "mid", "frontier")

# The env-var prefixes the from-env builder reads (one per tier).
_TIER_ENV_PREFIXES: dict[str, str] = {
    "frontier": "PERSONA_FRONTIER_",
    "mid": "PERSONA_MID_",
    "small": "PERSONA_SMALL_",
}

# Spec M4 (T5a) — the FREE plan's dedicated model-set env prefixes. A SEPARATE,
# free-only registry is built from these (``free_tier_registry_from_env``); a free
# user's whole tier chain comes from here, never the paid ``PERSONA_<TIER>_`` vars.
_FREE_TIER_ENV_PREFIXES: dict[str, str] = {
    "frontier": "PERSONA_FREE_FRONTIER_",
    "mid": "PERSONA_FREE_MID_",
    "small": "PERSONA_FREE_SMALL_",
}


class TierMetadata(BaseModel):
    """Per-tier routing metadata (Spec 18 T04; D-18-3, D-18-X-protocol-location).

    Frozen Pydantic v2 + ``extra="forbid"``. Lives at the :class:`TierRegistry`
    layer, **NOT** on the :class:`~persona.backends.ChatBackend` Protocol
    (D-18-X-protocol-location). The :class:`UnifiedRouter` (Spec 18 T09–T11)
    reads it via :meth:`TierRegistry.metadata_for`; the backend never does.

    The :class:`HeuristicRouter` path (Spec 05 rules) does NOT consult metadata
    — it operates on context signals + capability matrices only. Metadata is
    populated, sparse, and additive; absence is handled per
    D-18-X-partial-metadata-behaviour (tiers without metadata are excluded
    from Layer 2 scoring; fall back to :class:`HeuristicRouter` if filtered
    set is empty).

    V5 R-V5-1 coordination: V5 reads :attr:`first_token_latency_ms` from this
    registry. One measurement, two consumers (D-18-X-latency-measurement-source).

    Attributes:
        cost_input_per_1k_tokens: Provider cost per 1k INPUT tokens (cents).
        cost_output_per_1k_tokens: Provider cost per 1k OUTPUT tokens (cents).
        first_token_latency_ms: Mean first-token latency (milliseconds). Seed
            from provider docs or env; the in-band
            :class:`FirstTokenLatencyTracker` (T06) updates the value via EWMA
            once warmed up.
        throughput_tokens_per_sec: Mean throughput (tokens/sec) after first
            token. Provider-documented or empirical.
        context_window: Maximum context window the backend supports (tokens).
            Drives Layer 1's context-window constraint when the turn's
            ``estimated_input_tokens`` exceeds the window.
        tool_strength: Categorical strength of the model's native tool-calling.
            Drives Layer 1's strong-tools constraint when the turn's
            ``requires_strong_tools`` is ``True``. ``"weak"`` excludes the tier
            in that case; ``"medium"`` / ``"strong"`` keep it.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    cost_input_per_1k_tokens: float = Field(ge=0.0)
    cost_output_per_1k_tokens: float = Field(ge=0.0)
    first_token_latency_ms: float = Field(ge=0.0)
    throughput_tokens_per_sec: float = Field(ge=0.0)
    context_window: int = Field(gt=0)
    tool_strength: Literal["weak", "medium", "strong"]
    # Spec 20 T14 additive — D-18-5 quality-proxy boost on hard turns.
    reasoning_capable: bool = False
    # Spec 20 T14 additive — D-13-3 verify-at-deploy precedent. False signals
    # operator that cost values are best-estimate (e.g., NVIDIA hosted catalog
    # — no public $/Mtok per R-20-4). Scorer skips cost weighting when False.
    cost_verified_at_deploy: bool = True


@dataclass(frozen=True)
class TierConfig:
    """How to instantiate the backend for one tier (spec §6.2).

    Spec 18 (T04) adds optional :attr:`metadata` carrying cost / first-token
    latency / throughput / context window / tool strength for the Spec 18
    :class:`UnifiedRouter`. Existing constructions stay valid — the field
    defaults to ``None`` (no metadata; :class:`UnifiedRouter` excludes the
    tier from Layer 2 scoring per D-18-X-partial-metadata-behaviour).

    Attributes:
        name: Tier name — ``"frontier"``, ``"mid"``, or ``"small"``.
        backend_config: The :class:`persona.backends.BackendConfig` passed to
            :func:`persona.backends.load_backend` on first use.
        metadata: Optional :class:`TierMetadata` for the Spec 18 router. When
            ``None``, the tier is invisible to :class:`UnifiedRouter`'s Layer 2
            scorer (the heuristic floor still serves the tier — fallback path).
        preconstructed_backend: Optional pre-built backend instance. Spec 20
            T17 (D-20-17): when the tier is configured via
            ``PERSONA_<TIER>_MODELS`` the :class:`MultiModelChatBackend`
            wrapper is constructed up-front at registry-build time (the
            wrapper holds N concrete backends), so :meth:`TierRegistry.get`
            returns this instance directly and skips :func:`load_backend`.
            ``None`` (the default) means the legacy lazy path
            via ``backend_config`` applies.
    """

    name: str
    backend_config: BackendConfig
    metadata: TierMetadata | None = field(default=None)
    preconstructed_backend: ChatBackend | None = field(default=None)


class TierRegistry:
    """Resolves tier names to backends; lazy-instantiates and caches.

    Args:
        tiers: Mapping of tier name to :class:`TierConfig`. May be partial
            (only the tiers the deployment configured). An empty mapping is
            valid only if a single-backend fallback was baked in by the
            builder; otherwise :meth:`get` raises.
    """

    def __init__(self, tiers: dict[str, TierConfig]) -> None:
        self._tiers = dict(tiers)
        self._cache: dict[str, ChatBackend] = {}
        # Spec 20 T17 (D-20-17): tiers configured via MODELS-list arrive with a
        # preconstructed wrapper (or bare-single backend post credential
        # resolution). Seed the cache so `.get()` returns the same instance
        # without ever calling :func:`load_backend` for that tier.
        for tier_name, tier_config in self._tiers.items():
            if tier_config.preconstructed_backend is not None:
                self._cache[tier_name] = tier_config.preconstructed_backend

    @property
    def configured_tier_names(self) -> tuple[str, ...]:
        """Names of every tier that has a :class:`TierConfig` registered.

        Insertion order is preserved so callers (e.g., the router's vision
        pre-filter, T13-T09) can produce stable error context strings.
        """
        return tuple(self._tiers)

    def model_name_for(self, tier_name: str) -> str:
        """Return the configured model name for ``tier_name`` without instantiating.

        Spec 18 (T05). Read-only — does NOT trigger backend instantiation.
        Used by :class:`HeuristicRouter.route` and :class:`UnifiedRouter.route`
        to populate :attr:`RoutingDecision.model` without forcing eager
        client construction.

        Args:
            tier_name: The tier name to look up.

        Returns:
            The configured model name string (e.g. ``"claude-sonnet-4-6"``).
            Resolves through the same fallback chain as :meth:`get`.

        Raises:
            TierNotConfiguredError: No tier resolves, even after fallback.
        """
        effective = self._resolve(tier_name)
        return self._tiers[effective].backend_config.model

    def metadata_for(self, tier_name: str) -> TierMetadata | None:
        """Return :class:`TierMetadata` for ``tier_name``, or ``None`` if unset.

        Spec 18 (T04). Resolves through the same fallback chain as :meth:`get`,
        so a request for an unconfigured tier consults the fallback's metadata
        — but unlike :meth:`get`, this method does NOT instantiate the backend
        (metadata lookup is read-only and never triggers backend construction).

        Args:
            tier_name: The tier name to look up.

        Returns:
            The :class:`TierMetadata` instance configured for the effective
            tier, or ``None`` when neither the requested tier nor its fallback
            has metadata populated. ``None`` is the signal Spec 18's
            ``UnifiedRouter`` reads to exclude the tier from Layer 2 scoring
            (D-18-X-partial-metadata-behaviour); the heuristic floor still
            serves the tier on the fallback path (D-18-4).

        Raises:
            TierNotConfiguredError: No tier resolves, even after fallback.
        """
        effective = self._resolve(tier_name)
        return self._tiers[effective].metadata

    def candidate_models_for(self, tier_name: str) -> tuple[tuple[str, str], ...]:
        """Return the tier's candidate ``(provider, model)`` pairs (Spec 23 T10).

        Read-only — reads the preconstructed :class:`MultiModelChatBackend`
        wrapper's ``backends`` (Spec 20 D-20-17) WITHOUT instantiating anything
        (it never calls :meth:`get`). This is the enumeration seam the
        :class:`~persona_runtime.routing.intelligent_router.IntelligentRouter`
        uses to score the models within a tier (D-23-X-seam-shape).

        Returns ``()`` — "nothing to choose, no-op" — when the tier has no
        preconstructed multi-model wrapper: a single-backend (triplet / lazy)
        tier, a degenerate length-1 wrapper that was bypassed at build time, or
        an unconfigured tier with no fallback. The IntelligentRouter treats an
        empty result as "this is not a multi-model tier" and leaves the
        rule-based slot-0 selection untouched.

        Args:
            tier_name: The tier the router chose.

        Returns:
            ``(provider_name, model_name)`` for each constructed sub-backend in
            fallback order, or ``()`` when the tier is not a multi-model tier.
        """
        # Resolve WITHOUT logging / instantiation: exact match, then the same
        # fallback order :meth:`get` uses, else give up quietly.
        effective = tier_name if tier_name in self._tiers else None
        if effective is None:
            effective = next((c for c in _FALLBACK_ORDER if c in self._tiers), None)
        if effective is None:
            return ()
        backend = self._tiers[effective].preconstructed_backend
        if not isinstance(backend, MultiModelChatBackend):
            return ()
        return tuple((b.provider_name, b.model_name) for b in backend.backends)

    def supports_vision_for(self, tier_name: str) -> bool:
        """Whether the backend for ``tier_name`` accepts image content.

        Resolves through the same fallback chain as :meth:`get` (so a request
        for an unconfigured tier consults the fallback), then answers WITHOUT
        instantiating a backend (R1-D-1): if a backend is already built
        (preconstructed or previously cached) its ``supports_vision`` property
        is read directly; otherwise the answer is computed statically from the
        tier's ``(provider, model)`` via :func:`persona.backends.vision_supported`
        — the same O(1) lookup the backend constructor uses to set its own
        ``supports_vision``. This is a read over a static fact, so it never
        needs a live backend (a keyless community boot 500'd when this called
        ``load_backend``); the router (T13-T09) consults it BEFORE any rule
        fires so image-bearing turns can never land on a text-only tier.

        Args:
            tier_name: The tier name to inspect.

        Returns:
            ``True`` iff the resolved tier is vision-capable; ``False`` if it is
            text-only.

        Raises:
            TierNotConfiguredError: No tier resolves, even after fallback.
        """
        effective = self._resolve(tier_name)
        # Already-built backend (preconstructed at init / cached by a prior
        # get()) → read its live property, identical to before (cloud-unchanged
        # path; the MultiModelChatBackend's any()-over-the-chain answer is read
        # here too). Defensive ``getattr``: a legacy backend predating the
        # ChatBackend ``supports_vision`` declaration reads as not-vision-capable.
        built = self._cache.get(effective)
        if built is not None:
            return bool(getattr(built, "supports_vision", False))
        # Else compute statically from (provider, model) — no instantiation.
        config = self._tiers[effective].backend_config
        return vision_supported(config.provider, config.model)

    def get(self, tier_name: str) -> ChatBackend:
        """Return the backend for ``tier_name``, instantiating + caching once.

        Resolves the effective tier (applying the ``small → mid → frontier``
        fallback when ``tier_name`` is unconfigured), then returns the cached
        backend or builds it via :func:`load_backend` on first use.

        Args:
            tier_name: The tier the router chose.

        Returns:
            A :class:`ChatBackend`. The same instance is returned for repeated
            calls that resolve to the same effective tier.

        Raises:
            TierNotConfiguredError: No tier resolves, even after fallback.
        """
        effective = self._resolve(tier_name)
        if effective not in self._cache:
            self._cache[effective] = load_backend(self._tiers[effective].backend_config)
        return self._cache[effective]

    def _resolve(self, tier_name: str) -> str:
        """Return the configured tier name to use for ``tier_name``.

        Exact match wins. Otherwise walk the fallback order and return the
        first configured tier. Raise if nothing is configured.
        """
        if tier_name in self._tiers:
            return tier_name
        for candidate in _FALLBACK_ORDER:
            if candidate in self._tiers:
                _logger.warning(
                    "tier not configured; falling back requested={requested} using={using}",
                    requested=tier_name,
                    using=candidate,
                )
                return candidate
        raise TierNotConfiguredError(
            "no model tier is configured",
            context={
                "requested": tier_name,
                "configured": ", ".join(sorted(self._tiers)) or "(none)",
            },
        )

    async def aclose(self) -> None:
        """Disconnect every cached backend (D-05-4).

        Owned by the composition root, NOT the loop. Duck-types the cleanup:
        a backend exposing ``aclose`` or ``disconnect`` (e.g.,
        :class:`persona.backends.OllamaBackend`) is awaited; others are
        skipped. Never-``get()``-ed tiers are not instantiated just to close
        them. Idempotent — the cache is cleared after closing.
        """
        for name, backend in self._cache.items():
            closer = getattr(backend, "aclose", None) or getattr(backend, "disconnect", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception as exc:  # noqa: BLE001 — shutdown is best-effort; log and continue
                _logger.warning(
                    "error closing tier backend tier={tier} error={error}",
                    tier=name,
                    error=type(exc).__name__,
                )
        self._cache.clear()


def tier_metadata_from_env(*, prefix: str) -> TierMetadata | None:
    """Build :class:`TierMetadata` from per-tier env vars (Spec 18 T04).

    All six fields must be present (and well-formed) for a non-``None``
    return — partial env configuration returns ``None`` and is logged at
    DEBUG. This is the operator-override path; the in-band
    :class:`FirstTokenLatencyTracker` (T06) is the measured path that
    supersedes the env value once warmed up.

    Env var schema (per tier ``PREFIX`` matching the tier's
    :attr:`BackendConfig` prefix, e.g. ``PERSONA_FRONTIER_``):

    * ``<PREFIX>COST_INPUT_PER_1K`` (float, cents)
    * ``<PREFIX>COST_OUTPUT_PER_1K`` (float, cents)
    * ``<PREFIX>FIRST_TOKEN_LATENCY_MS`` (float, milliseconds)
    * ``<PREFIX>THROUGHPUT_TOKENS_PER_SEC`` (float)
    * ``<PREFIX>CONTEXT_WINDOW`` (int, tokens)
    * ``<PREFIX>TOOL_STRENGTH`` (one of ``"weak"`` / ``"medium"`` /
      ``"strong"``)

    Args:
        prefix: The tier's env-var prefix (e.g. ``"PERSONA_FRONTIER_"``).

    Returns:
        A :class:`TierMetadata` instance when all six env vars are present
        and parse cleanly; ``None`` otherwise (partial / absent / malformed).
    """
    required_keys = (
        f"{prefix}COST_INPUT_PER_1K",
        f"{prefix}COST_OUTPUT_PER_1K",
        f"{prefix}FIRST_TOKEN_LATENCY_MS",
        f"{prefix}THROUGHPUT_TOKENS_PER_SEC",
        f"{prefix}CONTEXT_WINDOW",
        f"{prefix}TOOL_STRENGTH",
    )
    if not all(key in os.environ for key in required_keys):
        return None
    # Spec 20 T14 additive — both default-safe.
    reasoning_capable_raw = os.environ.get(f"{prefix}REASONING_CAPABLE", "false").lower()
    cost_verified_raw = os.environ.get(f"{prefix}COST_VERIFIED_AT_DEPLOY", "true").lower()
    try:
        return TierMetadata(
            cost_input_per_1k_tokens=float(os.environ[required_keys[0]]),
            cost_output_per_1k_tokens=float(os.environ[required_keys[1]]),
            first_token_latency_ms=float(os.environ[required_keys[2]]),
            throughput_tokens_per_sec=float(os.environ[required_keys[3]]),
            context_window=int(os.environ[required_keys[4]]),
            tool_strength=os.environ[required_keys[5]],  # type: ignore[arg-type]
            reasoning_capable=reasoning_capable_raw in {"true", "1", "yes", "on"},
            cost_verified_at_deploy=cost_verified_raw in {"true", "1", "yes", "on"},
        )
    except (ValueError, TypeError) as exc:
        _logger.warning(
            "malformed tier metadata env vars; metadata absent prefix={prefix} error={error}",
            prefix=prefix,
            error=type(exc).__name__,
        )
        return None


def tier_registry_from_env(
    *,
    default_prefix: str = "PERSONA_",
    openrouter_subscription_mode: OpenRouterSubscriptionMode | None = None,
) -> TierRegistry:
    """Build a :class:`TierRegistry` from the environment (D-05-3 + D-20-17).

    Per-tier precedence (Spec 20 D-20-17 four cases):

    * **(a) MODELS-only set** — ``PERSONA_<TIER>_MODELS`` parses to N slots;
      build a :class:`MultiModelChatBackend` wrapper around the N resolved
      backends (or return the bare backend when N==1 after credential
      resolution). No log.
    * **(b) Triplet-only set** — ``PERSONA_<TIER>_PROVIDER`` +
      ``..._MODEL`` + ``..._API_KEY`` all set; build a single backend via
      :func:`load_backend` (backward-compat fast path). No log.
    * **(c) Both set** — MODELS wins; :func:`resolve_tier_config` emits an
      INFO log naming the ignored triplet vars.
    * **(d) Malformed MODELS** — :class:`MalformedTierModelsError` propagates
      from the parser.

    Plus the partial-triplet branch (1-2 of 3 + no MODELS) →
    :class:`IncompleteTierConfigError` propagates.

    Per-slot credential resolution disposition (Spec 20 D-20-15):

    * ≥1 provider resolves → build wrapper with the resolved slots, WARN per
      skipped slot, log the missing env var.
    * ALL slots fail → raise
      :class:`persona.backends.errors.TierNotConfiguredError` with
      ``missing_providers`` + ``configured_models`` + ``consulted_env_vars``
      context for operator diagnosis.

    When no tier blocks are present at all, a single backend built from
    ``default_prefix`` (the spec-02 ``PERSONA_*`` defaults) is registered for
    every tier name (Spec 05 single-backend fallback, unchanged).

    Spec 18 (T04): per-tier :class:`TierMetadata` continues to populate
    additively via :func:`tier_metadata_from_env`.

    Spec 22 (T14): when ``openrouter_subscription_mode`` is ``"free"`` (resolved
    at startup by ``persona_runtime.openrouter_subscription``), ``openrouter/X``
    MODELS entries lacking a ``:free`` suffix are dropped per tier with a WARN
    (D-22-2). A tier whose entire MODELS list drops away is simply not
    registered — it falls back via the registry's tier chain (fail-soft, not a
    hard error). ``None`` (probe skipped, no key, or paid-mode) is a no-op, so
    existing Spec-20 configurations are unaffected.

    Args:
        default_prefix: Env prefix for the single-backend fallback.
        openrouter_subscription_mode: Resolved OpenRouter mode (``"free"`` /
            ``"paid"``) or ``None`` (no OpenRouter free-mode filtering).

    Returns:
        A registry with whatever tiers the environment configured.
    """
    tiers: dict[str, TierConfig] = {}
    env_snapshot: dict[str, str] = dict(os.environ)
    resolver = ProviderCredentialResolver(env=env_snapshot)
    for tier_name, prefix in _TIER_ENV_PREFIXES.items():
        resolution = resolve_tier_config(tier_name, env=env_snapshot)
        if resolution.models is not None:
            # D-22-2: drop non-:free openrouter entries in free-mode (no-op when
            # mode is None / paid). An emptied MODELS list means the tier is left
            # unregistered → registry fallback chain (fail-soft).
            filtered_models = filter_openrouter_free_mode(
                resolution.models,
                mode=openrouter_subscription_mode,
                tier_name=tier_name,
                keep_free_suffix=True,
            )
            # R9-124: a retired model (NVIDIA's llama-3.3-70b, EOL 2026-08-26) sat in the
            # deployed mid chain for ten days. Dropped here with a WARN, so a stale env
            # var costs a log line at startup instead of a dead slot on every turn.
            filtered_models = filter_retired_models(filtered_models, tier_name=tier_name)
            if not filtered_models:
                continue
            tiers[tier_name] = _tier_config_from_models_list(
                tier_name=tier_name,
                prefix=prefix,
                resolution=replace(resolution, models=filtered_models),
                resolver=resolver,
            )
        elif resolution.triplet is not None:
            tiers[tier_name] = TierConfig(
                name=tier_name,
                backend_config=BackendConfig.from_env(prefix=prefix),
                metadata=tier_metadata_from_env(prefix=prefix),
            )
        elif f"{prefix}PROVIDER" in env_snapshot:
            # Defensive: triplet partial cases have already raised inside
            # :func:`resolve_tier_config`; reaching here means the legacy
            # detection sees PROVIDER set but the new resolver classified the
            # tier as unconfigured (e.g., PROVIDER=anthropic but no triplet
            # MODEL or API_KEY). Preserve the pre-Spec-20 behaviour for
            # operators who only set PROVIDER + rely on per-tier env defaults.
            tiers[tier_name] = TierConfig(
                name=tier_name,
                backend_config=BackendConfig.from_env(prefix=prefix),
                metadata=tier_metadata_from_env(prefix=prefix),
            )

    if not tiers:
        single = BackendConfig.from_env(prefix=default_prefix)
        single_metadata = tier_metadata_from_env(prefix=default_prefix)
        for tier_name in _TIER_ENV_PREFIXES:
            tiers[tier_name] = TierConfig(
                name=tier_name,
                backend_config=single,
                metadata=single_metadata,
            )
        _logger.info(
            "no per-tier env configured; serving the default backend for all tiers "
            "provider={provider}",
            provider=single.provider,
        )

    return TierRegistry(tiers)


def free_tier_registry_from_env(
    *,
    openrouter_subscription_mode: OpenRouterSubscriptionMode | None = None,
) -> TierRegistry:
    """Build a FREE-ONLY :class:`TierRegistry` from ``PERSONA_FREE_<TIER>_MODELS`` (Spec M4, T5a).

    The free plan's dedicated model set (D-M4-4): a SEPARATE registry whose every tier's whole
    :class:`~persona.backends.multi_model.MultiModelChatBackend` fallback chain is free-only, so a
    free user's fallback walk can NEVER reach a paid model. Reads ``PERSONA_FREE_FRONTIER_MODELS``
    / ``PERSONA_FREE_MID_MODELS`` (+ optional ``PERSONA_FREE_SMALL_MODELS``) through the SAME
    parse / credential-resolve / wrapper path the paid tiers use.

    **FAIL-CLOSED (the crux, T5a):** a tier whose ``PERSONA_FREE_<TIER>_MODELS`` is unset, empty,
    malformed, or has no resolvable credentials is simply ABSENT — there is **NO** fallback to the
    ``PERSONA_*`` default (unlike :func:`tier_registry_from_env`). If nothing free is configured the
    registry is EMPTY, so a free user's :meth:`TierRegistry.get` raises
    :class:`~persona_runtime.errors.TierNotConfiguredError` (→ the graceful T5b "capacity full /
    upgrade" response) rather than serving a paid model. Only frontier + mid need configuring —
    ``small`` falls back within the free registry (``small → mid → frontier``, all free).

    Args:
        openrouter_subscription_mode: Resolved OpenRouter mode (``"free"`` / ``"paid"``) or
            ``None`` — passed through the same ``:free``-suffix filter as the paid builder.

    Returns:
        A free-only registry (possibly empty — fail-closed).
    """
    tiers: dict[str, TierConfig] = {}
    env_snapshot: dict[str, str] = dict(os.environ)
    resolver = ProviderCredentialResolver(env=env_snapshot)
    for tier_name, prefix in _FREE_TIER_ENV_PREFIXES.items():
        raw = env_snapshot.get(f"{prefix}MODELS", "").strip()
        if not raw:
            continue  # fail-closed: an unconfigured free tier is ABSENT (never a paid default)
        try:
            models = parse_models_list(tier_name, raw)
            filtered = filter_openrouter_free_mode(
                models,
                mode=openrouter_subscription_mode,
                tier_name=tier_name,
                keep_free_suffix=True,
            )
            filtered = filter_retired_models(filtered, tier_name=tier_name)
            if not filtered:
                continue
            tiers[tier_name] = _tier_config_from_models_list(
                tier_name=tier_name,
                prefix=prefix,
                resolution=TierResolution(tier=tier_name, models=filtered, triplet=None),
                resolver=resolver,
            )
        except (
            MalformedTierModelsError,
            LocalProviderInModelsListError,
            ModelsListTierNotConfiguredError,
        ) as exc:
            # Fail-closed: a broken free tier is ABSENT, never a paid fallback. Boot proceeds
            # (paid users unaffected); a free user on this tier gets the graceful T5b response.
            _logger.warning(
                "free tier {tier} not built ({error}); absent — fail-closed (no paid fallback)",
                tier=tier_name,
                error=type(exc).__name__,
            )
            continue
    return TierRegistry(tiers)


def _tier_config_from_models_list(
    *,
    tier_name: str,
    prefix: str,
    resolution: TierResolution,
    resolver: ProviderCredentialResolver,
) -> TierConfig:
    """Build a :class:`TierConfig` for a MODELS-list tier (D-20-15 + D-20-17).

    Resolves each ``(provider, model)`` slot through the resolver; WARNs and
    skips slots whose credentials are missing; raises
    :class:`ModelsListTierNotConfiguredError` when every slot fails (D-20-15
    ALL-fail branch).

    When the resolved chain length is 1 the wrapper is bypassed: the bare
    single backend is registered directly so callers pay zero wrapper
    overhead on the degenerate path.

    Args:
        tier_name: Tier name (lower-case).
        prefix: Tier env-var prefix (e.g. ``"PERSONA_FRONTIER_"``).
        resolution: :class:`TierResolution` from
            :func:`resolve_tier_config` with ``models`` populated.
        resolver: Shared :class:`ProviderCredentialResolver` over the same
            env snapshot.

    Returns:
        A :class:`TierConfig` with ``preconstructed_backend`` populated.

    Raises:
        ModelsListTierNotConfiguredError: Every slot's credentials missing.
    """
    assert resolution.models is not None  # narrow for type-checker
    resolved_backends: list[ChatBackend] = []
    skipped: list[tuple[str, str]] = []  # (provider, env_var)
    for position, (provider, model) in enumerate(resolution.models):
        try:
            creds = resolver.resolve(provider)
        except ProviderCredentialMissingError as exc:
            env_var = exc.context.get("env_var", "")
            _logger.warning(
                "provider credential missing; skipping backend slot "
                "tier={tier} provider={provider} env_var={env_var} "
                "position={position}/{total}",
                tier=tier_name,
                provider=provider,
                env_var=env_var,
                position=position,
                total=len(resolution.models),
            )
            skipped.append((provider, env_var))
            continue
        slot_config = BackendConfig(
            provider=provider,
            model=model,
            api_key=creds.api_key,
            base_url=creds.base_url or None,
        )
        resolved_backends.append(load_backend(slot_config))

    if not resolved_backends:
        # D-20-15 ALL-fail branch: every provider in the MODELS list lacked
        # a credential. Fail loud at construction with operator-actionable
        # context.
        raise ModelsListTierNotConfiguredError(
            f"tier {tier_name!r} has no resolvable backends",
            context={
                "tier": tier_name,
                "configured_models": ",".join(f"{p}/{m}" for p, m in resolution.models),
                "missing_providers": ",".join(p for p, _ in skipped),
                "consulted_env_vars": ",".join(v for _, v in skipped if v),
            },
        )

    # Primary slot's (provider, model) — used for `model_name_for()` so the
    # router can populate `RoutingDecision.model` without instantiating.
    primary_provider, primary_model = resolution.models[0]
    primary_config = BackendConfig(
        provider=primary_provider,
        model=primary_model,
    )

    if len(resolved_backends) == 1:
        # Degenerate: skip the wrapper entirely (D-20-17 case (a) length-1
        # bare-single fast path).
        return TierConfig(
            name=tier_name,
            backend_config=primary_config,
            metadata=tier_metadata_from_env(prefix=prefix),
            preconstructed_backend=resolved_backends[0],
        )

    wrapper = MultiModelChatBackend(
        backends=resolved_backends,
        tier_name=tier_name,
    )
    return TierConfig(
        name=tier_name,
        backend_config=primary_config,
        metadata=tier_metadata_from_env(prefix=prefix),
        preconstructed_backend=wrapper,
    )
