"""Owner-billing for a generated avatar, shared by the request path and the durable queue.

Spec M3 (T3b, D-M3-11) flipped avatar generation from free to owner-billed at its
real image cost, but only the in-request hook (``routes/personas.py``) learned
that; the queue's generator kept returning ``cost_micros=0`` and never touched
the ledger. With the queue as the default path that would have made every
avatar free by accident. This module is the ONE place the charge is computed and
recorded, so the two paths cannot drift again.

Fail-soft by contract: a billing hiccup is logged and never raised, because a
persona must never fail to exist (or a job fail to settle) over a ledger error.
Idempotent by key: an at-least-once redelivery re-uses the same ``billing_key``
and does not double-charge (D-M3-R5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.billing import BillingConfig, credits_charged
from persona.logging import get_logger
from persona_runtime.cost import compute_turn_cost
from pydantic import BaseModel, ConfigDict

if TYPE_CHECKING:
    from persona.imagegen import GenerationResult
    from persona_runtime.cost import CostSource
    from sqlalchemy import Engine

    from persona_api.editions.credits_policy import CreditsPolicy

__all__ = ["AvatarCharge", "bill_avatar_owner"]

_LOG = get_logger("api.avatar_billing")

#: Ledger micros per cent (``persona.tasks.MICROS_PER_DOLLAR`` is 10 000 per dollar).
_MICROS_PER_CENT = 100


class AvatarCharge(BaseModel):
    """What one avatar generation cost and what the owner was charged for it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    credits: int
    cost_cents: float
    cost_basis: str

    @property
    def cost_micros(self) -> int:
        """The provider cost in ledger micros, the unit the job meter records."""
        return round(self.cost_cents * _MICROS_PER_CENT)


def bill_avatar_owner(
    *,
    credits_policy: CreditsPolicy | None,
    rls_engine: Engine,
    cost_source: CostSource | None,
    image_credit_floor: int,
    owner_id: str,
    persona_id: str,
    result: GenerationResult,
    billing_key: str,
) -> AvatarCharge | None:
    """Charge the persona owner for a successfully generated avatar (Spec M3, D-M3-11).

    Prices the generation like any image (``compute_turn_cost`` over the served
    model's usage; an OpenRouter ``usage.cost`` actual is preferred), runs it
    through the credit formula (floored; infra rides the floor, D-M3-4 amendment)
    and records the deduct with ``cost_cents`` / ``cost_basis`` under
    ``billing_key``. Returns the charge, or ``None`` when nothing was recorded:
    no credits policy composed (a worker without a billing seam), or a ledger
    failure, which is logged and swallowed.

    Args:
        credits_policy: The edition's credits policy, or ``None`` when the caller
            has no billing seam composed.
        rls_engine: The owner-scoped engine the ledger write runs on.
        cost_source: Pricing metadata for the estimate fallback; ``None`` means
            only a response-side actual can price the call.
        image_credit_floor: The minimum credits any image costs.
        owner_id: The persona owner, who pays.
        persona_id: The persona the avatar belongs to (log context).
        result: The provider's generation result (usage + actual cost).
        billing_key: The idempotency key of this charge; a redelivery with the
            same key does not charge twice.
    """
    if credits_policy is None:
        return None
    try:
        cost_cents, basis = compute_turn_cost(
            provider=result.provider,
            model=result.model,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            actual_cost_usd=result.cost_usd,
            source=cost_source,
        )
        charge = credits_charged(
            provider_cents=cost_cents,
            infra_flat_cents=0.0,
            markup=BillingConfig().credit_markup,
            floor=image_credit_floor,
        )
        credits_policy.deduct_idempotent(
            rls_engine=rls_engine,
            user_id=owner_id,
            amount=charge,
            reason=f"avatar_gen:{basis}",
            billing_key=billing_key,
            cost_cents=cost_cents,
            cost_basis=basis,
        )
    except Exception as exc:  # noqa: BLE001 — billing must never break the avatar path
        _LOG.warning(
            "avatar owner-billing failed (fail-soft)", persona_id=persona_id, error=str(exc)
        )
        return None
    return AvatarCharge(credits=charge, cost_cents=cost_cents, cost_basis=str(basis))
