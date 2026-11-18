"""Charging the owner for an image a persona makes in chat (R9-308).

The chat ``generate_image`` tool used to call the backend directly, with no pre-check, no
charge and no spend kind, so every image a persona generated in a turn cost the house the
provider price and the person nothing beyond the turn's model tokens. This module is the
api half of the tool's charge seam (:mod:`persona.imagegen.charge`); the composition root
injects it so core never imports the ledger.

**Its own keyed ledger entries per tool call**, not a share of the turn's charge. The
turn's charge fires only on clean completion, has no billing key and is clamped to a
per-turn ceiling, so an image folded into it would be free whenever the turn was cancelled
or failed after the image was made, and could be clamped away. The per-execution
``code_execution`` charge already bills at the tool with the owner taken from the request
context, and this follows it; the avatar charge (``avatar_billing``) supplies the
per-image pricing and the billing key.

The money moves the way the HTTP route's do (``imagegen/service.py``), with every write
keyed so a retry or a redelivery cannot move it twice:

* **Hold, before the provider.** ``deduct_idempotent`` takes the estimate for every image
  asked for (the registry price, floored). That one call checks the balance and books the
  daily spend cap atomically, so an image the owner cannot afford, or one past
  ``credits_max_per_day``, is refused and the provider is never called. Because the hold
  is taken rather than merely checked, two concurrent turns cannot both spend one balance.
* **True-up, after a success.** The images RETURNED are priced through
  ``generation_cost_cents``, the one place an image call is priced (R9-198). Anything above
  the hold is captured (``capture_up_to_idempotent``, floored at the balance, since the
  image exists); anything below it is given back (``grant_idempotent``, which credits the
  allowance bucket exactly as ``refund`` does, but keyed). The true-up row carries the
  call's ``cost_cents`` and basis even when the adjustment is zero.
* **Release, after a failure.** The whole hold is given back under its own key.

Each write is retried once with the same key. If it still fails, a durable
``image_chat_unbilled`` (or ``image_chat_unrefunded``) audit row records the key and the
amounts, never the prompt, and the tool is told through :class:`ImageChargeLedgerError` so
it keeps the image.

**What bills nothing.** No credits policy; community's unlimited policy (every move is a
no-op); no request owner bound (the CLI); a surface that cannot show the image, which the
connector registry states explicitly (``delivers_images=False``, the owner's ruling: the
house pays for an image Telegram never delivers). A task leg also binds no request owner,
so an image made in a leg bills nothing (R9-309). ``code_execution`` is NOT the precedent
there: since 2026-09-19 it reports ``SpendKind.SANDBOX`` through ``report_leg_spend`` and
the leg bills it; the leg ledger has no image kind yet.

Ledger reasons start with ``image_chat`` so the usage breakdown files them under image
spend (``packages/web/src/lib/usage-surfaces.ts`` maps any ``image`` prefix there; a
``chat`` prefix would have landed them under chat).
"""

from __future__ import annotations

import asyncio
import uuid
from typing import TYPE_CHECKING, Final

from persona.billing import BillingConfig, credits_charged
from persona.billing.image_pricing import image_cents
from persona.errors import CreditsExhaustedError, DailySpendCapExceededError
from persona.imagegen.charge import ImageChargeLedgerError, ImageChargeRefusedError
from persona.logging import get_logger

from persona_api.sandbox.context import get_sandbox_request_context
from persona_api.services import audit_service
from persona_api.services.image_cost import generation_cost_cents

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.imagegen import GenerationResult, ImageGenOptions
    from persona_runtime.cost import CostSource
    from sqlalchemy import Engine

    from persona_api.editions.credits_policy import CreditsPolicy

__all__ = ["CHAT_IMAGE_REASON", "ChatImageCharge", "ChatImageChargeHook"]

_LOG = get_logger("api.imagegen.chat_charge")

#: The prefix of every chat image ledger reason and billing key.
CHAT_IMAGE_REASON: Final[str] = "image_chat"

#: How many times each ledger write is attempted: one retry, safe because it is keyed.
_WRITE_ATTEMPTS: Final[int] = 2

#: Why a hold was refused; the reason becomes the tool result's reason.
_NO_CREDITS = "insufficient_credits"
_DAY_CAP = "daily_spend_cap"
_UNAVAILABLE = "billing_unavailable"


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
        """Hold the estimate, or refuse; ``None`` when this turn bills no images."""
        ctx = get_sandbox_request_context()
        if self._policy is None or ctx is None:
            return None
        if not ctx.delivers_images:
            _LOG.info(
                "chat image not charged: this surface cannot deliver images",
                owner=ctx.owner_id,
                provider=provider,
            )
            return None
        estimate = self._estimate(options.count, provider=provider, model=model)
        charge = ChatImageCharge(
            credits_policy=self._policy,
            rls_engine=self._engine,
            cost_source=self._cost_source,
            markup=self._markup,
            floor=self._floor,
            owner_id=ctx.owner_id,
            billing_key=f"{CHAT_IMAGE_REASON}:{uuid.uuid4().hex}",
            held=estimate,
        )
        await charge.hold()
        return charge

    def _estimate(self, count: int, *, provider: str, model: str) -> int:
        """What ``count`` images should cost, in credits, before any exist.

        The registry's per-image price times the count asked for, floored. A backend with
        no row (OpenRouter reports its actual only afterwards) is held at the floor, which
        is all that can be said before the call without guessing a price; the true-up
        captures the rest.
        """
        per_image_cents, _ = image_cents(provider, model=model)
        return credits_charged(
            provider_cents=per_image_cents * count,
            infra_flat_cents=0.0,
            markup=self._markup,
            floor=self._floor,
        )


class ChatImageCharge:
    """One chat image call's money: a keyed hold, then a keyed true-up or release."""

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
        held: int,
    ) -> None:
        self._policy = credits_policy
        self._engine = rls_engine
        self._cost_source = cost_source
        self._markup = markup
        self._floor = floor
        self.owner_id = owner_id
        self.billing_key = billing_key
        self.held = held

    async def hold(self) -> None:
        """Take the estimate before the provider is called, or refuse the image."""
        try:
            await self._write(
                self._policy.deduct_idempotent,
                amount=self.held,
                reason=f"{CHAT_IMAGE_REASON}_pre",
                billing_key=f"{self.billing_key}:pre",
            )
        except CreditsExhaustedError as exc:
            raise _refusal(
                _NO_CREDITS,
                f"Not enough credits to generate this image. It costs about "
                f"{_credits(self.held)}. Add credits to make images.",
            ) from exc
        except DailySpendCapExceededError as exc:
            raise _refusal(
                _DAY_CAP,
                "Today's spending limit has been reached, so this image was not made. "
                "The limit resets at midnight UTC.",
            ) from exc
        except _LedgerWriteFailedError as exc:
            raise _refusal(
                _UNAVAILABLE,
                "Credits could not be checked just now, so this image was not made. "
                "Try again in a moment.",
            ) from exc

    async def settle(self, result: GenerationResult) -> None:
        """True the hold up to what ``result`` cost; a repeat moves nothing."""
        cost_cents, basis = generation_cost_cents(result, cost_source=self._cost_source)
        if basis == "unpriced":
            _LOG.warning(
                "chat image billed at the floor: no provider cost and no pricing row "
                "provider={provider} model={model}",
                provider=result.provider,
                model=result.model,
            )
        real = credits_charged(
            provider_cents=cost_cents,
            infra_flat_cents=0.0,  # infra via the credit floor (D-M3-4 amendment)
            markup=self._markup,
            floor=self._floor,
        )
        delta = real - self.held
        key = f"{self.billing_key}:settle"
        try:
            if delta >= 0:
                # A zero capture still writes the row that carries what the call cost.
                await self._write(
                    self._policy.capture_up_to_idempotent,
                    amount=delta,
                    reason=f"{CHAT_IMAGE_REASON}:{basis}",
                    billing_key=key,
                    cost_cents=cost_cents,
                    cost_basis=basis,
                )
            else:
                await self._write(
                    self._policy.grant_idempotent,
                    amount=-delta,
                    reason=f"{CHAT_IMAGE_REASON}_trueup:{basis}",
                    billing_key=key,
                    cost_cents=cost_cents,
                    cost_basis=basis,
                )
        except DailySpendCapExceededError:
            # The hold was inside the cap; only the part above the estimate is past it.
            self._record_miss(
                "image_chat_unbilled",
                amount=delta,
                cost_cents=cost_cents,
                basis=str(basis),
                cause="daily_spend_cap",
            )
        except _LedgerWriteFailedError as exc:
            self._record_miss(
                "image_chat_unbilled",
                amount=delta,
                cost_cents=cost_cents,
                basis=str(basis),
                cause=exc.cause,
            )
            raise ImageChargeLedgerError(
                "chat image true-up could not be written",
                context={"billing_key": self.billing_key, "amount": str(delta)},
            ) from exc

    async def release(self) -> None:
        """Give the whole hold back after a failed generation; a repeat moves nothing."""
        try:
            await self._write(
                self._policy.grant_idempotent,
                amount=self.held,
                reason=f"{CHAT_IMAGE_REASON}_refund:provider_failure",
                billing_key=f"{self.billing_key}:release",
            )
        except _LedgerWriteFailedError as exc:
            self._record_miss(
                "image_chat_unrefunded",
                amount=self.held,
                cost_cents=None,
                basis="",
                cause=exc.cause,
            )
            raise ImageChargeLedgerError(
                "chat image hold could not be released",
                context={"billing_key": self.billing_key, "amount": str(self.held)},
            ) from exc

    async def _write(
        self, method: Callable[..., object], **kwargs: str | int | float | None
    ) -> None:
        """Run one keyed ledger write, retrying once; the key makes the retry safe.

        A refusal the policy raises on purpose (no credits, the day cap) is not a failure
        and is not retried. Anything else that survives the retry becomes
        :class:`_LedgerWriteFailedError`.
        """
        for attempt in range(1, _WRITE_ATTEMPTS + 1):
            try:
                await asyncio.to_thread(
                    method, rls_engine=self._engine, user_id=self.owner_id, **kwargs
                )
            except (CreditsExhaustedError, DailySpendCapExceededError):
                raise
            except Exception as exc:  # noqa: BLE001 (any ledger fault; re-raised as ours)
                if attempt == _WRITE_ATTEMPTS:
                    raise _LedgerWriteFailedError(type(exc).__name__) from exc
                _LOG.warning(
                    "chat image ledger write failed; retrying under the same key key={key}",
                    key=kwargs.get("billing_key", ""),
                )
            else:
                return

    def _record_miss(
        self, action: str, *, amount: int, cost_cents: float | None, basis: str, cause: str
    ) -> None:
        """Leave a durable audit row for money that did not move; no content, only figures."""
        _LOG.warning(
            "{action}: owner={owner} key={key} amount={amount} cause={cause}",
            action=action,
            owner=self.owner_id,
            key=self.billing_key,
            amount=amount,
            cause=cause,
        )
        audit_service.record(
            engine=self._engine,
            user_id=self.owner_id,
            action=action,
            target=self.billing_key,
            metadata={
                "billing_key": self.billing_key,
                "credits": str(amount),
                "held": str(self.held),
                "cost_cents": "" if cost_cents is None else str(cost_cents),
                "cost_basis": basis,
                "cause": cause,
            },
        )


class _LedgerWriteFailedError(Exception):
    """A keyed ledger write failed twice. ``cause`` names the last error's type."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


def _credits(amount: int) -> str:
    return f"{amount} credit{'s' if amount != 1 else ''}"


def _refusal(reason: str, message: str) -> ImageChargeRefusedError:
    return ImageChargeRefusedError(message, context={"reason": reason})
