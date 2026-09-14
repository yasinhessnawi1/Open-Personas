"""The task cost ledger (Spec A2, T2, criterion 8).

A0 *meters* spend per leg (model / sandbox / external); A2 *accounts* per task; A3
*enforces* against the total (D-A0-X-metering-bar). This module is the pure accounting
value type: a frozen per-kind tally with a functional :meth:`CostLedger.record` that
returns a new ledger (the durable store persists it; the leg handler adds to it
atomically with the checkpoint append — D-A2-X-idempotency).
"""

from __future__ import annotations

from decimal import ROUND_CEILING, Decimal
from enum import StrEnum
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["MICROS_PER_CENT", "CostLedger", "SpendKind", "micros_from_cents"]

#: Ledger micros per cent of priced cost.
#:
#: The ledger's unit is the **micro**, and 10 000 micros is one unit of account: that is
#: the unit the contract's ``ContractBounds.total_budget_micros`` cap is written in and the
#: unit the task surfaces render as money. The project's pricing path is denominated in
#: **cents** throughout (Spec M2's ``compute_turn_cost`` returns cost in cents; Spec M3
#: accounts at 1 credit = 1 cent), so one cent is 100 micros. This constant is the single
#: place the two scales meet: anything that turns a priced cost into ledger spend goes
#: through :func:`micros_from_cents` rather than scaling the number itself.
MICROS_PER_CENT: Final[int] = 100


class SpendKind(StrEnum):
    """The spend class A0 meters and A2 accounts (mirrors the A0 ``meter(kind=...)``)."""

    MODEL = "model"
    SANDBOX = "sandbox"
    EXTERNAL = "external"


class CostLedger(BaseModel):
    """Cumulative task spend, tallied by kind in ``amount_micros`` (criterion 8).

    Frozen; :meth:`record` returns a new ledger (functional update, like ``Job``'s
    transition). ``total_micros`` is the number A3 enforces against and A6 displays.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    model_micros: int = Field(default=0, ge=0)
    sandbox_micros: int = Field(default=0, ge=0)
    external_micros: int = Field(default=0, ge=0)

    @property
    def total_micros(self) -> int:
        """The grand total across all spend kinds."""
        return self.model_micros + self.sandbox_micros + self.external_micros

    def record(self, kind: SpendKind, amount_micros: int) -> CostLedger:
        """Return a new ledger with ``amount_micros`` added to ``kind``'s tally.

        Args:
            kind: The spend class to credit.
            amount_micros: A non-negative spend amount (``amount_micros`` unit).

        Returns:
            A new :class:`CostLedger`; the receiver is unchanged (frozen).

        Raises:
            ValueError: If ``amount_micros`` is negative.
        """
        if amount_micros < 0:
            msg = "spend amount must be non-negative"
            raise ValueError(msg)
        field = f"{kind.value}_micros"
        current: int = getattr(self, field)
        return self.model_copy(update={field: current + amount_micros})


def micros_from_cents(cost_cents: float) -> int:
    """Convert a priced cost in cents into ledger micros (the unit the cap is written in).

    The one conversion between the pricing path's cents and the ledger's micros. It exists
    because the two scales are easy to mix up and the consequence of mixing them up is a
    safety bound that does not mean what the user was told it means: before this, the task
    ledger accrued a raw token count into a field compared against a money cap.

    Rounds **up** to the next whole micro, on the printed decimal value so binary-float
    noise can never invent a micro (the same discipline
    :func:`persona.billing.formula.credits_charged` uses). Up, because under-recording
    spend loosens a bound whose whole job is to stop a runaway task; the error is a
    hundredth of a cent per leg either way.

    Args:
        cost_cents: The priced cost in cents. Negative values (a defensive case the
            pricing path already rules out) are clamped to zero rather than credited back
            against the cap.

    Returns:
        The spend in ledger micros, ready for :meth:`CostLedger.record`.
    """
    if cost_cents <= 0:
        return 0
    micros = Decimal(str(cost_cents)) * MICROS_PER_CENT
    return int(micros.to_integral_value(rounding=ROUND_CEILING))
