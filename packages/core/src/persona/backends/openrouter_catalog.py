"""OpenRouter catalog client + subscription-state resolution (Spec 22 T10/T11).

Two coupled deliverables in one module:

* **T10 — :class:`OpenRouterCatalogClient`.** A *synchronous* ``httpx`` client
  (D-22-11 — startup / config-reload one-shots, never on the turn path)
  wrapping ``GET /api/v1/models`` (the model catalog) and ``GET /api/v1/key``
  (the subscription-mode probe, D-22-3 — NOT ``/api/v1/credits``, which the
  current docs gate behind a management key). The parsed catalog is the
  metadata source Spec 23 consumes for intelligent routing; this module is
  therefore a **stable public surface** — its method signatures are pinned by
  the T12 contract test.

* **T11 — :class:`OpenRouterSubscriptionState` + resolution functions.** Pure
  mappers from a key-info probe (or a probe failure) to a frozen
  subscription-state value. The *runtime* resolver (Spec 22 T13,
  ``persona_runtime.openrouter_subscription``) owns env reading + the
  fail-open policy; these functions stay side-effect-free for testability.

Response models use ``extra="ignore"`` (D-22-12 — a documented deviation from
the project's ``extra="forbid"`` default; OpenRouter adds response fields
without notice, verified live). Pricing values are decimal **strings** in USD
per single token (D-22-13) and parse to :class:`~decimal.Decimal`. Our own
boundary type :class:`OpenRouterSubscriptionState` keeps ``extra="forbid"``.

References:
    docs/specs/phase2/spec_22/decisions.md D-22-1/3/5/9/11/12/13/14;
    research.md §R-22-1 (endpoint shapes, verified 2026-06-10).
"""

from __future__ import annotations

from datetime import datetime  # noqa: TC003 — Pydantic needs runtime access
from decimal import Decimal
from typing import Any, Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from persona.backends.config import DEFAULT_BASE_URLS
from persona.backends.errors import (
    AuthenticationError,
    OpenRouterBalanceProbeError,
    OpenRouterCatalogError,
)
from persona.logging import get_logger

__all__ = [
    "OpenRouterArchitecture",
    "OpenRouterCatalogClient",
    "OpenRouterKeyInfo",
    "OpenRouterModelEntry",
    "OpenRouterPricing",
    "OpenRouterSubscriptionMode",
    "OpenRouterSubscriptionState",
    "free_mode_fallback",
    "subscription_state_from_key_info",
]

_LOG = get_logger("backends.openrouter_catalog")

OpenRouterSubscriptionMode = Literal["free", "paid"]

# Dynamic routing-variant suffixes (D-22-6). These are NOT separate catalog
# entries, so a catalog lookup by exact slug must strip them first. Static
# variants (:free / :extended / :thinking / :beta) ARE separate entries and
# are looked up verbatim. Kept local to decouple this module from
# ``openai_compat`` (the canonical set lives there for tier-3 inference).
_DYNAMIC_VARIANTS: Final[frozenset[str]] = frozenset({"nitro", "floor", "exacto", "online"})


# ---------------------------------------------------------------------------
# Response models (extra="ignore" — D-22-12)
# ---------------------------------------------------------------------------


class OpenRouterPricing(BaseModel):
    """Per-token pricing for one catalog model (D-22-13).

    Values arrive as decimal strings in USD per single token (``"0.000003"``
    = $3/Mtok) and parse to :class:`~decimal.Decimal`. Only ``prompt`` and
    ``completion`` are guaranteed present; every other pricing key
    (``image``, ``web_search``, ``input_cache_read``, the docs-listed
    ``discount`` number, ...) is dropped by ``extra="ignore"``.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    prompt: Decimal = Decimal("0")
    completion: Decimal = Decimal("0")


class OpenRouterArchitecture(BaseModel):
    """Modality / tokenizer descriptor for one catalog model.

    ``input_modalities`` is the authoritative vision signal (``"image"`` in
    the array → vision-capable, D-22-10b); the derived ``modality`` string,
    ``tokenizer``, and ``instruct_type`` are NOT capability signals.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    tokenizer: str | None = None


class OpenRouterModelEntry(BaseModel):
    """One parsed ``GET /api/v1/models`` catalog entry.

    The capability properties implement D-22-10b: tools is gated on
    ``"tools" in supported_parameters`` (NOT ``tool_choice``); vision on
    ``"image" in architecture.input_modalities`` (the array, never the
    derived modality string). ``is_free`` follows D-22-14 — the ``:free``
    suffix is authoritative, NOT zero pricing.

    ``expiration_date`` IS this catalog's deprecation signal (an ISO date
    string; set on ~5/346 live entries, verified 2026-07-09 — see
    docs/specs/phase2/spec_22/research.md). It is kept as the raw string
    (no date parsing) because this module is a pure catalog parser, not a
    policy layer — consumers that need an "announced-EOL" filter (M1-T5)
    key off *presence* (``is not None``), not the parsed value.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    id: str
    canonical_slug: str | None = None
    name: str = ""
    context_length: int | None = None
    expiration_date: str | None = None
    pricing: OpenRouterPricing = Field(default_factory=OpenRouterPricing)
    architecture: OpenRouterArchitecture = Field(default_factory=OpenRouterArchitecture)
    supported_parameters: tuple[str, ...] = ()

    @property
    def is_free(self) -> bool:
        """Whether this is a free-tier model (``:free`` suffix — D-22-14)."""
        return self.id.endswith(":free")

    @property
    def supports_tools(self) -> bool:
        """Whether the model advertises native tool calling (D-22-10b)."""
        return "tools" in self.supported_parameters

    @property
    def supports_vision(self) -> bool:
        """Whether the model accepts image input (D-22-10b)."""
        return "image" in self.architecture.input_modalities


class OpenRouterKeyInfo(BaseModel):
    """Parsed ``GET /api/v1/key`` response data (the D-22-3 probe).

    ``is_free_tier`` is the subscription-mode signal: it reports whether the
    account has *ever* purchased credits (lifetime-purchase, not current
    balance), which aligns with OpenRouter's free-tier request/day caps.
    ``limit_remaining`` is the per-key USD headroom (``None`` when no per-key
    limit is configured) — it is NOT the account balance.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    is_free_tier: bool = False
    limit: Decimal | None = None
    limit_remaining: Decimal | None = None
    usage: Decimal = Decimal("0")


# ---------------------------------------------------------------------------
# Catalog client (T10)
# ---------------------------------------------------------------------------


class OpenRouterCatalogClient:
    """Synchronous client for the OpenRouter catalog + key-info endpoints.

    Construction is cheap (no network). :meth:`list_models` fetches and caches
    the catalog in-process (D-22-5); :meth:`get_key_info` runs the
    subscription-mode probe. Both are startup / config-reload operations
    (D-22-1 / D-22-11) — never call them on the per-turn path.

    The HTTP transport is injectable (``transport=``) so tests drive a
    :class:`httpx.MockTransport` without real network.
    """

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str | None = None,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        """Construct the client.

        Args:
            api_key: OpenRouter inference key (``sk-or-v1-...``). Sent as a
                bearer token on every request.
            base_url: Override for the API root; defaults to
                :data:`DEFAULT_BASE_URLS` ``["openrouter"]``.
            timeout_s: Per-request timeout in seconds.
            transport: Optional ``httpx`` transport (tests inject a
                :class:`httpx.MockTransport`).
        """
        self._base_url = base_url or DEFAULT_BASE_URLS["openrouter"]
        self._client = httpx.Client(
            base_url=self._base_url,
            timeout=timeout_s,
            headers={"Authorization": f"Bearer {api_key}"},
            transport=transport,
        )
        self._models_cache: tuple[OpenRouterModelEntry, ...] | None = None

    def list_models(self, *, force_refresh: bool = False) -> tuple[OpenRouterModelEntry, ...]:
        """Fetch (and cache) the OpenRouter model catalog (D-22-1 / D-22-5).

        Returns the in-process cache when present unless ``force_refresh`` is
        set (the config-reload path). ``~``-prefixed routing-alias ids are
        dropped at parse (D-22-14); individual entries that fail validation
        are skipped with a WARN (the catalog is best-effort metadata) — only
        a structurally broken envelope raises.

        Returns:
            The parsed catalog entries, in catalog order.

        Raises:
            OpenRouterCatalogError: network failure, non-2xx status, or an
                unparseable top-level envelope. ``context["reason"]`` is one
                of ``timeout`` / ``http_error`` / ``malformed_response``.
        """
        if self._models_cache is not None and not force_refresh:
            return self._models_cache
        payload = self._get_json("models", error_cls=OpenRouterCatalogError)
        data = payload.get("data")
        if not isinstance(data, list):
            raise OpenRouterCatalogError(
                "openrouter /models response missing a 'data' array",
                context={"provider": "openrouter", "reason": "malformed_response"},
            )
        entries: list[OpenRouterModelEntry] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            if str(item.get("id", "")).startswith("~"):
                # D-22-14 — routing-alias ids are filtered out.
                continue
            try:
                entries.append(OpenRouterModelEntry.model_validate(item))
            except (ValueError, TypeError) as exc:
                _LOG.warning(
                    "skipping unparseable openrouter catalog entry",
                    model_id=str(item.get("id", "<missing>")),
                    error=str(exc),
                )
        self._models_cache = tuple(entries)
        return self._models_cache

    def get_key_info(self) -> OpenRouterKeyInfo:
        """Run the subscription-mode probe via ``GET /api/v1/key`` (D-22-3).

        Returns:
            Parsed key info; ``is_free_tier`` drives the free/paid mode.

        Raises:
            AuthenticationError: HTTP 401 — invalid/missing key (D-22-9,
                fail-loud; distinct from a transient probe failure).
            OpenRouterBalanceProbeError: timeout, non-401 non-2xx status, or
                unparseable body. ``context["reason"]`` is one of ``timeout``
                / ``http_error`` / ``malformed_response``. The runtime
                resolver (T13) treats this as a conservative free-mode
                fallback (D-22-3).
        """
        payload = self._get_json(
            "key",
            error_cls=OpenRouterBalanceProbeError,
            auth_error_is_loud=True,
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise OpenRouterBalanceProbeError(
                "openrouter /key response missing a 'data' object",
                context={"provider": "openrouter", "reason": "malformed_response"},
            )
        try:
            return OpenRouterKeyInfo.model_validate(data)
        except (ValueError, TypeError) as exc:
            raise OpenRouterBalanceProbeError(
                "openrouter /key response failed to parse",
                context={"provider": "openrouter", "reason": "malformed_response"},
            ) from exc

    def close(self) -> None:
        """Close the underlying ``httpx.Client``."""
        self._client.close()

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _get_json(
        self,
        path: str,
        *,
        error_cls: type[OpenRouterCatalogError | OpenRouterBalanceProbeError],
        auth_error_is_loud: bool = False,
    ) -> dict[str, Any]:
        """GET ``path`` and return the JSON object, translating failures.

        401 maps to :class:`AuthenticationError` when ``auth_error_is_loud``
        (the key probe — D-22-9); otherwise non-2xx maps to ``error_cls``
        with a structured ``reason``.
        """
        try:
            response = self._client.get(path)
            response.raise_for_status()
            payload = response.json()
        except httpx.TimeoutException as exc:
            raise error_cls(
                f"openrouter /{path} timed out",
                context={"provider": "openrouter", "reason": "timeout"},
            ) from exc
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            if auth_error_is_loud and status == 401:
                raise AuthenticationError(
                    f"openrouter rejected the API key (HTTP {status})",
                    context={"provider": "openrouter", "status_code": str(status)},
                ) from exc
            raise error_cls(
                f"openrouter /{path} returned HTTP {status}",
                context={
                    "provider": "openrouter",
                    "reason": "http_error",
                    "status_code": str(status),
                },
            ) from exc
        except httpx.HTTPError as exc:
            raise error_cls(
                f"openrouter /{path} request failed",
                context={"provider": "openrouter", "reason": "http_error"},
            ) from exc
        if not isinstance(payload, dict):
            raise error_cls(
                f"openrouter /{path} returned a non-object body",
                context={"provider": "openrouter", "reason": "malformed_response"},
            )
        return payload


# ---------------------------------------------------------------------------
# Subscription state (T11)
# ---------------------------------------------------------------------------


class OpenRouterSubscriptionState(BaseModel):
    """Resolved OpenRouter subscription mode (Spec 22 surface #5, D-22-3).

    Our own boundary type — frozen + ``extra="forbid"`` (unlike the
    OpenRouter response models above). The spec sketch named a
    ``balance_cents`` field; D-22-3 reshaped the probe to ``/api/v1/key``,
    whose signal is ``is_free_tier`` + per-key ``limit_remaining`` (USD), so
    this state carries those instead of a cents balance.

    Attributes:
        mode: ``"free"`` (only ``:free`` models usable) or ``"paid"``.
        is_free_tier: The probed lifetime-purchase flag (``True`` when the
            account has never bought credits). Also ``True`` on a
            conservative free-mode fallback.
        limit_remaining: Per-key USD headroom from the probe, or ``None``.
        last_checked_at: When the probe ran (tz-aware UTC).
        probe_failed: ``True`` when ``mode="free"`` is a *fallback* from a
            probe failure (timeout / 5xx) rather than a confirmed
            ``is_free_tier`` — lets operators distinguish "free by policy"
            from "free because we couldn't confirm paid" (D-22-3).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: OpenRouterSubscriptionMode
    is_free_tier: bool
    limit_remaining: Decimal | None = None
    last_checked_at: datetime
    probe_failed: bool = False


def subscription_state_from_key_info(
    key_info: OpenRouterKeyInfo, *, checked_at: datetime
) -> OpenRouterSubscriptionState:
    """Map a successful key-info probe to a subscription state (D-22-3).

    ``is_free_tier=True`` → free-mode; ``False`` → paid-mode. Call-time
    balance exhaustion is handled separately by the 402→fallback path
    (D-22-15), not here.
    """
    return OpenRouterSubscriptionState(
        mode="free" if key_info.is_free_tier else "paid",
        is_free_tier=key_info.is_free_tier,
        limit_remaining=key_info.limit_remaining,
        last_checked_at=checked_at,
        probe_failed=False,
    )


def free_mode_fallback(*, checked_at: datetime, reason: str) -> OpenRouterSubscriptionState:
    """Build the conservative free-mode state for a failed probe (D-22-3).

    Timeout / 5xx on the probe cannot confirm paid credits, so the resolver
    degrades to free-mode and flags ``probe_failed=True``. The ``reason`` is
    logged by the caller; it is not stored on the frozen state.
    """
    _LOG.warning(
        "openrouter balance probe failed; falling back to free-mode",
        reason=reason,
    )
    return OpenRouterSubscriptionState(
        mode="free",
        is_free_tier=True,
        limit_remaining=None,
        last_checked_at=checked_at,
        probe_failed=True,
    )


def strip_dynamic_variant(slug: str) -> str:
    """Strip a dynamic routing-variant suffix from an OpenRouter slug (D-22-6).

    ``anthropic/claude-3.5-sonnet:nitro`` → ``anthropic/claude-3.5-sonnet``.
    Static variants (``:free`` etc.) are separate catalog entries and are
    left intact. Helper for catalog consumers (e.g. Spec 23) doing
    exact-slug lookups against :meth:`OpenRouterCatalogClient.list_models`.
    """
    base, sep, variant = slug.partition(":")
    if sep and variant in _DYNAMIC_VARIANTS:
        return base
    return slug
