"""Shared owner-billing for background LLM surfaces (Spec M3, T5 — D-M3-8 / D-M3-R5).

Episodic consolidation, persona-voice auto-pick, the initiative scan, the
conversation retitle, K2 synthesis and the file extraction each make a real model
call the owner benefits from but no end-user is in the loop for (graph
consolidation is a deterministic embedding merge, no model call, so it charges
nothing and is not wired). Three of those were added on 2026-09-19, each having
been unbilled twice over: the handler never called this helper AND the worker root
built its backend unmetered, so wiring only one half would have billed the floor.
Two surfaces here are not model calls at all: a file extraction's sandbox render
(per-execution infra) and an outbound SMS segment (a per-unit provider meter), and
they have their own siblings below for exactly that reason.

Each bills the **persona owner** its real cost POST-HOC through this one helper:
price via the seam (``compute_turn_cost`` — OpenRouter ``usage.cost`` actual
preferred; else the resolver estimate; else the floor), charge idempotently
(``capture_up_to_idempotent`` — floored, keyed on the surface's natural
``billing_key`` so a retry / re-fire does not double-charge), record
``cost_cents`` / ``cost_basis``, reason ``<surface>:<basis>``, infra via the floor.

**Fail-soft everywhere** — a billing hiccup must NEVER break the background op (the
op is the deliverable; the charge is enrichment).

R9-179 item 3: the served ``(provider, model)`` pair this helper already receives is
also the api's attribution point for background surfaces, so the free-model daily
request meter reads it here, background jobs on a free chain spend the same
account-wide cap a free user's turn does.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.billing import BillingConfig, credits_charged, infra_rate_cents
from persona.logging import get_logger
from persona_runtime.cost import compute_turn_cost

from persona_api.services.free_model_usage import FreeModelDailyCounter

if TYPE_CHECKING:
    from persona.billing import CostBasis, InfraUnit
    from persona_runtime.cost import CostSource
    from sqlalchemy import Engine

    from persona_api.editions.credits_policy import CreditsPolicy

__all__ = ["bill_background_infra", "bill_background_llm", "bill_background_provider"]

_LOG = get_logger("api.background_billing")


def bill_background_llm(
    *,
    credits_policy: CreditsPolicy | None,
    rls_engine: Engine | None,
    owner_id: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost_usd: float | None,
    surface: str,
    billing_key: str,
    cost_source: CostSource | None = None,
    billing_config: BillingConfig | None = None,
    floor: int = 1,
) -> None:
    """Owner-bill one background LLM op's real cost, idempotent + fail-soft (Spec M3, T5).

    Args:
        credits_policy / rls_engine: The billing seam + owner-scoped engine. Either
            ``None`` → no billing (the plain / community-unmetered shape).
        owner_id: The persona owner (the payer, D-M3-8).
        provider / model: The served backend's identity (for pricing).
        prompt_tokens / completion_tokens: The call's token counts (summed across
            the surface's model calls, if it made several).
        cost_usd: The OpenRouter ``usage.cost`` actual, or ``None`` (then priced
            from the token counts via the resolver / static tables).
        surface: The ledger reason prefix (e.g. ``"graph_consolidation"``).
        billing_key: The surface's natural idempotency key — a retry / re-fire with
            the same key does not double-charge (``ON CONFLICT DO NOTHING``).
        cost_source: The pricing resolver chain (full catalog coverage); ``None``
            → the static-only default (OpenRouter actuals still price exactly).
        floor: The minimum charge (infra via the floor, D-M3-4 amendment).
    """
    if credits_policy is None or rls_engine is None:
        return
    # A surface that made no real model call (0 tokens, no cost) charges nothing.
    if prompt_tokens <= 0 and completion_tokens <= 0 and not cost_usd:
        return
    # R9-179 item 3, a background op on a free chain spends the SAME account-wide
    # daily cap a free user's turn does, and this is where the api already knows which
    # model served it. Meter it from that pair rather than from a second attribution.
    # Fail-soft inside the counter; a paid model is not counted.
    FreeModelDailyCounter.from_env(rls_engine).record_served(provider=provider, model=model)
    config = billing_config or BillingConfig()
    try:
        cost_cents, basis = compute_turn_cost(
            provider=provider,
            model=model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            actual_cost_usd=cost_usd,
            source=cost_source,
        )
        charge = credits_charged(
            provider_cents=cost_cents,
            infra_flat_cents=0.0,  # infra via the floor (D-M3-4 amendment)
            markup=config.credit_markup,
            floor=floor,
        )
        credits_policy.capture_up_to_idempotent(
            rls_engine=rls_engine,
            user_id=owner_id,
            amount=charge,
            reason=f"{surface}:{basis}",
            billing_key=billing_key,
            cost_cents=cost_cents,
            cost_basis=basis,
        )
    except Exception as exc:  # noqa: BLE001 — billing must NEVER break the background op
        _LOG.warning(
            "background LLM owner-billing failed (fail-soft) surface={surface} "
            "owner={owner} key={key}: {err}",
            surface=surface,
            owner=owner_id,
            key=billing_key,
            err=str(exc),
        )


def bill_background_infra(
    *,
    credits_policy: CreditsPolicy | None,
    rls_engine: Engine | None,
    owner_id: str,
    infra_unit: InfraUnit,
    surface: str,
    billing_key: str,
    billing_config: BillingConfig | None = None,
    floor: int = 1,
) -> None:
    """Owner-bill one background op's PER-EXECUTION INFRA cost, idempotent + fail-soft.

    The infra-only sibling of :func:`bill_background_llm`, same formula and same
    ``capture_up_to_idempotent`` conflict gate, for work that costs real infra and no
    provider tokens. The rate comes from the one pricing truth
    (``persona.billing.infra_rate_cents`` over the ``PERSONA_INFRA_RATE_*`` config), so a
    background surface cannot come to disagree with the interactive one about a price.

    This is the seam ``fb1ad05c`` settled for a task leg's sandbox execution: the leg
    charges the execution itself, once, keyed to its own checkpoint, with the sandbox
    riding ``infra_flat_cents`` rather than ``provider_cents``. A background job that
    drives the sandbox outside a leg needs the same thing said in the same words, and
    saying it here rather than a third time in a handler is what keeps it one seam.

    Args:
        credits_policy / rls_engine: The billing seam + owner-scoped engine. Either
            ``None`` → no billing (the plain / community-unmetered shape).
        owner_id: The persona owner (the payer, D-M3-8).
        infra_unit: Which ``PERSONA_INFRA_RATE_*`` rate this execution is charged at.
        surface: The ledger reason prefix (e.g. ``"file_extract_sandbox"``).
        billing_key: The op's natural idempotency key. It must be DISTINCT from any
            other charge the same op makes: the conflict gate is unique on
            ``billing_key`` alone, so a shared key means the first claim row silently
            swallows the second charge.
        billing_config: The markup + infra rates; ``None`` reads them from env.
        floor: The minimum charge (D-M3-4 amendment).
    """
    if credits_policy is None or rls_engine is None:
        return
    config = billing_config or BillingConfig()
    infra_cents = infra_rate_cents(config, infra_unit)
    try:
        charge = credits_charged(
            provider_cents=0.0,  # no provider cost on an infra-only surface
            infra_flat_cents=infra_cents,
            markup=config.credit_markup,
            floor=floor,
        )
        credits_policy.capture_up_to_idempotent(
            rls_engine=rls_engine,
            user_id=owner_id,
            amount=charge,
            reason=f"{surface}:infra_flat",
            billing_key=billing_key,
            cost_cents=0.0,
            cost_basis="infra_flat",
        )
    except Exception as exc:  # noqa: BLE001 (billing must NEVER break the background op)
        _LOG.warning(
            "background infra owner-billing failed (fail-soft) surface={surface} "
            "owner={owner} key={key}: {err}",
            surface=surface,
            owner=owner_id,
            key=billing_key,
            err=str(exc),
        )


def bill_background_provider(
    *,
    credits_policy: CreditsPolicy | None,
    rls_engine: Engine | None,
    owner_id: str,
    provider_cents: float,
    cost_basis: CostBasis,
    surface: str,
    billing_key: str,
    billing_config: BillingConfig | None = None,
    floor: int = 1,
) -> None:
    """Owner-bill one background op's PROVIDER cost, idempotent + fail-soft.

    The third sibling. :func:`bill_background_llm` prices a model call from its token
    counts and :func:`bill_background_infra` charges an infra rate with no provider at
    all; this one is for a surface that costs real provider money per unit and has no
    tokens to price it from, because the provider meters something else. An outbound SMS
    is the case that needed it: Twilio bills per SEGMENT, reports the segment count on the
    delivery status callback, and nothing about that is a token.

    The caller brings the price. That is deliberate rather than lazy: a per-unit price
    this helper cannot verify is worse than no charge at all, since undercharging costs
    the house money while a guessed price overcharges a person. A caller with no
    configured price must not call this.

    Args:
        credits_policy / rls_engine: The billing seam + owner-scoped engine. Either
            ``None`` -> no billing (the plain / community-unmetered shape).
        owner_id: The persona owner (the payer, D-M3-8).
        provider_cents: The op's real provider cost in cents (already multiplied out
            over however many units the provider metered).
        cost_basis: The provenance recorded on the ledger row (``"provider_meter"`` for a
            per-unit provider meter, the SMS segment case).
        surface: The ledger reason prefix (e.g. ``"sms_segments"``).
        billing_key: The op's natural idempotency key, which must be DISTINCT from any
            other charge the same op makes: the conflict gate is unique on
            ``billing_key`` alone (R9-196), so a shared key means the first claim row
            silently swallows the second charge.
        billing_config: The markup + infra rates; ``None`` reads them from env.
        floor: The minimum charge (D-M3-4 amendment).
    """
    if credits_policy is None or rls_engine is None:
        return
    config = billing_config or BillingConfig()
    try:
        charge = credits_charged(
            provider_cents=provider_cents,
            infra_flat_cents=0.0,  # infra via the floor (D-M3-4 amendment)
            markup=config.credit_markup,
            floor=floor,
        )
        credits_policy.capture_up_to_idempotent(
            rls_engine=rls_engine,
            user_id=owner_id,
            amount=charge,
            reason=f"{surface}:{cost_basis}",
            billing_key=billing_key,
            cost_cents=provider_cents,
            cost_basis=cost_basis,
        )
    except Exception as exc:  # noqa: BLE001 (billing must NEVER break the background op)
        _LOG.warning(
            "background provider owner-billing failed (fail-soft) surface={surface} "
            "owner={owner} key={key}: {err}",
            surface=surface,
            owner=owner_id,
            key=billing_key,
            err=str(exc),
        )
