"""Resolver-backed turn-cost computation (Spec M2, D-M2-1 / D-M2-3).

One pricing truth: per-turn cost estimates come from the Spec-22/23 metadata
resolver chain (static-authoritative-first, OpenRouter-catalog-for-coverage),
and OpenRouter-served turns carry the response's REAL cost when captured
(``TokenUsage.cost_usd``, D-M2-3). This module replaces the deleted
``persona_runtime.logging`` ``_PRICE_TABLE`` estimator (S05-3 / D-25-7) — the
fourth, orphaned price table is gone; the Spec-23 tables (quarterly-reviewed,
MAINTENANCE.md D-23-3) and the OpenRouter catalog are the only numbers homes.

Invariants (spec M2 §2):

* Pricing NEVER blocks or delays a turn — resolution is dict-lookup only
  (``allow_fetch=False``: a cold OpenRouter catalog index is a miss, never a
  network fetch; the api lifespan warms the index off-loop).
* Absent data degrades honestly — unknown model → ``(0.0, "unpriced")`` with a
  once-per-pair warning, never a guess.

``cost_basis`` vocabulary (``TurnLog.cost_basis``, D-M2-1):

* ``"actual_openrouter"`` — the response's own ``usage.cost`` (what we paid).
* ``"estimate_static"`` — static per-provider table hit (vendor-published).
* ``"estimate_catalog"`` — OpenRouter catalog hit (derived, best-effort).
* ``"unpriced"`` — no data; ``cost_cents`` recorded as ``0.0``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable

from persona.logging import get_logger

if TYPE_CHECKING:
    from persona.backends.model_metadata import ModelMetadata

__all__ = ["CostBasis", "CostSource", "compute_turn_cost"]

_logger = get_logger("runtime.cost")

CostBasis = Literal["actual_openrouter", "estimate_static", "estimate_catalog", "unpriced"]

#: Cents rounding applied to BOTH pricing arms: 6 decimals (micro-cent) so tiny
#: turns don't collapse to 0.0 and float noise never leaks into the persisted
#: record. Originally the response-side-actual's USD->cents rounding; spec M2
#: review finding I1 extends the SAME treatment to the estimate arm's summed
#: cents (static/catalog table lookups also pick up float noise, and the
#: worker's printed-value ceil turns that noise into a real overcharge).
_ACTUAL_ROUND_DECIMALS: Final[int] = 6


@runtime_checkable
class CostSource(Protocol):
    """Provenance-carrying metadata lookup for turn pricing (D-M2-1).

    :class:`~persona.backends.metadata.ChainedModelMetadataResolver` implements
    this structurally. The cost path always passes ``allow_fetch=False`` —
    implementations MUST NOT perform network I/O under that flag (the module
    invariant: pricing never blocks a turn).
    """

    def resolve_with_source(
        self, model_id: str, *, allow_fetch: bool = True
    ) -> tuple[ModelMetadata, Literal["static", "catalog"]] | None:
        """Return ``(metadata, which-link-answered)``, or ``None`` on a miss."""
        ...


@lru_cache(maxsize=1)
def _default_source() -> CostSource:
    """The zero-network fallback source: static tables only.

    Bare loops (CLI / unit tests) that were never handed a composition-root
    resolver still estimate curated models — no key, no catalog, no network.
    The api composition root (RuntimeFactory) injects the full shared chain
    instead, so hosted turns additionally get OpenRouter-catalog coverage.
    """
    from persona.backends.metadata import (  # noqa: PLC0415 — lazy: keep import-time light
        ChainedModelMetadataResolver,
        StaticModelMetadataResolver,
    )

    return ChainedModelMetadataResolver(static=StaticModelMetadataResolver(), openrouter=None)


_warned_unpriced: set[tuple[str, str]] = set()


def _canonical_model_id(provider: str, model: str) -> str:
    """The resolver-chain lookup id for a served ``(provider, model)`` pair.

    The static tables key on the provider-prefixed id
    (``"anthropic/claude-sonnet-4-6"``, Spec 23) while backends report
    ``provider`` and ``model`` separately — and in provider-specific shapes:

    * **openrouter** — ``model`` IS the catalog slug (``"z-ai/glm-4.6"``);
      prefixing it with ``openrouter/`` would miss both chain links. As-is.
    * **already-prefixed** — NVIDIA catalog ids arrive prefixed
      (``"nvidia/llama-3.3-…"``, the Spec-25 §2.6 silent-miss lesson). As-is.
    * **bare** — everything else (``"claude-sonnet-4-6"``) gains its provider
      prefix to form the canonical id.
    """
    if provider == "openrouter":
        return model
    prefix = f"{provider}/"
    return model if model.startswith(prefix) else f"{prefix}{model}"


def compute_turn_cost(
    *,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    actual_cost_usd: float | None = None,
    source: CostSource | None = None,
) -> tuple[float, CostBasis]:
    """Price one turn: ``(cost_cents, cost_basis)`` (D-M2-1 / D-M2-3).

    A response-side OpenRouter actual (``actual_cost_usd``, captured per
    D-M2-3) is authoritative when present — it is what we actually paid for
    the routed request. Otherwise the resolver chain estimates from the served
    model's metadata WITHOUT fetching (module invariant); a full miss degrades
    honestly to ``(0.0, "unpriced")`` with one warning per (provider, model).

    Args:
        provider: The served backend's provider name.
        model: The served backend's model name (provider-specific shape —
            see :func:`_canonical_model_id`).
        prompt_tokens: Prompt tokens for the estimate arms.
        completion_tokens: Completion tokens for the estimate arms.
        actual_cost_usd: The response's own cost in USD (OpenRouter credits ≈
            USD), or ``None`` when the provider reported none. Negative values
            are treated as absent (defensive; the parse layer already drops
            them).
        source: The provenance-carrying resolver chain, or ``None`` for the
            zero-network static-only default (bare / CLI / test loops).

    Returns:
        ``(cost_cents, cost_basis)`` — cents as float (estimates keep the raw
        arithmetic; actuals round to micro-cents), basis per the module
        vocabulary.
    """
    # M1 (spec M2 review): gate the actual arm on the OpenRouter provider — the
    # ``cost_usd`` field is a response-side actual ONLY when it came off an
    # OpenRouter route (D-M2-3, the usage-accounting opt-in). A future non-OR
    # backend that starts populating ``cost_usd`` (a different currency of
    # "cost", e.g. a provider-reported estimate) must never be mistaken for
    # "what we actually paid" and minted as ``actual_openrouter`` — it falls
    # through to the honest resolver-chain estimate below instead.
    if provider == "openrouter" and actual_cost_usd is not None and actual_cost_usd >= 0.0:
        return round(actual_cost_usd * 100.0, _ACTUAL_ROUND_DECIMALS), "actual_openrouter"
    chain = source if source is not None else _default_source()
    hit = chain.resolve_with_source(_canonical_model_id(provider, model), allow_fetch=False)
    if hit is None:
        key = (provider, model)
        if key not in _warned_unpriced:
            _warned_unpriced.add(key)
            _logger.warning(
                "no pricing metadata; turn recorded unpriced provider={provider} model={model}",
                provider=provider,
                model=model,
            )
        return 0.0, "unpriced"
    metadata, link = hit
    # I1 (spec M2 review): round the estimate sum the SAME way the actual arm
    # already rounds (``_ACTUAL_ROUND_DECIMALS``). Raw float arithmetic on an
    # exact-integer-cent estimate can land a 1e-15 noise bit above the true
    # value (e.g. 9.0 -> 9.000000000000002); the worker's printed-value ceil
    # (``Decimal(str(cost))``) then rounds that noise UP to an extra whole
    # credit. Rounding here — once, at the one place cents are computed —
    # removes the noise before it ever reaches the ceil.
    cents = round(
        (prompt_tokens / 1000.0) * metadata.cost_input_per_1k_tokens
        + (completion_tokens / 1000.0) * metadata.cost_output_per_1k_tokens,
        _ACTUAL_ROUND_DECIMALS,
    )
    return cents, "estimate_static" if link == "static" else "estimate_catalog"
