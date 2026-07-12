"""Chained model-metadata resolver (Spec 23 T5; D-23-5 / D-23-X-resolver-precedence).

Composes the static per-provider tables and the OpenRouter catalog resolver into
one :class:`~persona.backends.model_metadata.ModelMetadataResolver`.

**Precedence — static-authoritative-when-present, OpenRouter-for-coverage**
(D-23-X-resolver-precedence, a Phase-4 refinement of D-23-5's literal
"OpenRouter → static" order). The refinement is forced by two facts discovered in
implementation:

* The OpenRouter catalog structurally lacks ``quality_benchmark`` and
  ``latency_p50_ms`` (it publishes neither), and its pricing is *derived* /
  best-effort (R-23-5).
* The static tables are *provider-authoritative* (cost from vendor pages, authored
  quality / latency, per-row ``cost_verified_at_deploy``) for the curated set
  (R-23-2).

So for a curated model the static record is strictly better on every axis;
consulting OpenRouter first would hand curated models a neutral (0.5) quality and
a placeholder latency. D-23-5's intent — "OpenRouter for breadth, static for
authority" — is preserved: static is the *accuracy primary* for curated models,
OpenRouter is the *coverage fallback* for the 300+ uncurated long-tail models
(supplying derived cost + context + capability, with neutral quality / latency
that the live :class:`FirstTokenLatencyTracker` later supersedes).

A miss from BOTH returns ``None`` → the IntelligentRouter degrades to rule-based
selection for that turn (criterion 9), never crashing.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from persona.backends.model_metadata import ModelMetadata, ModelMetadataResolver

__all__ = ["ChainedModelMetadataResolver", "MetadataSource"]

#: Which chain link answered a :meth:`ChainedModelMetadataResolver.resolve_with_source`
#: lookup — the provenance the Spec M2 cost estimator maps to ``cost_basis``
#: (``"static"`` → ``estimate_static``, ``"catalog"`` → ``estimate_catalog``).
MetadataSource = Literal["static", "catalog"]


class ChainedModelMetadataResolver:
    """Resolve metadata static-first, OpenRouter-for-coverage (D-23-X-resolver-precedence).

    Implements the
    :class:`~persona.backends.model_metadata.ModelMetadataResolver` Protocol
    structurally. Either resolver may be ``None`` (e.g. no OpenRouter key
    configured → static-only; or a static-free deployment → catalog-only).

    Args:
        static: The authoritative static-table resolver (consulted first for
            curated models). ``None`` to disable.
        openrouter: The broad-coverage catalog resolver (consulted when the
            static table misses). ``None`` to disable.
    """

    def __init__(
        self,
        *,
        static: ModelMetadataResolver | None,
        openrouter: ModelMetadataResolver | None,
    ) -> None:
        self._static = static
        self._openrouter = openrouter

    def resolve(self, model_id: str) -> ModelMetadata | None:
        """Return metadata for ``model_id`` (static first, then OpenRouter), or ``None``."""
        if self._static is not None:
            static_hit = self._static.resolve(model_id)
            if static_hit is not None:
                return static_hit
        if self._openrouter is not None:
            return self._openrouter.resolve(model_id)
        return None

    def resolve_with_source(
        self, model_id: str, *, allow_fetch: bool = True
    ) -> tuple[ModelMetadata, MetadataSource] | None:
        """Resolve like :meth:`resolve` but also report WHICH link answered (Spec M2, D-M2-1).

        The M2 cost estimator needs two things :meth:`resolve` cannot give it:

        * **Provenance** — the TurnLog's ``cost_basis`` distinguishes an
          authored static-table estimate (``"static"``) from a derived
          OpenRouter-catalog one (``"catalog"``).
        * **A no-network guarantee on the turn path** — with
          ``allow_fetch=False`` the OpenRouter link is consulted through
          ``resolve_no_fetch`` (a cold catalog index is a miss, never a
          fetch). A composed resolver that does not expose ``resolve_no_fetch``
          cannot promise fetch-free resolution and is SKIPPED under
          ``allow_fetch=False`` — pricing must never block a turn (M2 §2), so
          the honest degrade (static / unpriced) beats a maybe-fetch.

        The IntelligentRouter keeps calling :meth:`resolve` unchanged.
        """
        if self._static is not None:
            static_hit = self._static.resolve(model_id)
            if static_hit is not None:
                return static_hit, "static"
        if self._openrouter is not None:
            if allow_fetch:
                catalog_hit = self._openrouter.resolve(model_id)
            else:
                no_fetch = getattr(self._openrouter, "resolve_no_fetch", None)
                catalog_hit = no_fetch(model_id) if callable(no_fetch) else None
            if catalog_hit is not None:
                return catalog_hit, "catalog"
        return None
