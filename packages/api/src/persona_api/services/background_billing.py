"""Shared owner-billing for background LLM surfaces (Spec M3, T5 — D-M3-8 / D-M3-R5).

Episodic consolidation, persona-voice auto-pick, and the initiative scan each make
a real model call the owner benefits from but no end-user is in the loop for (graph
consolidation is a deterministic embedding merge — no model call, so it charges
nothing and is not wired). Each bills the **persona owner** its real cost POST-HOC
through this one
helper: price via the seam (``compute_turn_cost`` — OpenRouter ``usage.cost`` actual
preferred; else the resolver estimate; else the floor), charge idempotently
(``capture_up_to_idempotent`` — floored, keyed on the surface's natural
``billing_key`` so a retry / re-fire does not double-charge), record
``cost_cents`` / ``cost_basis``, reason ``<surface>:<basis>``, infra via the floor.

**Fail-soft everywhere** — a billing hiccup must NEVER break the background op (the
op is the deliverable; the charge is enrichment).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.billing import BillingConfig, credits_charged
from persona.logging import get_logger
from persona_runtime.cost import compute_turn_cost

if TYPE_CHECKING:
    from persona_runtime.cost import CostSource
    from sqlalchemy import Engine

    from persona_api.editions.credits_policy import CreditsPolicy

__all__ = ["bill_background_llm"]

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
