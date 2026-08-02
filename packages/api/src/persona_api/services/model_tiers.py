"""Shared model-tier composition: the OpenRouter mode probe + the free-plan registry.

Promoted out of :mod:`persona_api.app` (R9-074). Model SELECTION — which tier
registry a caller resolves, and whether the free-plan gate is armed at all — is a
policy decision that must be identical on every surface that runs a chat turn.
The hosted API composes it in its lifespan; the standalone connector service
(``python -m persona_connectors``) composes its own :class:`RuntimeFactory` in a
separate process and cannot import :mod:`persona_api.app` without dragging the
whole FastAPI app construction with it.

Before this module the connector simply omitted ``free_tier_registry``, and
``RuntimeFactory._plan_tier_selection`` reads ``None`` as *"gating off"* — so
every free-plan user was silently served the PAID tiers on Telegram / Discord /
Slack / WhatsApp / SMS / email. Keeping the decision in ONE place both processes
import is what makes that class of drift impossible rather than merely fixed.

R9-096 extends the same idea from *composition* to *resolution*. Building the
free registry is only half the gate; something has to CHOOSE between it and the
paid registry per owner. That choice used to be typed out inside
``RuntimeFactory._plan_tier_selection`` alone, so every surface that resolved a
backend without going through the factory (the in-process worker's background
jobs, the voice auto-pick) silently ran on the paid tiers.
:func:`select_plan_tier_registry` is now that one choice, and
:class:`PlanScopedChatBackend` is the one way a long-lived composition root can
hold a backend that still resolves per owner.

Nothing here holds owner state; every function is a pure composition step over
the resolved :class:`~persona_api.config.APIConfig` + the environment, and the
plan-scoped backend re-reads the caller's plan on every call.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.backends.errors import AuthenticationError
from persona.backends.errors import TierNotConfiguredError as BackendTierNotConfiguredError
from persona.logging import get_logger
from persona_runtime.errors import TierNotConfiguredError as RegistryTierNotConfiguredError
from persona_runtime.openrouter_subscription import resolve_openrouter_subscription
from persona_runtime.tier import free_tier_registry_from_env

from persona_api.config import Edition
from persona_api.services.llm_usage_collector import UsageCollectingBackend

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends.openrouter_catalog import OpenRouterSubscriptionMode
    from persona.backends.protocol import ChatBackend
    from persona.backends.types import ChatResponse, StreamChunk, ToolSpec
    from persona.schema.conversation import ConversationMessage
    from persona_runtime.tier import TierRegistry
    from sqlalchemy import Engine

    from persona_api.config import APIConfig

__all__ = [
    "PlanScopedChatBackend",
    "build_free_tier_registry",
    "plan_scoped_background_backend",
    "resolve_openrouter_subscription_mode",
    "select_plan_tier_registry",
    "warn_if_cloud_free_registry_empty",
]

#: What :class:`PlanScopedChatBackend`'s metadata properties report when the owner's
#: plan has no configured tier. Only ever observable on the failure path (a ``chat``
#: on the same backend raises), and deliberately not a real provider name.
_UNRESOLVED = "unresolved"

_LOG = get_logger("api.model_tiers")


def resolve_openrouter_subscription_mode() -> OpenRouterSubscriptionMode | None:
    """Resolve the OpenRouter free/paid mode at startup (Spec 22 T13 + T15).

    Probes ``GET /api/v1/key`` once (or honours the
    ``PERSONA_OPENROUTER_SUBSCRIPTION_MODE`` override) so the resolved mode can
    be threaded into both the chat :class:`~persona_runtime.tier.TierRegistry`
    (D-22-2 free-mode filter) and the image-gen factory (D-22-20 drop). Returns
    ``None`` when OpenRouter is not configured (no key) — the zero-touch opt-in
    path.

    Composition-root degradation: an
    :class:`~persona.backends.errors.AuthenticationError` (the resolver's D-22-9
    fail-loud signal for an invalid key) is logged at ERROR and swallowed here so
    one optional provider's bad key does NOT block startup — consistent with the
    graceful-absence pattern used for the image backend and the E2B-less sandbox
    pool. The misconfigured OpenRouter entries then surface their 401 at call
    time. A transient probe failure already degrades to free-mode inside the
    resolver (D-22-3).

    Returns:
        The resolved mode, or ``None`` when OpenRouter is unconfigured / rejected.
    """
    try:
        state = resolve_openrouter_subscription()
    except AuthenticationError as exc:
        _LOG.error(
            "OpenRouter API key rejected at startup; OpenRouter free-mode "
            "filtering disabled (reason={reason})",
            reason=str(exc),
        )
        return None
    if state is None:
        return None
    _LOG.info(
        "OpenRouter subscription mode resolved mode={mode} probe_failed={probe_failed}",
        mode=state.mode,
        probe_failed=state.probe_failed,
    )
    return state.mode


def warn_if_cloud_free_registry_empty(
    config: APIConfig, free_tier_registry: TierRegistry | None
) -> None:
    """Loud startup WARNING when cloud + an empty free-tier registry (R9-059).

    M4 fail-closed semantics mean an unconfigured ``PERSONA_FREE_*_MODELS`` set
    yields an EMPTY (non-``None``) registry — every free-plan / no-subscription
    caller is handed it, and their turn now fails via the graceful
    ``TierNotConfiguredError`` path (see ``persona_runtime.routing.layer1``).
    That failure is correct (no silent paid fallback) but easy to miss until a
    real user hits it. This warns the operator the moment the process boots,
    without blocking boot — WARNING only, never raises.

    Args:
        config: The resolved :class:`~persona_api.config.APIConfig` for this boot.
        free_tier_registry: The cloud free-tier registry, or ``None`` in
            community (no gating — never warns).
    """
    if (
        config.edition is Edition.cloud
        and free_tier_registry is not None
        and not free_tier_registry.configured_tier_names
    ):
        _LOG.warning(
            "cloud edition is up but no PERSONA_FREE_*_MODELS are configured — "
            "every free-plan / no-subscription caller will get an empty tier "
            "registry and fail chat. Set PERSONA_FREE_FRONTIER_MODELS / "
            "PERSONA_FREE_MID_MODELS, or ensure operator accounts have a paid "
            "subscription row."
        )


def build_free_tier_registry(
    config: APIConfig,
    *,
    openrouter_subscription_mode: OpenRouterSubscriptionMode | None,
) -> TierRegistry | None:
    """The plan-gating registry for this edition — ONE definition, every surface.

    Spec M4 (T5a): the FREE plan's dedicated free-only tier registry, built ONLY
    in the cloud edition. Community is unmetered and has no plans, so it gets
    ``None``, which makes ``RuntimeFactory._plan_tier_selection`` short-circuit to
    the paid tiers — byte-identical to the pre-M4 behaviour, ungated by design.

    Fail-closed: an unconfigured ``PERSONA_FREE_*_MODELS`` set yields an EMPTY
    (non-``None``) registry, so a free user's turn raises the graceful T5b
    response rather than silently falling back to a paid model. That case is
    warned about loudly here (R9-059) so the operator sees it at boot.

    R9-074: this is the whole reason the function exists rather than being
    inlined at each composition root — ``None`` and "empty" mean opposite things
    (gating OFF vs gating ON with nothing configured), and a surface that simply
    forgets the argument gets the dangerous one.

    Args:
        config: The resolved :class:`~persona_api.config.APIConfig` for this boot.
        openrouter_subscription_mode: The mode from
            :func:`resolve_openrouter_subscription_mode`, threaded through the
            same ``:free``-suffix filter the paid builder uses.

    Returns:
        The cloud free-only registry (possibly empty), or ``None`` in community.
    """
    free_tier_registry = (
        free_tier_registry_from_env(openrouter_subscription_mode=openrouter_subscription_mode)
        if config.edition is Edition.cloud
        else None
    )
    warn_if_cloud_free_registry_empty(config, free_tier_registry)
    return free_tier_registry


def select_plan_tier_registry(
    *,
    rls_engine: Engine,
    paid_tier_registry: TierRegistry,
    free_tier_registry: TierRegistry | None,
) -> TierRegistry:
    """The tier registry the CURRENT owner's plan entitles them to (Spec M4, D-M4-9).

    THE plan gate, in one place. ``free_tier_registry`` is ``None`` when gating is off
    (community / self-host — no plans, nothing to gate), in which case every caller
    resolves the paid tiers, byte-identically to the pre-M4 behaviour. Otherwise the
    owner's plan is read RLS-scoped from their ``subscription`` row (the
    ``current_user_id`` contextvar): ``free`` resolves the free-only registry, a paid
    plan resolves the paid one.

    Fail-safe by construction: an absent row, an unreadable plan, or no owner bound at
    all defaults to ``free`` — the restrictive set — so a lookup miss can never open the
    paid tiers to a free user.

    Args:
        rls_engine: The owner-scoped engine the ``subscription`` row is read on.
        paid_tier_registry: The full paid tier registry.
        free_tier_registry: The free-only registry, or ``None`` when gating is off.

    Returns:
        The registry this owner may resolve tiers against.
    """
    if free_tier_registry is None:
        return paid_tier_registry  # gating off (community / self-host)
    from persona_api.middleware.rls_context import current_user_id
    from persona_api.services import subscription_service

    user_id = current_user_id.get()
    plan_code = "free"
    if user_id:
        row = subscription_service.get_subscription(rls_engine, user_id=user_id)
        if row is not None:
            plan_code = str(row.get("plan_code") or "free")
    return free_tier_registry if plan_code == "free" else paid_tier_registry


class PlanScopedChatBackend:
    """A :class:`ChatBackend` that resolves the CURRENT owner's plan tier per call (R9-096).

    The composition-root problem this solves: a long-lived surface (the in-process
    worker's job registry, a boot-time reconciliation sweep) resolves its backend ONCE,
    at startup, when no owner is bound — so it can only ever hold ONE plan's models, and
    it held the paid ones for everybody. Free-plan owners' background summarisation,
    initiative scans and title refreshes therefore ran on paid models, which is both a
    cost leak and a D-M4-9 violation ("a free user must NEVER reach a paid model").

    This backend is the seam that fixes it without rebuilding every collaborator per
    job: it holds the tier NAME plus both registries and defers
    :func:`select_plan_tier_registry` to **call** time, inside the per-job / per-request
    owner scope. No instance is ever bound to one owner's plan, so one shared instance
    serving many owners is safe by construction.

    Fail-closed: when the resolved plan's registry has no such tier (the documented
    empty-free-registry case), ``chat`` / ``chat_stream`` raise
    ``TierNotConfiguredError`` — the caller's existing fail-soft catch treats that as
    "surface not wired" and skips. There is deliberately no paid fallback.

    Cost: one small indexed ``subscription`` read per call. These are background /
    create-time ops (single-digit calls per job), never the streaming chat hot path.
    """

    def __init__(
        self,
        *,
        tier: str,
        rls_engine: Engine,
        paid_tier_registry: TierRegistry,
        free_tier_registry: TierRegistry | None,
    ) -> None:
        """Bind the tier NAME and both registries; the owner is resolved per call.

        Args:
            tier: The tier name to resolve (``"small"`` / ``"mid"`` / ``"frontier"``).
                Unchanged by this seam — the per-surface tier knobs still choose it.
            rls_engine: The owner-scoped engine the plan is read on.
            paid_tier_registry: The full paid registry.
            free_tier_registry: The free-only registry, or ``None`` when gating is off.
        """
        self._tier = tier
        self._engine = rls_engine
        self._paid = paid_tier_registry
        self._free = free_tier_registry

    def _resolve(self) -> ChatBackend:
        """Resolve this tier against the current owner's plan registry."""
        registry = select_plan_tier_registry(
            rls_engine=self._engine,
            paid_tier_registry=self._paid,
            free_tier_registry=self._free,
        )
        return registry.get(self._tier)

    def _resolve_or_none(self) -> ChatBackend | None:
        """Resolve, or ``None`` when the owner's plan has no configured tier.

        The metadata properties must not raise: callers read ``provider_name`` from
        inside their own ``except`` blocks (``TierSummarizer`` does exactly this), and a
        raising property there would replace an honest domain error with a confusing one.
        """
        try:
            return self._resolve()
        except (BackendTierNotConfiguredError, RegistryTierNotConfiguredError):
            return None

    @property
    def provider_name(self) -> str:
        """The resolved backend's provider, or ``"unresolved"`` when the plan has none."""
        backend = self._resolve_or_none()
        return backend.provider_name if backend is not None else _UNRESOLVED

    @property
    def model_name(self) -> str:
        """The resolved backend's model, or ``"unresolved"`` when the plan has none."""
        backend = self._resolve_or_none()
        return backend.model_name if backend is not None else _UNRESOLVED

    @property
    def supports_native_tools(self) -> bool:
        """Whether the resolved backend uses native tool calling (``False`` if unresolved)."""
        backend = self._resolve_or_none()
        return backend.supports_native_tools if backend is not None else False

    @property
    def supports_vision(self) -> bool:
        """Whether the resolved backend accepts images (``False`` if unresolved)."""
        backend = self._resolve_or_none()
        return backend.supports_vision if backend is not None else False

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
        """Chat on the current owner's plan-resolved backend."""
        return await self._resolve().chat(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            top_p=top_p,
            top_k=top_k,
        )

    async def chat_stream(
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
        """Stream on the current owner's plan-resolved backend."""
        async for chunk in self._resolve().chat_stream(
            messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            stop=stop,
            top_p=top_p,
            top_k=top_k,
        ):
            yield chunk


def plan_scoped_background_backend(
    *,
    tier: str,
    rls_engine: Engine,
    paid_tier_registry: TierRegistry,
    free_tier_registry: TierRegistry | None,
    metered: bool,
) -> ChatBackend:
    """The ONE way a background surface gets a model backend (R9-096).

    Composes :class:`PlanScopedChatBackend` (per-owner plan resolution) and, when
    ``metered``, wraps it in :class:`~persona_api.services.llm_usage_collector.
    UsageCollectingBackend` so the handler's ``collect_llm_usage`` block still captures
    the real per-op cost for owner billing (Spec M3, T5). The wrapper is the OUTER
    layer, exactly as it was when the inner backend came straight from the registry, so
    metering is unchanged.

    ``metered`` is required rather than defaulted: the two forgettable decisions on this
    path — "is it plan-gated" and "is it billed" — are both stated at every call site.
    R9-074's lesson is that a keyword with a default is a keyword that gets forgotten,
    and the dangerous value is the one you get by forgetting.

    Args:
        tier: The tier name the surface's own knob selected.
        rls_engine: The owner-scoped engine (the worker binds ``current_user_id`` per job).
        paid_tier_registry: The full paid registry.
        free_tier_registry: The free-only registry, or ``None`` when gating is off.
        metered: Whether this surface bills the owner for the call's real cost.

    Returns:
        A backend that resolves the calling owner's plan on every call.
    """
    backend = PlanScopedChatBackend(
        tier=tier,
        rls_engine=rls_engine,
        paid_tier_registry=paid_tier_registry,
        free_tier_registry=free_tier_registry,
    )
    return UsageCollectingBackend(backend) if metered else backend
