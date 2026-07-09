"""Model catalog service — curated shortlist + browse-all with OpenRouter prices.

Spec M1 T5. ``GET /v1/models`` (``routes/models.py``) is a thin HTTP wrapper over
:func:`list_models`. This module composes the EXISTING read-only OpenRouter
collaborators — Spec 22's
:class:`~persona.backends.openrouter_catalog.OpenRouterCatalogClient` and Spec 23's
:class:`~persona.backends.metadata.openrouter_resolver.OpenRouterModelMetadataResolver`
— no new network client, no second cache layer: the catalog client already caches
its one HTTP fetch in-process (D-22-5), and :func:`_default_client` caches the
CLIENT ITSELF at module scope so every request reuses that same in-process cache
(construction is cheap / no network, D-22-11).

Two lists, one shared validity filter (spec_M1_design.md §4):

* ``recommended`` — :data:`CURATED_MODEL_IDS` ∩ valid catalog entries, in curated
  order — the hand-kept "what we suggest" shortlist. ONE edit point (§4.1).
* ``all`` — every valid catalog entry (the "Browse all" power path), each flagged
  with ``recommended`` so the UI can highlight the shortlist inline.

"Valid" = chat-capable (``"text"`` in the entry's ``architecture.output_modalities``
— excludes a hypothetical embedding-/audio-only-output entry; every entry in the
live catalog passes this today, so it is a defensive no-op filter, not a live
exclusion) AND :meth:`OpenRouterModelMetadataResolver.resolve` returns metadata for
the id. The resolver's own ``_ensure_index`` already drops entries with invalid
pricing — the R9-018 ``-1`` sentinel class OpenRouter uses for its own meta-router
aliases (``openrouter/auto`` etc.) — and logs ONE summary WARNing per catalog
fetch for the whole skipped set; this module does NOT re-log per entry when
:meth:`resolve` returns ``None`` — that would be exactly the log spam R9-018 fixed.
There is no separate "deprecated" boolean on :class:`OpenRouterModelEntry` (the
live catalog does not expose one to this codebase's ``extra=\"ignore\"`` model) —
the invalid-pricing check IS the only "unusable / deprecated-class" signal
available, so it does double duty as both filters named in the task brief.

Fail-open (D-22-1 precedent): no ``PERSONA_OPENROUTER_API_KEY`` configured, or a
catalog fetch failure, both yield ``stale=True`` + empty ``models`` — never an
exception, so ``GET /v1/models`` always returns 200 (the route has nothing to
catch).

Price unit conversion: :class:`ModelMetadata` denominates cost in CENTS per 1k
tokens (``packages/core/src/persona/backends/model_metadata.py`` — "Provider cost
per 1k INPUT tokens (cents)"; verified against ``OpenRouterModelMetadataResolver
._entry_to_metadata``'s ``* 100_000`` USD-per-token → cents-per-1k conversion). The
catalog API surface promises USD per 1M tokens (the design doc's "$in / $out per
1M" price tag, §6). :func:`_usd_per_1m` is the ONE conversion point:

    USD/1M = (cents_per_1k / 100 cents-per-usd) * (1_000_000 / 1_000 tokens-per-1k)
           = cents_per_1k * 10

Pinned by a unit test against a hand-computed value (1 cent/1k tokens = $0.01/1k =
$10/1M) and cross-checked against a live catalog entry's known price shape.

References:
    docs/specs/phase3/spec_M1_persona_model_selection/spec_M1_design.md §4/§4.1/§6;
    docs/specs/phase3/spec_M1_persona_model_selection/spec_M1_plan.md Task 5.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Final, Literal

from persona.backends.errors import OpenRouterCatalogError
from persona.backends.metadata.openrouter_resolver import OpenRouterModelMetadataResolver
from persona.backends.openrouter_catalog import OpenRouterCatalogClient
from persona.logging import get_logger

if TYPE_CHECKING:
    from persona.backends.model_metadata import ModelMetadata
    from persona.backends.openrouter_catalog import OpenRouterModelEntry

__all__ = [
    "CURATED_MODEL_IDS",
    "ModelCatalogEntry",
    "ModelCatalogResult",
    "list_models",
]

_LOG = get_logger("api.services.model_catalog_service")

# Same env vars every other OpenRouter collaborator in this codebase reads (the
# runtime's openrouter_subscription resolver and the intelligent-router factory
# builder) — one source of truth for the credential, not a fourth redefinition.
_API_KEY_ENV = "PERSONA_OPENROUTER_API_KEY"
_BASE_URL_ENV = "PERSONA_OPENROUTER_BASE_URL"

#: The curated "what we suggest" shortlist (spec_M1_design.md §4.1). ONE edit
#: point — add / remove / replace an id here to change the offering; an id that
#: stops resolving simply drops out of ``recommended`` (fail-open), it never
#: errors. Live-validated against the OpenRouter catalog on 2026-07-09 (see
#: task-5-report.md for the full validation run): two ids from the original spec
#: intent had moved on and were replaced with their current same-family/size
#: successor —``qwen/qwen3-235b-a22b-instruct`` → ``qwen/qwen3-235b-a22b-2507``
#: (live display name "...Instruct 2507") and ``mistralai/ministral-8b`` →
#: ``mistralai/ministral-8b-2512``. The other eight ids from spec §4.1 validated
#: unchanged.
CURATED_MODEL_IDS: Final[tuple[str, ...]] = (
    "anthropic/claude-sonnet-4.6",  # top quality
    "google/gemini-2.5-pro",  # top quality
    "openai/gpt-5",  # top quality
    "z-ai/glm-4.6",  # balanced / value
    "deepseek/deepseek-chat",  # balanced / value
    "qwen/qwen3-235b-a22b-2507",  # balanced / value (was qwen3-235b-a22b-instruct)
    "meta-llama/llama-3.3-70b-instruct",  # cheap / fast
    "google/gemini-2.5-flash",  # cheap / fast
    "mistralai/ministral-8b-2512",  # cheap / fast (was ministral-8b)
    "deepseek/deepseek-r1",  # reasoning
)

# ModelMetadata.cost_*_per_1k_tokens is CENTS per 1k tokens (model_metadata.py
# docstring; cross-checked against OpenRouterModelMetadataResolver's own
# USD-per-token -> cents-per-1k factor of 100_000). USD/1M = cents_per_1k * 10 —
# see the module docstring for the derivation.
_CENTS_PER_1K_TO_USD_PER_1M: Final[float] = 10.0


@dataclass(frozen=True, slots=True)
class ModelCatalogEntry:
    """One model offered by ``GET /v1/models`` (service-layer shape; route wraps it).

    Attributes:
        id: The canonical OpenRouter model id (``"anthropic/claude-sonnet-4.6"``).
        label: Human display name (the catalog's ``name``, falling back to ``id``
            when the catalog omits it).
        provider: The id's prefix before the first ``/`` (``"anthropic"``).
        input_price_per_1m: USD per 1,000,000 INPUT tokens.
        output_price_per_1m: USD per 1,000,000 OUTPUT tokens.
        context_length: Maximum context window, tokens.
        tools_supported: Whether the model advertises native tool calling.
        recommended: Whether this id is in :data:`CURATED_MODEL_IDS` — always
            ``True`` for ``scope="recommended"``; marks the shortlist inline for
            ``scope="all"``.
    """

    id: str
    label: str
    provider: str
    input_price_per_1m: float
    output_price_per_1m: float
    context_length: int
    tools_supported: bool
    recommended: bool


@dataclass(frozen=True, slots=True)
class ModelCatalogResult:
    """The full ``GET /v1/models`` payload for one ``scope`` (service-layer shape).

    Attributes:
        models: The filtered, ordered entries for the requested scope.
        source: Always ``"openrouter"`` — the sole catalog source today.
        stale: ``True`` when the catalog could not be fetched (no client
            configured or the fetch failed) — ``models`` is then empty, per the
            fail-open contract (never an exception, never a 500).
    """

    models: tuple[ModelCatalogEntry, ...]
    source: Literal["openrouter"]
    stale: bool


def _usd_per_1m(cents_per_1k_tokens: float) -> float:
    """Convert :class:`ModelMetadata`'s cents-per-1k-token cost to a USD-per-1M price tag."""
    return cents_per_1k_tokens * _CENTS_PER_1K_TO_USD_PER_1M


@lru_cache(maxsize=1)
def _default_client() -> OpenRouterCatalogClient | None:
    """The module-scoped catalog client (construction is cheap / no network, D-22-11).

    ``None`` when ``PERSONA_OPENROUTER_API_KEY`` is unset — the same zero-touch
    opt-in posture every other OpenRouter collaborator in this codebase shares.
    Cached for the process lifetime: one client, one in-process HTTP-fetch cache
    (D-22-5) shared by every ``GET /v1/models`` request — no second cache layer
    on top of it.
    """
    api_key = os.environ.get(_API_KEY_ENV, "").strip()
    if not api_key:
        return None
    base_url = os.environ.get(_BASE_URL_ENV, "").strip() or None
    return OpenRouterCatalogClient(api_key, base_url=base_url)


def _is_chat_capable(entry: OpenRouterModelEntry) -> bool:
    """Whether the catalog entry can serve chat completions (produces text output)."""
    return "text" in entry.architecture.output_modalities


def _to_catalog_entry(
    entry: OpenRouterModelEntry, metadata: ModelMetadata, *, recommended: bool
) -> ModelCatalogEntry:
    """Map one valid ``(entry, metadata)`` pair to the API-facing shape."""
    return ModelCatalogEntry(
        id=entry.id,
        label=entry.name or entry.id,
        provider=entry.id.split("/", 1)[0],
        input_price_per_1m=_usd_per_1m(metadata.cost_input_per_1k_tokens),
        output_price_per_1m=_usd_per_1m(metadata.cost_output_per_1k_tokens),
        context_length=metadata.context_length,
        tools_supported=metadata.tools_supported,
        recommended=recommended,
    )


def list_models(
    *,
    scope: Literal["recommended", "all"] = "recommended",
    client: OpenRouterCatalogClient | None = None,
) -> ModelCatalogResult:
    """Build the ``GET /v1/models`` payload for ``scope`` (spec_M1_design.md §4).

    Fail-open: no client configured (no API key) or a catalog fetch failure both
    yield ``stale=True`` with an empty ``models`` tuple — never an exception, so
    the route always returns 200.

    Args:
        scope: ``"recommended"`` (default) — :data:`CURATED_MODEL_IDS` ∩ valid, in
            curated order. ``"all"`` — every valid catalog entry, each flagged
            with ``recommended``.
        client: Override the module-scoped client. Tests inject a fake with no
            network — the fake need only implement
            ``list_models(*, force_refresh: bool = False) -> tuple[OpenRouterModelEntry, ...]``,
            the same shape :class:`OpenRouterCatalogClient` exposes.

    Returns:
        The models for ``scope``, always with ``source="openrouter"``.
    """
    catalog_client = client if client is not None else _default_client()
    if catalog_client is None:
        _LOG.warning(
            "GET /v1/models: PERSONA_OPENROUTER_API_KEY not configured; "
            "serving stale/empty (fail-open)",
            scope=scope,
        )
        return ModelCatalogResult(models=(), source="openrouter", stale=True)

    try:
        entries = catalog_client.list_models()
    except OpenRouterCatalogError as exc:
        _LOG.warning(
            "GET /v1/models: openrouter catalog fetch failed; serving stale/empty (fail-open)",
            scope=scope,
            reason=exc.context.get("reason", ""),
        )
        return ModelCatalogResult(models=(), source="openrouter", stale=True)

    resolver = OpenRouterModelMetadataResolver(catalog_client)
    curated_ids = frozenset(CURATED_MODEL_IDS)

    # Pre-select the candidate entries per scope; the validity filter below
    # (chat-capable + resolvable pricing) is then applied identically to both,
    # so "recommended" and "all" can never disagree on what counts as valid.
    candidates: tuple[OpenRouterModelEntry, ...]
    if scope == "recommended":
        by_id = {entry.id: entry for entry in entries}
        candidates = tuple(by_id[model_id] for model_id in CURATED_MODEL_IDS if model_id in by_id)
    else:
        candidates = entries

    models: list[ModelCatalogEntry] = []
    for entry in candidates:
        if not _is_chat_capable(entry):
            continue
        metadata = resolver.resolve(entry.id)
        if metadata is None:
            # Invalid/unusable pricing (R9-018 -1 sentinel class) or otherwise
            # unresolvable. The resolver already logged ONE summary WARNing for
            # the whole fetch; a per-entry re-log here would be exactly the log
            # spam R9-018 fixed.
            continue
        models.append(_to_catalog_entry(entry, metadata, recommended=entry.id in curated_ids))

    return ModelCatalogResult(models=tuple(models), source="openrouter", stale=False)
