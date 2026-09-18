"""Per-provider credential resolver + tier-MODELS parser for Spec 20.

This module is the single source of truth for converting a ``<provider>``
reference (used by the cross-provider multi-model wrapper) into a
``(api_key, base_url)`` tuple, and for parsing the comma-separated
``PERSONA_<TIER>_MODELS`` env var into a list of ``(provider, model)`` slots.

Implements the locked behaviour from ``docs/specs/phase2/spec_20/decisions.md``:

* D-20-15 — three-tier disposition for missing-vs-empty credentials.
* D-20-17 — four-case precedence between MODELS list and the per-tier
  ``PROVIDER + MODEL + API_KEY`` triplet (case (d) malformed + the
  partial-triplet ``IncompleteTierConfigError`` branch).
* D-20-18 — EXPLICIT REJECT of ``local`` / ``ollama`` in MODELS lists.

T11's public surface is consumed by T17 (TierRegistry wiring). The functions
here are pure-library and side-effect-free aside from the INFO log emitted by
``resolve_tier_config`` when case (c) fires.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal, TypeVar, get_args

from pydantic import SecretStr

from persona.backends.config import DEFAULT_BASE_URLS, Provider
from persona.backends.errors import (
    AuthenticationError,
    IncompleteTierConfigError,
    LocalProviderInModelsListError,
    MalformedTierModelsError,
    ProviderCredentialMissingError,
)
from persona.logging import get_logger

if TYPE_CHECKING:
    from persona.backends.openrouter_catalog import OpenRouterSubscriptionMode

__all__ = [
    "FREE_BY_DEFINITION_OPENROUTER_SLUGS",
    "ProviderCredentials",
    "ProviderCredentialResolver",
    "TierResolution",
    "filter_openrouter_free_mode",
    "is_free_openrouter_slot",
    "parse_models_list",
    "resolve_tier_config",
]

_ProviderStr = TypeVar("_ProviderStr", bound=str)

_LOG = get_logger("persona.backends.credentials")

# D-20-15 — keyless providers (no PERSONA_<NAME>_API_KEY env var consulted).
_KEYLESS_PROVIDERS: Final[frozenset[str]] = frozenset({"ollama", "local"})

# D-20-18 — providers that the parser must reject inside a MODELS list (even
# though they appear in the Provider Literal as single-backend fast-path
# targets). The hint text routes operators to the correct alternative.
_LOCAL_PROVIDERS_REJECTED_IN_MODELS_LISTS: Final[frozenset[str]] = frozenset({"local", "ollama"})

_LOCAL_REJECT_HINTS: Final[dict[str, str]] = {
    "local": "use PERSONA_LOCAL_MODEL_ID single-backend fast path",
    "ollama": (
        "use PERSONA_<TIER>_PROVIDER=ollama single-backend fast path for "
        "local-dev; ollama is not designed as a cross-provider fallback peer"
    ),
}

_VALID_PROVIDERS: Final[frozenset[str]] = frozenset(get_args(Provider))

# Cloudflare Workers AI's OpenAI-compatible chat base URL embeds the account id:
# ``.../accounts/{ACCOUNT_ID}/ai/v1/``. Unlike the static DEFAULT_BASE_URLS
# entries, it must be assembled at resolution time from
# ``PERSONA_CLOUDFLARE_ACCOUNT_ID`` (the same env var the imagegen path reads).
# The resolver appends this suffix to the account-prefix template
# (DEFAULT_BASE_URLS["cloudflare"], which ends at ``/accounts/``).
_CLOUDFLARE_ACCOUNT_ID_ENV: Final[str] = "PERSONA_CLOUDFLARE_ACCOUNT_ID"
_CLOUDFLARE_CHAT_URL_SUFFIX: Final[str] = "/ai/v1/"


@dataclass(frozen=True)
class ProviderCredentials:
    """Resolved ``(api_key, base_url)`` tuple for a single provider reference.

    Attributes:
        provider: Provider Literal symbol the credentials belong to.
        api_key: Wrapped API key. ``None`` for keyless providers
            (D-20-15: ``ollama`` and ``local`` have no per-provider key).
        base_url: Resolved base URL. May be ``""`` for the ``local`` provider
            (no HTTP transport — runs in-process via HuggingFace).
    """

    provider: Provider
    api_key: SecretStr | None
    base_url: str

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return (
            f"ProviderCredentials(provider={self.provider!r}, "
            f"api_key=<redacted>, base_url={self.base_url!r})"
        )


@dataclass(frozen=True)
class TierResolution:
    """Result of evaluating one tier's env-var configuration.

    Either ``models`` (D-20-17 case (a)/(c) — MODELS list wins) OR
    ``triplet`` (D-20-17 case (b) — backward-compat single backend) is
    populated; never both. T17 (TierRegistry) consumes this structure to
    decide between wrapper-building and bare-single-backend construction.

    Attributes:
        tier: Tier name (lower-case: ``frontier`` / ``mid`` / ``small`` /
            ``imagegen``).
        models: Parsed ``[(provider, model), ...]`` list when MODELS wins,
            else ``None``.
        triplet: ``(provider, model, api_key)`` tuple when the backward-compat
            triplet wins, else ``None``.
        triplet_ignored: ``True`` for D-20-17 case (c) — emitted alongside
            INFO log so T17 can surface in TurnLog if useful.
    """

    tier: str
    models: list[tuple[Provider, str]] | None
    triplet: tuple[Provider, str, SecretStr] | None
    triplet_ignored: bool = False


class ProviderCredentialResolver:
    """Resolves a ``<provider>`` reference to credentials via env vars.

    Snapshots the env dict at construction so per-test injection is trivial
    (D-20-15 requires a clean separation from ``os.environ`` so test cases
    don't leak between each other).

    Per-provider env-var convention (read on every ``resolve`` call):

    * ``PERSONA_<PROVIDER>_API_KEY`` — the credential.
    * ``PERSONA_<PROVIDER>_BASE_URL`` — optional override; falls back to
      :data:`DEFAULT_BASE_URLS` (or ``""`` for ``local``).
    """

    def __init__(self, env: dict[str, str] | None = None) -> None:
        """Snapshot the environment.

        Args:
            env: Optional explicit env dict. When omitted, copies the current
                process environment so subsequent ``os.environ`` mutations
                don't change resolver behaviour mid-run.
        """
        self._env: dict[str, str] = dict(env if env is not None else os.environ)

    def resolve(self, provider: Provider) -> ProviderCredentials:
        """Resolve a provider reference to a :class:`ProviderCredentials`.

        Implements D-20-15's three-tier disposition:

        1. Provider in :data:`_KEYLESS_PROVIDERS` → return with ``api_key=None``.
        2. ``PERSONA_<PROVIDER>_API_KEY`` absent → raise
           :class:`ProviderCredentialMissingError`.
        3. ``PERSONA_<PROVIDER>_API_KEY`` set but empty/whitespace → raise
           :class:`AuthenticationError` (explicit-but-wrong is a louder bug
           than absent-entirely; mirrors Spec 02 §10 #8).

        Args:
            provider: Provider Literal symbol to resolve.

        Returns:
            A :class:`ProviderCredentials` with redacted ``api_key``.

        Raises:
            ProviderCredentialMissingError: Per D-20-15 condition (2).
            AuthenticationError: Per D-20-15 condition (3).
        """
        key_var = f"PERSONA_{provider.upper()}_API_KEY"
        base_var = f"PERSONA_{provider.upper()}_BASE_URL"
        base_url = self._env.get(base_var) or DEFAULT_BASE_URLS.get(provider, "")
        if provider == "cloudflare" and not self._env.get(base_var):
            # Cloudflare's chat base URL embeds the account id; assemble it from
            # the account-prefix template + PERSONA_CLOUDFLARE_ACCOUNT_ID unless
            # the operator supplied a full PERSONA_CLOUDFLARE_BASE_URL override
            # (handled by the branch guard above). Account id is REQUIRED — fail
            # fast with a config-layer error rather than emit a broken URL.
            account_id = (self._env.get(_CLOUDFLARE_ACCOUNT_ID_ENV) or "").strip()
            if not account_id:
                raise ProviderCredentialMissingError(
                    "provider 'cloudflare' requires an account id for its chat "
                    "base URL but none is configured",
                    context={"provider": provider, "env_var": _CLOUDFLARE_ACCOUNT_ID_ENV},
                )
            base_url = f"{base_url}{account_id}{_CLOUDFLARE_CHAT_URL_SUFFIX}"
        if provider in _KEYLESS_PROVIDERS:
            return ProviderCredentials(provider=provider, api_key=None, base_url=base_url)
        raw_key = self._env.get(key_var)
        if raw_key is None:
            raise ProviderCredentialMissingError(
                f"provider {provider!r} has no API key configured",
                context={"provider": provider, "env_var": key_var},
            )
        if not raw_key.strip():
            raise AuthenticationError(
                f"provider {provider!r} API key is empty",
                context={"provider": provider, "env_var": key_var},
            )
        return ProviderCredentials(provider=provider, api_key=SecretStr(raw_key), base_url=base_url)


def parse_models_list(tier_name: str, raw_value: str) -> list[tuple[Provider, str]]:
    """Parse a ``PERSONA_<TIER>_MODELS`` env var into ``(provider, model)`` slots.

    Implements D-20-17 case (d) malformed-detection + D-20-18 EXPLICIT
    REJECT of ``local`` / ``ollama``. The parser is strict: every failure
    mode is surfaced as a typed exception with a structured ``reason``
    context key so operators see exactly which entry failed.

    Format: comma-separated CSV; each entry is ``<provider>/<model>``
    (D-20-13 SLASH convention — NVIDIA + HuggingFace + Ollama canonical).

    Args:
        tier_name: Tier name (lower-case) — recorded in error context.
        raw_value: Raw env-var value as read by the caller.

    Returns:
        Parsed list of ``(provider, model)`` tuples preserving CSV order
        (fallback chain order is meaningful — see D-20-4).

    Raises:
        MalformedTierModelsError: D-20-17 case (d) — empty input, empty CSV
            entry, missing slash, unknown provider, or empty model component.
        LocalProviderInModelsListError: D-20-18 — ``local`` or ``ollama``
            token in any slot.
    """
    stripped = raw_value.strip()
    if not stripped:
        raise MalformedTierModelsError(
            f"PERSONA_{tier_name.upper()}_MODELS is empty after strip",
            context={
                "tier": tier_name,
                "value": raw_value,
                "reason": "empty_after_strip",
            },
        )

    results: list[tuple[Provider, str]] = []
    for position, raw_entry in enumerate(stripped.split(",")):
        entry = raw_entry.strip()
        if not entry:
            raise MalformedTierModelsError(
                f"PERSONA_{tier_name.upper()}_MODELS has empty CSV entry at position {position}",
                context={
                    "tier": tier_name,
                    "value": raw_value,
                    "reason": "empty_csv_entry",
                    "position": str(position),
                },
            )
        if "/" not in entry:
            raise MalformedTierModelsError(
                f"PERSONA_{tier_name.upper()}_MODELS entry {entry!r} missing "
                f"slash separator at position {position}",
                context={
                    "tier": tier_name,
                    "value": raw_value,
                    "reason": "missing_slash",
                    "position": str(position),
                },
            )
        provider_token, _, model_token = entry.partition("/")
        provider_token = provider_token.strip()
        model_token = model_token.strip()
        if provider_token not in _VALID_PROVIDERS:
            raise MalformedTierModelsError(
                f"PERSONA_{tier_name.upper()}_MODELS unknown provider "
                f"{provider_token!r} at position {position}",
                context={
                    "tier": tier_name,
                    "value": raw_value,
                    "reason": "unknown_provider",
                    "position": str(position),
                },
            )
        if not model_token:
            raise MalformedTierModelsError(
                f"PERSONA_{tier_name.upper()}_MODELS empty model after slash "
                f"at position {position}",
                context={
                    "tier": tier_name,
                    "value": raw_value,
                    "reason": "empty_model",
                    "position": str(position),
                },
            )
        # D-20-18 EXPLICIT REJECT — fires AFTER Provider Literal validation
        # (so unknown-provider takes precedence over reject-hint) BEFORE
        # credential lookup (so we don't waste a resolver call).
        if provider_token in _LOCAL_PROVIDERS_REJECTED_IN_MODELS_LISTS:
            raise LocalProviderInModelsListError(
                f"PERSONA_{tier_name.upper()}_MODELS rejects {provider_token!r} "
                f"at position {position}",
                context={
                    "tier": tier_name,
                    "position": str(position),
                    "hint": _LOCAL_REJECT_HINTS[provider_token],
                },
            )
        # Narrow str → Provider — the membership check above guarantees this.
        results.append((provider_token, model_token))  # type: ignore[arg-type]
    return results


#: OpenRouter slugs that are free BY DEFINITION although they carry no ``:free`` suffix.
#:
#: The free-mode filter's ``:free``-suffix test is a PROXY for "this model costs a
#: free-plan user nothing". The proxy is right for every ``<vendor>/<model>:free``
#: entry and wrong for exactly one slug, so the exception is named here rather than
#: widening the proxy into a guess:
#:
#: * ``openrouter/free``, OpenRouter's own free-only auto-router. It selects among
#:   free models ONLY, so it can never reach a paid model, which is the property the
#:   free plan's promise (D-M4-9, "free NEVER reaches a paid model") actually needs.
#:   Without this entry the suffix rule dropped it from BOTH free chains in free-mode,
#:   silently removing their last fallback (R9-179 item 1, observed at the v145 boot).
#:
#: Add a slug here only when the PROVIDER guarantees it is free by construction; a
#: model that merely happens to be free today belongs in the ``:free`` set.
FREE_BY_DEFINITION_OPENROUTER_SLUGS: Final[frozenset[str]] = frozenset({"openrouter/free"})


def is_free_openrouter_slot(provider: str, model: str) -> bool:
    """Is this ``(provider, model)`` slot an OpenRouter model that is free to call?

    The ONE place "is this free?" is decided. Shared by the free-mode tier filter
    (:func:`filter_openrouter_free_mode`) and by the api's free-daily-request meter,
    so what the filter KEEPS and what the meter COUNTS can never drift apart.

    Args:
        provider: The slot's provider token; only ``openrouter`` can be free here
            (every other provider bills, and a local backend is not metered).
        model: The slot's model token, e.g. ``qwen/qwen3-30b-a3b:free`` or ``free``.

    Returns:
        ``True`` for a ``:free``-suffixed OpenRouter model, or for a slug listed in
        :data:`FREE_BY_DEFINITION_OPENROUTER_SLUGS`.
    """
    if provider != "openrouter":
        return False
    return model.endswith(":free") or f"{provider}/{model}" in FREE_BY_DEFINITION_OPENROUTER_SLUGS


def filter_openrouter_free_mode(
    models: list[tuple[_ProviderStr, str]],
    *,
    mode: OpenRouterSubscriptionMode | None,
    tier_name: str,
    keep_free_suffix: bool,
) -> list[tuple[_ProviderStr, str]]:
    """Drop OpenRouter MODELS entries unusable under free-mode (D-22-2 / D-22-20).

    Pure, shared by the chat tier registry (``persona_runtime.tier``) and the
    image-gen factory (:func:`persona.imagegen._factory.load_image_backend_from_env`).
    The ``mode`` value is resolved by the runtime subscription resolver
    (Spec 22 T13) and injected as a plain string — keeping this filter in
    ``persona-core`` without a forward dependency on ``persona-runtime``.

    Two postures, selected by ``keep_free_suffix``:

    * **Chat (D-22-2, ``keep_free_suffix=True``):** in free-mode, drop only
      ``openrouter/<model>`` entries that are not free to call (free-tier users
      haven't paid for paid models). "Free to call" is
      :func:`is_free_openrouter_slot`: the ``:free`` suffix, OR membership of
      :data:`FREE_BY_DEFINITION_OPENROUTER_SLUGS`, today just ``openrouter/free``,
      the free-only auto-router, which the suffix test alone dropped (R9-179 item 1).
    * **Image (D-22-20, ``keep_free_suffix=False``):** in free-mode, drop ALL
      ``openrouter/<model>`` entries — zero ``:free`` image-output models exist,
      so any OpenRouter image entry is unusable; fail-fast over a call-time 402.
      The free-by-definition allowlist does NOT reprieve anything here: it names a
      chat auto-router, which is not an image-output model either.

    Non-OpenRouter entries are never touched, and ``mode != "free"`` (paid or
    ``None`` = mode unknown/probe-skipped) is a pass-through no-op (fail-soft:
    each dropped entry emits a WARN; the system continues).

    Args:
        models: Parsed ``(provider, model)`` slots, in chain order.
        mode: Resolved OpenRouter subscription mode, or ``None`` (no filtering).
        tier_name: Tier label for the WARN log (e.g. ``frontier`` / ``imagegen``).
        keep_free_suffix: ``True`` keeps ``:free`` entries (chat); ``False``
            drops every OpenRouter entry (image).

    Returns:
        The kept slots, preserving order.
    """
    if mode != "free":
        return models
    kept: list[tuple[_ProviderStr, str]] = []
    for provider, model in models:
        is_openrouter = provider == "openrouter"
        is_usable_free = keep_free_suffix and is_free_openrouter_slot(provider, model)
        if is_openrouter and not is_usable_free:
            _LOG.warning(
                "dropping OpenRouter entry unusable in free-mode "
                "tier={tier} model={model} keep_free_suffix={keep}",
                tier=tier_name,
                model=model,
                keep=keep_free_suffix,
            )
            continue
        kept.append((provider, model))
    return kept


_TRIPLET_SUFFIXES: Final[tuple[Literal["PROVIDER", "MODEL", "API_KEY"], ...]] = (
    "PROVIDER",
    "MODEL",
    "API_KEY",
)


def resolve_tier_config(
    tier_name: str,
    env: dict[str, str] | None = None,
) -> TierResolution:
    """Resolve one tier's env-var precedence per D-20-17.

    Four cases (D-20-17):

    * (a) MODELS set + triplet UNSET → MODELS wins, no log.
    * (b) MODELS unset/empty + triplet ALL THREE SET → triplet wins (backward-compat).
    * (c) MODELS set + triplet ≥1 SET → MODELS wins + INFO log naming
      ignored vars.
    * (d) MODELS malformed → :class:`MalformedTierModelsError` from the parser.

    Plus the partial-triplet branch: triplet 1-2 of 3 set + no MODELS →
    :class:`IncompleteTierConfigError` naming the missing vars.

    Args:
        tier_name: Tier name (lower-case: ``frontier``/``mid``/``small``/``imagegen``).
        env: Optional explicit env dict (defaults to ``os.environ``).

    Returns:
        A :class:`TierResolution` carrying either ``models`` or ``triplet``.

    Raises:
        MalformedTierModelsError: D-20-17 case (d).
        LocalProviderInModelsListError: D-20-18.
        IncompleteTierConfigError: Triplet partial-set + no MODELS.
    """
    snapshot: dict[str, str] = dict(env if env is not None else os.environ)
    prefix = f"PERSONA_{tier_name.upper()}_"
    models_var = f"{prefix}MODELS"
    triplet_vars = {suffix: f"{prefix}{suffix}" for suffix in _TRIPLET_SUFFIXES}

    raw_models = snapshot.get(models_var, "")
    models_set = bool(raw_models.strip())
    triplet_present = {
        suffix: bool(snapshot.get(var, "").strip()) for suffix, var in triplet_vars.items()
    }
    triplet_set_count = sum(1 for v in triplet_present.values() if v)

    if models_set:
        parsed = parse_models_list(tier_name, raw_models)
        triplet_ignored = triplet_set_count > 0
        if triplet_ignored:
            ignored = sorted(
                triplet_vars[suffix] for suffix, present in triplet_present.items() if present
            )
            _LOG.info(
                "PERSONA_{tier}_MODELS takes precedence; ignoring triplet vars "
                "{ignored} (D-20-17 case (c))",
                tier=tier_name.upper(),
                ignored=",".join(ignored),
            )
        return TierResolution(
            tier=tier_name,
            models=parsed,
            triplet=None,
            triplet_ignored=triplet_ignored,
        )

    # MODELS unset/empty — evaluate the triplet.
    if triplet_set_count == 0:
        # Neither side configured: T17 falls back to the global default
        # backend per the existing Spec 05 chain. Return an empty resolution
        # rather than raising — "no config" is a valid state for non-default
        # tiers (e.g., IMAGEGEN before the operator wires it up).
        return TierResolution(tier=tier_name, models=None, triplet=None)

    if triplet_set_count < 3:
        missing = sorted(
            triplet_vars[suffix] for suffix, present in triplet_present.items() if not present
        )
        raise IncompleteTierConfigError(
            f"tier {tier_name!r} triplet is incomplete; missing {missing}",
            context={
                "tier": tier_name,
                "missing_vars": ",".join(missing),
            },
        )

    # D-20-17 case (b): triplet wins, no log.
    provider_raw = snapshot[triplet_vars["PROVIDER"]].strip()
    if provider_raw not in _VALID_PROVIDERS:
        raise MalformedTierModelsError(
            f"PERSONA_{tier_name.upper()}_PROVIDER unknown provider {provider_raw!r}",
            context={
                "tier": tier_name,
                "value": provider_raw,
                "reason": "unknown_provider",
            },
        )
    model_raw = snapshot[triplet_vars["MODEL"]].strip()
    api_key_raw = snapshot[triplet_vars["API_KEY"]]
    return TierResolution(
        tier=tier_name,
        models=None,
        triplet=(provider_raw, model_raw, SecretStr(api_key_raw)),  # type: ignore[arg-type]
        triplet_ignored=False,
    )
