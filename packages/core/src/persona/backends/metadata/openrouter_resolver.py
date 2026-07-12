"""OpenRouter catalog → :class:`ModelMetadata` resolver (Spec 23 T4; D-23-5 primary).

Wraps Spec 22's :class:`~persona.backends.openrouter_catalog.OpenRouterCatalogClient`
(read-only) and maps catalog entries to :class:`ModelMetadata`. The OpenRouter
catalog is the **broad-coverage primary** source (300+ models, current pricing in
one call); the static tables are the authoritative fallback (D-23-5).

What the catalog **does** provide → mapped directly: per-token pricing → cost,
``context_length``, ``supported_parameters`` → ``tools_supported``,
``architecture.input_modalities`` → ``vision_supported``.

What the catalog does **not** provide → neutral documented sentinels:
``quality_benchmark`` (no benchmark in the catalog) and ``latency_p50_ms`` (no
latency in the catalog). These sentinels are only used for OpenRouter-ONLY models
(the long tail); the :class:`ChainedModelMetadataResolver` (T5) overlays the
static table's authored quality/latency whenever the model is also curated, and
the live :class:`FirstTokenLatencyTracker` supersedes latency once warmed (D-23-6).

Fail-open (D-22-1 / D-23-8): a catalog fetch failure degrades to an empty index
(every lookup → ``None``, i.e. defer to the static fallback) rather than crashing.
Refresh is startup-only + on-staleness via :meth:`refresh` (D-23-8) — never per
turn (D-22-11).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from persona.backends.errors import OpenRouterCatalogError
from persona.backends.model_metadata import ModelMetadata
from persona.backends.openrouter_catalog import strip_dynamic_variant
from persona.logging import get_logger

if TYPE_CHECKING:
    from persona.backends.openrouter_catalog import (
        OpenRouterCatalogClient,
        OpenRouterModelEntry,
    )

__all__ = ["OpenRouterModelMetadataResolver"]

_LOG = get_logger("backends.metadata.openrouter_resolver")

# USD per single token → cents per 1k tokens: × 1000 tokens × 100 cents.
_USD_PER_TOKEN_TO_CENTS_PER_1K: Final[float] = 100_000.0

# Neutral sentinels for fields the catalog does not publish (documented above).
# Only surface for OpenRouter-only models; the chained resolver overlays the
# static table's authored values, and the live latency tracker supersedes latency.
_UNKNOWN_QUALITY: Final[float] = 0.5
_UNKNOWN_LATENCY_MS: Final[float] = 800.0
# Conservative context floor when the catalog omits context_length (rare).
_FALLBACK_CONTEXT_LENGTH: Final[int] = 8_192


class OpenRouterModelMetadataResolver:
    """Resolve :class:`ModelMetadata` from the OpenRouter catalog (D-23-5 primary).

    Implements the
    :class:`~persona.backends.model_metadata.ModelMetadataResolver` Protocol
    structurally. The catalog index is built lazily on first :meth:`resolve`
    (the client caches the HTTP fetch) and reused; :meth:`refresh` forces a
    re-fetch for the D-23-8 on-staleness path.

    Args:
        client: A constructed
            :class:`~persona.backends.openrouter_catalog.OpenRouterCatalogClient`.
    """

    def __init__(self, client: OpenRouterCatalogClient) -> None:
        self._client = client
        self._index: dict[str, ModelMetadata] | None = None

    def resolve(self, model_id: str) -> ModelMetadata | None:
        """Return catalog metadata for ``model_id``, or ``None`` on a miss/failure.

        Dynamic routing variants (``:nitro`` etc., D-22-6) are stripped before
        lookup; static variants (``:free`` etc.) are looked up verbatim.
        """
        index = self._ensure_index()
        return index.get(strip_dynamic_variant(model_id))

    def resolve_no_fetch(self, model_id: str) -> ModelMetadata | None:
        """Like :meth:`resolve`, but NEVER triggers the catalog fetch (Spec M2, D-M2-1).

        The turn-path cost estimator calls this: pricing must never block or
        delay a turn (spec M2 §2 invariant), so a cold — not-yet-warmed —
        index is a plain miss (``None``, defer to static / unpriced) rather
        than a synchronous HTTP fetch on the event loop. The index is warmed
        off the turn path (api lifespan / the D-M2-6 TTL refresh); once warm,
        this is the same dict lookup as :meth:`resolve`.
        """
        if self._index is None:
            return None
        return self._index.get(strip_dynamic_variant(model_id))

    def refresh(self) -> None:
        """Force a catalog re-fetch + re-index (D-23-8 on-staleness path)."""
        self._index = None
        self._ensure_index(force_refresh=True)

    @property
    def warm(self) -> bool:
        """Whether the id→metadata index has been built (Spec M2, D-M2-6).

        ``False`` on a cold resolver — the state in which the turn path's
        :meth:`resolve_no_fetch` answers ``None``. The lifespan freshness task
        reads this to decide whether a boot-time index build is still needed
        when the client cache was already warmed by another consumer.
        """
        return self._index is not None

    def reindex(self) -> None:
        """Rebuild the id→metadata index from the client's CURRENT cache (Spec M2, D-M2-6).

        The TTL companion to :meth:`refresh`: after
        ``OpenRouterCatalogClient.refresh_if_stale`` already fetched a fresh
        catalog, the derived index must follow — WITHOUT forcing a second
        HTTP fetch (``refresh`` would). Also serves as the boot-time warm's
        index build (the client's lazy first fetch happens inside, off-turn
        by the caller's contract). Fail-open like every index build.
        """
        self._index = None
        self._ensure_index()

    # ------------------------------------------------------------------ #

    def _ensure_index(self, *, force_refresh: bool = False) -> dict[str, ModelMetadata]:
        """Build (once) and return the id→metadata index; fail-open to empty."""
        if self._index is not None:
            return self._index
        try:
            entries = self._client.list_models(force_refresh=force_refresh)
        except OpenRouterCatalogError as exc:
            # D-22-1 fail-open: degrade to empty index (defer to static fallback),
            # never crash the resolver. The static resolver still serves curated
            # models; the chain returns None only when ALL sources miss.
            _LOG.warning(
                "openrouter catalog unavailable; metadata resolver degraded to empty index",
                reason=exc.context.get("reason", ""),
            )
            self._index = {}
            return self._index
        index: dict[str, ModelMetadata] = {}
        skipped = 0
        for entry in entries:
            # The live catalog carries entries our ModelMetadata rejects — e.g.
            # negative *sentinel* pricing ("-1" → variable / not-applicable, which
            # would violate the cost ``ge=0`` bound). These are genuinely unusable
            # for cost-based scoring (a model with no fixed price cannot be scored),
            # so skipping is benign — same fail-soft posture as the catalog parser
            # in OpenRouterCatalogClient.list_models (D-22-1). A skipped entry is a
            # metadata miss → static fallback → rule-based (criterion 9).
            #
            # R9-018: this used to WARN per entry, flooding the log (×N per boot
            # for the handful of sentinel-priced models). Detail stays at DEBUG;
            # the operator gets ONE summary WARNING with the count below.
            try:
                metadata = self._entry_to_metadata(entry)
            except (ValueError, TypeError) as exc:
                skipped += 1
                _LOG.debug(
                    "skipping openrouter catalog entry with invalid metadata",
                    model_id=entry.id,
                    error=type(exc).__name__,
                )
                continue
            index[entry.id] = metadata
            if entry.canonical_slug and entry.canonical_slug not in index:
                index[entry.canonical_slug] = metadata
        if skipped:
            _LOG.warning(
                "skipped openrouter catalog entries with incomplete metadata",
                skipped=skipped,
                indexed=len(index),
            )
        self._index = index
        return self._index

    @staticmethod
    def _entry_to_metadata(entry: OpenRouterModelEntry) -> ModelMetadata:
        """Map one catalog entry to :class:`ModelMetadata` (neutral quality/latency)."""
        return ModelMetadata(
            cost_input_per_1k_tokens=float(entry.pricing.prompt) * _USD_PER_TOKEN_TO_CENTS_PER_1K,
            cost_output_per_1k_tokens=(
                float(entry.pricing.completion) * _USD_PER_TOKEN_TO_CENTS_PER_1K
            ),
            latency_p50_ms=_UNKNOWN_LATENCY_MS,
            quality_benchmark=_UNKNOWN_QUALITY,
            tools_supported=entry.supports_tools,
            vision_supported=entry.supports_vision,
            context_length=entry.context_length or _FALLBACK_CONTEXT_LENGTH,
            # OpenRouter pricing is derived/best-effort (R-23-5): mark unverified
            # so the scorer treats it cautiously vs authored static-table cost.
            cost_verified_at_deploy=False,
        )
