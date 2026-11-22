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

from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Final, Literal, Protocol, runtime_checkable

from persona.billing.basis import CostBasis
from persona.logging import get_logger

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from persona.backends.model_metadata import ModelMetadata
    from persona.backends.types import BackendAttempt

# Spec M3 (D-M3-12): ``CostBasis`` now lives in ``persona.billing.basis`` (core)
# so the billing seam has one authoritative home for the vocabulary; re-exported
# here so ``from persona_runtime.cost import CostBasis`` importers are unchanged.
__all__ = [
    "CENTS_ROUND_DECIMALS",
    "CostBasis",
    "CostSource",
    "PricedRound",
    "compute_turn_cost",
    "least_certain_basis",
    "price_rounds",
    "served_model_metadata",
]

_logger = get_logger("runtime.cost")

#: Cents rounding applied to BOTH pricing arms: 6 decimals (micro-cent) so tiny
#: turns don't collapse to 0.0 and float noise never leaks into the persisted
#: record. Originally the response-side-actual's USD->cents rounding; spec M2
#: review finding I1 extends the SAME treatment to the estimate arm's summed
#: cents (static/catalog table lookups also pick up float noise, and the
#: worker's printed-value ceil turns that noise into a real overcharge).
CENTS_ROUND_DECIMALS: Final[int] = 6


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


def served_model_metadata(
    provider: str, model: str, *, source: CostSource | None = None
) -> ModelMetadata | None:
    """The metadata of a served ``(provider, model)``, looked up as the pricer does it.

    Same canonical id, same chain, same no-fetch guarantee as :func:`compute_turn_cost`,
    so every reader of a model's metadata (the agentic compaction window, R9-314) agrees
    with the price it is charged at. ``None`` is an honest miss; the caller decides what
    a miss means for it.

    Args:
        provider: The backend's provider name.
        model: The backend's model name, in the provider's own shape.
        source: The resolver chain, or ``None`` for the zero-network static default.
    """
    chain = source if source is not None else _default_source()
    hit = chain.resolve_with_source(_canonical_model_id(provider, model), allow_fetch=False)
    return None if hit is None else hit[0]


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
        return round(actual_cost_usd * 100.0, CENTS_ROUND_DECIMALS), "actual_openrouter"
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
    # already rounds (``CENTS_ROUND_DECIMALS``). Raw float arithmetic on an
    # exact-integer-cent estimate can land a 1e-15 noise bit above the true
    # value (e.g. 9.0 -> 9.000000000000002); the worker's printed-value ceil
    # (``Decimal(str(cost))``) then rounds that noise UP to an extra whole
    # credit. Rounding here — once, at the one place cents are computed —
    # removes the noise before it ever reaches the ceil.
    cents = round(
        (prompt_tokens / 1000.0) * metadata.cost_input_per_1k_tokens
        + (completion_tokens / 1000.0) * metadata.cost_output_per_1k_tokens,
        CENTS_ROUND_DECIMALS,
    )
    return cents, "estimate_static" if link == "static" else "estimate_catalog"


@dataclass(frozen=True)
class PricedRound:
    """One model call of a turn, with the identity that served it (R9-246).

    Attributes:
        prompt_tokens: The call's prompt tokens.
        completion_tokens: The call's completion tokens.
        cost_usd: The call's OpenRouter actual, or ``None``.
        provider: The served provider (the chain's ``ServedCall``, or a bare backend's
            own name). An empty ``model`` means no model served the round.
        model: The served model.
        failed_tries: Failed tries of this call that reported usage (an empty or
            degenerate completion, which the provider still bills). CHARGED, each at
            the model that made it (D-ORCH-11-18-s); one that reported no tokens costs
            nothing.
    """

    prompt_tokens: int
    completion_tokens: int
    cost_usd: float | None
    provider: str
    model: str
    failed_tries: tuple[BackendAttempt, ...] = ()


#: How certain each priced basis is, for labelling a turn whose rounds differ (D3):
#: the turn takes the LEAST certain one, so a partial actual never labels the whole
#: turn as an actual (the M2 review I2 honesty rule).
_BASIS_CERTAINTY: Final[dict[str, int]] = {
    "estimate_catalog": 0,
    "estimate_static": 1,
    "actual_openrouter": 2,
}


def least_certain_basis(bases: Iterable[CostBasis]) -> CostBasis:
    """The label for a sum of priced parts whose bases differ (D3, D-ORCH-11-18-h).

    The least certain basis among the PRICED parts (catalog estimate, then static
    estimate, then actual), so a partial actual never labels the whole as an actual
    (the M2 review I2 honesty rule). ``unpriced`` parts are ignored; the result is
    ``unpriced`` only when no part was priced. One rule for a chat or voice turn
    (:func:`price_rounds`), a task leg's steps and an authoring turn's attempts.
    """
    priced = [basis for basis in bases if basis != "unpriced"]
    if not priced:
        return "unpriced"
    return min(priced, key=lambda b: _BASIS_CERTAINTY.get(b, 0))


def price_rounds(
    rounds: Sequence[PricedRound],
    *,
    source: CostSource | None = None,
    surface: str = "turn",
) -> tuple[float, CostBasis]:
    """Price a turn round by round, each at the model that served it (R9-246).

    The one per-round pricing function for chat and voice. Each round goes through
    :func:`compute_turn_cost` at its OWN served pair, with its own OpenRouter actual
    (gated on that round's provider), and the rounds are summed and rounded ONCE to
    :data:`CENTS_ROUND_DECIMALS`. The caller charges the sum once, per turn, so the
    credit ceil is never taken per round.

    * A round with no served model (``model == ""``) is skipped.
    * D4: an unpriced round inside a turn with a priced round adds 0 cents and logs
      a warning naming its provider and model. A turn with no priced round is
      ``(0.0, "unpriced")``: the existing unpriced path (warn-once, the floor).
    * D3: the turn's basis is the least certain basis among its priced parts, the
      failed tries included.
    * D-ORCH-11-18-s (reverses D1 of D-ORCH-11-18-h): each of a round's
      ``failed_tries`` that reported tokens is priced at the model that made it and
      added to the total, inside the same single rounding (and the caller's single
      ceil), with an INFO line saying what it charged. One that reported no tokens
      costs nothing. An unpriced one adds 0 cents and is named in a warning (D4).

    Args:
        rounds: The turn's rounds in order.
        source: The pricing resolver chain (``None``: the static-only default).
        surface: A label for the log lines (``"chat"``, ``"voice"``).

    Returns:
        ``(cost_cents, cost_basis)`` for the whole turn.
    """
    cents = 0.0
    bases: list[CostBasis] = []
    unpriced: list[PricedRound] = []
    unpriced_tries: list[BackendAttempt] = []
    for priced_round in rounds:
        for attempt in priced_round.failed_tries:
            priced_try = _price_failed_try(attempt, source, surface)
            if priced_try is None:
                continue  # it reported no tokens: it costs nothing
            try_cents, try_basis = priced_try
            if try_basis == "unpriced":
                unpriced_tries.append(attempt)
                continue
            cents += try_cents
            bases.append(try_basis)
        if not priced_round.model:
            continue
        round_cents, basis = compute_turn_cost(
            provider=priced_round.provider,
            model=priced_round.model,
            prompt_tokens=priced_round.prompt_tokens,
            completion_tokens=priced_round.completion_tokens,
            actual_cost_usd=priced_round.cost_usd,
            source=source,
        )
        if basis == "unpriced":
            unpriced.append(priced_round)
            continue
        cents += round_cents
        bases.append(basis)
    if not bases:
        return 0.0, "unpriced"
    for priced_round in unpriced:
        _logger.warning(
            "unpriced round in a priced turn; it adds 0 cents "
            "surface={surface} provider={provider} model={model} "
            "prompt_tokens={prompt} completion_tokens={completion}",
            surface=surface,
            provider=priced_round.provider,
            model=priced_round.model,
            prompt=priced_round.prompt_tokens,
            completion=priced_round.completion_tokens,
        )
    for attempt in unpriced_tries:
        _logger.warning(
            "unpriced failed model try in a priced turn; it adds 0 cents "
            "surface={surface} provider={provider} model={model} error={error}",
            surface=surface,
            provider=attempt.provider,
            model=attempt.model,
            error=attempt.error_class,
        )
    return round(cents, CENTS_ROUND_DECIMALS), least_certain_basis(bases)


def _price_failed_try(
    attempt: BackendAttempt, source: CostSource | None, surface: str
) -> tuple[float, CostBasis] | None:
    """Price one failed try at the model that made it (D-ORCH-11-18-s).

    ``None`` when it reported no tokens (no usage, or zero tokens and no actual): it
    costs nothing and labels nothing. A priced try is logged at INFO with what it adds.
    """
    usage = attempt.usage
    if usage is None or not attempt.reported_tokens:
        return None
    cents, basis = compute_turn_cost(
        provider=attempt.provider,
        model=attempt.model,
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        actual_cost_usd=usage.cost_usd,
        source=source,
    )
    if basis != "unpriced":
        _logger.info(
            "failed model try charged "
            "surface={surface} provider={provider} model={model} error={error} "
            "prompt_tokens={prompt} completion_tokens={completion} "
            "cost_cents={cents} basis={basis}",
            surface=surface,
            provider=attempt.provider,
            model=attempt.model,
            error=attempt.error_class,
            prompt=usage.prompt_tokens,
            completion=usage.completion_tokens,
            cents=cents,
            basis=basis,
        )
    return cents, basis
