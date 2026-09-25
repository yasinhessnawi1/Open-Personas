"""Startup-time OpenRouter subscription resolver (Spec 22 T13).

Owns the *side-effecting* half of OpenRouter subscription resolution: env
reading + the catalog-client probe + the fail-open policy. The pure mappers
it composes live in
:mod:`persona.backends.openrouter_catalog`
(:func:`subscription_state_from_key_info`, :func:`free_mode_fallback`) and
stay side-effect-free for testability.

Decisions wired here:

* **D-22-3 (fail-open probe).** A non-fatal probe failure
  (:class:`~persona.backends.errors.OpenRouterBalanceProbeError` — timeout /
  5xx / malformed body) degrades conservatively to **free-mode** via
  :func:`free_mode_fallback` (cannot confirm paid credits) and WARNs rather
  than taking the backend down.
* **D-22-7 (operator escape hatch).** ``PERSONA_OPENROUTER_SUBSCRIPTION_MODE``
  forces ``"free"`` / ``"paid"`` (case-insensitive, stripped) and skips the
  probe entirely. An invalid value is an operator typo and fails loud.
* **D-22-9 (fail-loud auth).** An :class:`~persona.backends.errors.AuthenticationError`
  (401 / invalid key) propagates — a broken key is an operator error, not a
  transient degradation.
* **D-22-11 (one-shot, off the turn path).** The probe is a synchronous
  startup / config-reload operation; this resolver runs once at composition,
  never per turn.

Backward compatibility: OpenRouter is opt-in. With no
``PERSONA_OPENROUTER_API_KEY`` configured this resolver returns ``None`` and
existing configurations are untouched.

References:
    docs/specs/phase2/spec_22/decisions.md D-22-3/7/9/11.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from persona.backends.errors import (
    AuthenticationError,
    OpenRouterBalanceProbeError,
)
from persona.backends.openrouter_catalog import (
    OpenRouterCatalogClient,
    OpenRouterSubscriptionState,
    free_mode_fallback,
    subscription_state_from_key_info,
)
from persona.logging import get_logger

from persona_runtime.errors import InvalidSubscriptionModeError

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.backends.openrouter_catalog import OpenRouterSubscriptionMode

__all__ = [
    "SUBSCRIPTION_MODES",
    "SUBSCRIPTION_MODE_ENV",
    "resolve_openrouter_subscription",
    "resolve_openrouter_subscription_mode",
]

_LOG = get_logger("runtime.openrouter_subscription")

_API_KEY_ENV = "PERSONA_OPENROUTER_API_KEY"
_BASE_URL_ENV = "PERSONA_OPENROUTER_BASE_URL"
#: The operator override for the subscription mode. Public since R9-213 part 2: the api and
#: voice must be handed the same value, so the deploy and the boot line name it too, and
#: voice's ERROR line names it without repeating the string or printing the value (R9-224).
SUBSCRIPTION_MODE_ENV = "PERSONA_OPENROUTER_SUBSCRIPTION_MODE"

#: The values the override accepts (after strip and lower-casing, as read below).
SUBSCRIPTION_MODES: frozenset[str] = frozenset({"free", "paid"})


def resolve_openrouter_subscription(
    env: dict[str, str] | None = None,
    *,
    now: datetime | None = None,
    client_factory: Callable[[str], OpenRouterCatalogClient] | None = None,
) -> OpenRouterSubscriptionState | None:
    """Resolve the OpenRouter subscription mode at startup (Spec 22 T13).

    Mirrors the env-snapshot convention of
    :func:`persona_runtime.tier.tier_registry_from_env` /
    :class:`persona.backends.credentials.ProviderCredentialResolver`.

    Resolution order:

    1. No ``PERSONA_OPENROUTER_API_KEY`` (absent or empty after strip) →
       ``None`` (OpenRouter is opt-in; provider unused — zero-touch
       backward compat).
    2. ``PERSONA_OPENROUTER_SUBSCRIPTION_MODE`` set to ``free`` / ``paid``
       (case-insensitive, stripped) → a forced state, **no probe** (D-22-7).
       Any other non-empty value → :class:`InvalidSubscriptionModeError`
       (a ``ValueError``; an operator typo).
    3. Otherwise probe ``GET /api/v1/key`` via the catalog client and map the
       result with :func:`subscription_state_from_key_info`.

    Fail-open policy (the probe branch only):

    * :class:`~persona.backends.errors.OpenRouterBalanceProbeError` →
      conservative :func:`free_mode_fallback` + WARN (D-22-3); not propagated.
    * :class:`~persona.backends.errors.AuthenticationError` → propagated
      (D-22-9, fail-loud).

    Args:
        env: Environment mapping to read (defaults to a snapshot of
            ``os.environ``). A copy is taken so later mutation is ignored.
        now: Timestamp recorded as ``last_checked_at`` (defaults to
            ``datetime.now(UTC)``). Tests pass a fixed value.
        client_factory: Builds the catalog client from the API key. Defaults
            to constructing an :class:`OpenRouterCatalogClient` with the
            configured base URL. Tests inject a fake to avoid network.

    Returns:
        The resolved subscription state, or ``None`` when OpenRouter is not
        configured.

    Raises:
        InvalidSubscriptionModeError: ``PERSONA_OPENROUTER_SUBSCRIPTION_MODE`` is
            set to a value other than ``free`` / ``paid`` (a ``ValueError``).
        AuthenticationError: the probe rejected the API key (HTTP 401,
            D-22-9 fail-loud).
    """
    snapshot = dict(os.environ if env is None else env)
    checked_at = now if now is not None else datetime.now(UTC)

    api_key = snapshot.get(_API_KEY_ENV, "").strip()
    if not api_key:
        return None

    forced = _resolve_forced_mode(snapshot.get(SUBSCRIPTION_MODE_ENV), checked_at=checked_at)
    if forced is not None:
        return forced

    factory = client_factory if client_factory is not None else _default_client_factory(snapshot)
    return _probe_subscription(api_key, checked_at=checked_at, client_factory=factory)


def resolve_openrouter_subscription_mode() -> OpenRouterSubscriptionMode | None:
    """Resolve the OpenRouter free/paid mode at startup (Spec 22 T13 + T15).

    Probes ``GET /api/v1/key`` once (or honours the
    ``PERSONA_OPENROUTER_SUBSCRIPTION_MODE`` override) so the resolved mode can
    be threaded into both the chat :class:`~persona_runtime.tier.TierRegistry`
    (D-22-2 free-mode filter) and the image-gen factory (D-22-20 drop). Returns
    ``None`` when OpenRouter is not configured (no key): the zero-touch opt-in
    path.

    One definition for every process that builds tier registries: the api, the
    connector service and voice (R9-224). It lived in the api until voice needed it,
    and voice may not import the api, so voice had simply gone without.

    Composition-root degradation: an
    :class:`~persona.backends.errors.AuthenticationError` (the resolver's D-22-9
    fail-loud signal for an invalid key) is logged at ERROR and swallowed here so
    one optional provider's bad key does NOT block startup, consistent with the
    graceful-absence pattern used for the image backend and the E2B-less sandbox
    pool. The misconfigured OpenRouter entries then surface their 401 at call
    time. A transient probe failure already degrades to free-mode inside the
    resolver (D-22-3).

    Returns:
        The resolved mode, or ``None`` when OpenRouter is unconfigured / rejected.

    Raises:
        InvalidSubscriptionModeError: the override is set to something other than
            ``free`` / ``paid``. Not swallowed here: each caller decides whether a
            typo stops its boot.
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


def _resolve_forced_mode(
    raw_mode: str | None, *, checked_at: datetime
) -> OpenRouterSubscriptionState | None:
    """Resolve the D-22-7 escape-hatch override, if any.

    Returns the forced state for a valid ``free`` / ``paid`` value (no probe),
    ``None`` when the override is unset/empty, and raises for any other value.
    """
    if raw_mode is None:
        return None
    mode = raw_mode.strip().lower()
    if not mode:
        return None
    if mode not in SUBSCRIPTION_MODES:
        raise InvalidSubscriptionModeError(
            f"{SUBSCRIPTION_MODE_ENV} must be 'free' or 'paid' (case-insensitive); got {raw_mode!r}"
        )
    _LOG.info(
        "openrouter subscription mode forced via env; skipping probe mode={mode}",
        mode=mode,
    )
    return OpenRouterSubscriptionState(
        mode=mode,  # type: ignore[arg-type]  # narrowed to the Literal by SUBSCRIPTION_MODES
        is_free_tier=mode == "free",
        limit_remaining=None,
        last_checked_at=checked_at,
        probe_failed=False,
    )


def _probe_subscription(
    api_key: str,
    *,
    checked_at: datetime,
    client_factory: Callable[[str], OpenRouterCatalogClient],
) -> OpenRouterSubscriptionState:
    """Probe ``GET /api/v1/key`` and apply the D-22-3 / D-22-9 policy.

    The client is always closed in a ``finally`` so a probe failure never
    leaks the underlying ``httpx.Client``.
    """
    client = client_factory(api_key)
    try:
        key_info = client.get_key_info()
    except OpenRouterBalanceProbeError as exc:
        reason = exc.context.get("reason", "unknown")
        return free_mode_fallback(checked_at=checked_at, reason=reason)
    except AuthenticationError:
        # D-22-9 — a broken key is an operator error, not a transient
        # degradation; surface it.
        _LOG.error("openrouter rejected the configured API key; failing loud")
        raise
    finally:
        client.close()
    return subscription_state_from_key_info(key_info, checked_at=checked_at)


def _default_client_factory(
    snapshot: dict[str, str],
) -> Callable[[str], OpenRouterCatalogClient]:
    """Build the default catalog-client factory honouring the base-URL env."""
    base_url = snapshot.get(_BASE_URL_ENV, "").strip() or None

    def _factory(api_key: str) -> OpenRouterCatalogClient:
        return OpenRouterCatalogClient(api_key, base_url=base_url)

    return _factory
