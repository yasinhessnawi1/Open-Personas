"""Charging the owner for an image a persona makes in chat (R9-308).

The chat ``generate_image`` tool used to call the backend directly, with no pre-check, no
charge and no spend kind, so every image a persona generated in a turn cost the house the
provider price and the person nothing beyond the turn's model tokens. This module is the
api half of the tool's charge seam (:mod:`persona.imagegen.charge`); the composition root
injects it so core never imports the ledger.

**One idempotent ledger entry per tool call**, the sandbox's shape rather than a share of
the turn's charge. The turn's charge is all-or-nothing on clean completion, carries no
billing key and is clamped to a per-turn ceiling, so an image folded into it would be free
whenever the turn was cancelled or failed after the image was made, and could be clamped
away. The per-execution ``code_execution`` charge already solved that problem by billing at
the tool, owner from the request context, and this follows it; the avatar charge
(``avatar_billing``) supplies the per-image pricing and the billing key.

* **Before the provider:** the owner's balance must cover the registry estimate for every
  image asked for (floored like any image). Short, and the tool returns a clear error and
  the provider is never called. No pre-deduct is taken, so a provider failure leaves
  nothing to refund and charges nothing.
* **After a success:** the images RETURNED are priced through ``generation_cost_cents``,
  the one place an image call is priced (R9-198), and captured with
  ``capture_up_to_idempotent`` under a key minted for this call. The work is done, so the
  capture takes what the balance covers rather than failing, as a completed task leg does.
  A write that fails is retried once with the same key, which the ledger's
  ``ON CONFLICT (billing_key) DO NOTHING`` makes safe even when the first write landed.

No request owner bound (the CLI, a task leg, whose worker binds none) bills nothing, as the
sandbox does. Community's unlimited policy passes the check and records nothing.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Final

from persona.billing import BillingConfig, credits_charged
from persona.billing.image_pricing import image_cents
from persona.errors import CreditsExhaustedError, DailySpendCapExceededError
from persona.imagegen.charge import ImageChargeRefusedError
from persona.logging import get_logger

from persona_api.sandbox.context import get_sandbox_request_context
from persona_api.services.image_cost import generation_cost_cents

if TYPE_CHECKING:
    from persona.imagegen import GenerationResult, ImageGenOptions
    from persona_runtime.cost import CostSource
    from sqlalchemy import Engine

    from persona_api.editions.credits_policy import CreditsPolicy

__all__ = ["CHAT_IMAGE_REASON", "ChatImageCharge", "ChatImageChargeHook"]

_LOG = get_logger("api.imagegen.chat_charge")

#: The ledger reason prefix of a chat image charge; the basis is appended (``chat_image:<basis>``).
CHAT_IMAGE_REASON: Final[str] = "chat_image"

#: How many times a settle write is attempted. Two: one retry, safe because it is keyed.
_SETTLE_ATTEMPTS: Final[int] = 2


class ChatImageChargeHook:
    """The :class:`~persona.imagegen.charge.ImageChargeHook` the api injects into the chat tool."""

    def __init__(
        self,
        *,
        credits_policy: CreditsPolicy | None,
        rls_engine: Engine,
        cost_source: CostSource | None,
        billing_config: BillingConfig | None = None,
        floor: int = 1,
    ) -> None:
        self._policy = credits_policy
        self._engine = rls_engine
        self._cost_source = cost_source
        self._markup = (billing_config or BillingConfig()).credit_markup
        self._floor = floor

    async def authorize(
        self, options: ImageGenOptions, *, provider: str, model: str
    ) -> ChatImageCharge | None:
        """Refuse an image the owner cannot afford; else hand back its pending charge."""
        ctx = get_sandbox_request_context()
        if self._policy is None or ctx is None:
            return None
        estimate = self._estimate(options.count, provider=provider, model=model)
        try:
            balance = await asyncio.to_thread(
                self._policy.require_credits, rls_engine=self._engine, user_id=ctx.owner_id
            )
        except CreditsExhaustedError as exc:
            raise _refusal(estimate=estimate, balance=0) from exc
        if balance < estimate:
            raise _refusal(estimate=estimate, balance=balance)
        return ChatImageCharge(
            credits_policy=self._policy,
            rls_engine=self._engine,
            cost_source=self._cost_source,
            markup=self._markup,
            floor=self._floor,
            owner_id=ctx.owner_id,
            billing_key=f"{CHAT_IMAGE_REASON}:{uuid.uuid4().hex}",
        )

    def _estimate(self, count: int, *, provider: str, model: str) -> int:
        """What ``count`` images should cost, in credits, before any exist.

        The registry's per-image price times the count asked for, floored. A backend with
        no row (OpenRouter reports its actual only afterwards) is checked at the floor,
        which is all that can be said before the call without guessing a price.
        """
        per_image_cents, _ = image_cents(provider, model=model)
        return credits_charged(
            provider_cents=per_image_cents * count,
            infra_flat_cents=0.0,
            markup=self._markup,
            floor=self._floor,
        )


class ChatImageCharge:
    """One authorized chat image call's pending charge, settled once under its own key."""

    def __init__(
        self,
        *,
        credits_policy: CreditsPolicy,
        rls_engine: Engine,
        cost_source: CostSource | None,
        markup: float,
        floor: int,
        owner_id: str,
        billing_key: str,
    ) -> None:
        self._policy = credits_policy
        self._engine = rls_engine
        self._cost_source = cost_source
        self._markup = markup
        self._floor = floor
        self.owner_id = owner_id
        self.billing_key = billing_key

    async def settle(self, result: GenerationResult) -> None:
        """Charge the owner for the images in ``result``; a repeat charges nothing."""
        cost_cents, basis = generation_cost_cents(result, cost_source=self._cost_source)
        if basis == "unpriced":
            _LOG.warning(
                "chat image billed at the floor: no provider cost and no pricing row "
                "provider={provider} model={model}",
                provider=result.provider,
                model=result.model,
            )
        amount = credits_charged(
            provider_cents=cost_cents,
            infra_flat_cents=0.0,  # infra via the credit floor (D-M3-4 amendment)
            markup=self._markup,
            floor=self._floor,
        )
        for attempt in range(1, _SETTLE_ATTEMPTS + 1):
            try:
                await asyncio.to_thread(
                    self._policy.capture_up_to_idempotent,
                    rls_engine=self._engine,
                    user_id=self.owner_id,
                    amount=amount,
                    reason=f"{CHAT_IMAGE_REASON}:{basis}",
                    billing_key=self.billing_key,
                    cost_cents=cost_cents,
                    cost_basis=basis,
                )
            except DailySpendCapExceededError:
                # Already audited by the policy; retrying cannot help today.
                _LOG.warning(
                    "chat image not billed: daily spend cap reached owner={owner} key={key}",
                    owner=self.owner_id,
                    key=self.billing_key,
                )
                return
            except Exception:
                if attempt == _SETTLE_ATTEMPTS:
                    raise
                _LOG.warning(
                    "chat image charge write failed; retrying under the same key key={key}",
                    key=self.billing_key,
                )
            else:
                return


def _refusal(*, estimate: int, balance: int) -> ImageChargeRefusedError:
    """The refusal the model relays: what the image costs and what is left."""
    msg = (
        f"Not enough credits to generate this image. It costs about {estimate} "
        f"credit{'s' if estimate != 1 else ''} and the balance is {balance}. "
        "Add credits to make images."
    )
    return ImageChargeRefusedError(
        msg,
        context={
            "reason": "insufficient_credits",
            "estimate": str(estimate),
            "balance": str(balance),
        },
    )
