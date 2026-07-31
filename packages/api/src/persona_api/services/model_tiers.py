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

Nothing here holds state; every function is a pure composition step over the
resolved :class:`~persona_api.config.APIConfig` + the environment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.backends.errors import AuthenticationError
from persona.logging import get_logger
from persona_runtime.openrouter_subscription import resolve_openrouter_subscription
from persona_runtime.tier import free_tier_registry_from_env

from persona_api.config import Edition

if TYPE_CHECKING:
    from persona.backends.openrouter_catalog import OpenRouterSubscriptionMode
    from persona_runtime.tier import TierRegistry

    from persona_api.config import APIConfig

__all__ = [
    "build_free_tier_registry",
    "resolve_openrouter_subscription_mode",
    "warn_if_cloud_free_registry_empty",
]

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
