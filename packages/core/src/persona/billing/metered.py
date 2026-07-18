"""The one billing seam — ``meter → price → deduct → record`` (Spec M3, D-M3-1/2).

Every paid surface bills through :class:`MeteredBilling`: it takes the surface's
metered real cost (``provider_cents``) + its infra flat, applies the one credit
formula (:func:`~persona.billing.formula.credits_charged`), and records the
charge through an injected :class:`LedgerPort` — carrying the true pre-markup
``cost_cents`` + the ``cost_basis`` + an optional idempotency ``billing_key``.

**Placement (D-M3-core-seam, hard constraint).** The seam lives in persona-core
over ``persona.credits`` and MUST NOT import persona-api. persona-voice injects
:class:`CoreCreditsLedger` (direct ``persona.credits``, no HTTP hop —
latency-critical); persona-api injects a ``CreditsPolicy``-backed adapter (T1b).

**Modes.**

* ``strict`` — all-or-nothing (raises :class:`~persona.errors.CreditsExhaustedError`
  when the balance can't cover the charge). For hard-gated callers.
* ``capture`` — partial-capture floored at 0 (never overdraws). For post-success
  / incremental billing (chat, agentic, voice) where the work already happened.

Passing a ``billing_key`` selects the idempotent variant of the chosen mode
(insert-first ``ON CONFLICT DO NOTHING`` — a re-delivered op does not
double-charge, D-M3-R5). No ``billing_key`` keeps the byte-identical non-keyed
``persona.credits`` paths (chat parity, T1b).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from persona.billing.basis import (
    CostBasis,  # noqa: TC001 — Pydantic needs the field type at runtime
)
from persona.billing.formula import credits_charged

if TYPE_CHECKING:
    from sqlalchemy import Engine

    from persona.billing.formula import BillingConfig

__all__ = [
    "ChargeMode",
    "ChargeResult",
    "CoreCreditsLedger",
    "LedgerPort",
    "MeteredBilling",
]

ChargeMode = Literal["strict", "capture"]


@runtime_checkable
class LedgerPort(Protocol):
    """The ledger surface :class:`MeteredBilling` records through.

    Mirrors ``persona.credits`` (keyword-only). ``persona-voice`` and background
    workers inject :class:`CoreCreditsLedger`; persona-api injects a
    ``CreditsPolicy``-backed adapter (T1b). The ``cost_cents`` / ``cost_basis``
    (provenance, D-M3-12) and ``billing_key`` (idempotency, D-M3-R5) are the M3
    additions over the M2 ledger surface.
    """

    def deduct(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        amount: int,
        reason: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int: ...

    def capture_up_to(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        amount: int,
        reason: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> tuple[int, int]: ...

    def deduct_idempotent(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        amount: int,
        reason: str,
        billing_key: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int: ...

    def capture_up_to_idempotent(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        amount: int,
        reason: str,
        billing_key: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> tuple[int, int]: ...


class ChargeResult(BaseModel):
    """The outcome of one :meth:`MeteredBilling.charge` (frozen boundary type).

    Attributes:
        charged: The whole-credit charge the formula produced (the *requested*
            amount; ``captured`` is what actually landed in ``capture`` mode).
        cost_cents: The true pre-markup provider cost recorded on the ledger row
            (``provider_cents``; ``0.0`` for a zero-provider ``infra_flat``
            surface — the basis, not the cost, marks it).
        cost_basis: The recorded provenance.
        new_balance: The balance after the move.
        captured: In ``capture`` mode, the credits actually captured
            (``<= charged`` when the balance was short); ``None`` in ``strict``
            mode.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    charged: int
    cost_cents: float
    cost_basis: CostBasis
    new_balance: int
    captured: int | None = None


class MeteredBilling:
    """Price a surface's real cost and record the charge through the ledger.

    Pure DI: the ledger and the billing config are injected; the seam owns no
    state and no I/O beyond the one ledger call.
    """

    def __init__(self, *, ledger: LedgerPort, config: BillingConfig) -> None:
        self._ledger = ledger
        self._config = config

    def price(
        self,
        *,
        provider_cents: float,
        infra_flat_cents: float,
        floor: int,
    ) -> int:
        """The whole-credit charge for this cost (the formula; no I/O).

        Exposed so a surface can pre-flight the charge (e.g. an image
        ceiling-estimate pre-deduct) without recording it.
        """
        return credits_charged(
            provider_cents=provider_cents,
            infra_flat_cents=infra_flat_cents,
            markup=self._config.credit_markup,
            floor=floor,
        )

    def charge(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        provider_cents: float,
        infra_flat_cents: float,
        basis: CostBasis,
        reason: str,
        floor: int,
        mode: ChargeMode = "strict",
        billing_key: str | None = None,
    ) -> ChargeResult:
        """Price ``(provider_cents + infra_flat_cents)`` and record the charge.

        Args:
            rls_engine: The RLS-scoped engine (the payer's tenant scope).
            user_id: The payer (caller for a turn, owner for background work).
            provider_cents: The call's real provider cost in cents (``0.0`` for a
                zero-provider surface). Recorded verbatim as the ledger
                ``cost_cents`` (true pre-markup truth — D-M3-12).
            infra_flat_cents: The additive infra flat rate for this unit.
            basis: The recorded ``cost_basis`` provenance.
            reason: The human ledger label (e.g. ``"image_gen"`` / ``"voice"``).
            floor: The surface's minimum whole-credit charge.
            mode: ``"strict"`` (all-or-nothing, raises on exhaustion) or
                ``"capture"`` (partial, floored at 0).
            billing_key: When set, uses the idempotent ledger variant — a
                re-delivered op with the same key does not double-charge
                (D-M3-R5). ``None`` keeps the byte-identical non-keyed path.

        Returns:
            A :class:`ChargeResult`.
        """
        charged = self.price(
            provider_cents=provider_cents, infra_flat_cents=infra_flat_cents, floor=floor
        )
        if mode == "strict":
            if billing_key is None:
                new_balance = self._ledger.deduct(
                    rls_engine=rls_engine,
                    user_id=user_id,
                    amount=charged,
                    reason=reason,
                    cost_cents=provider_cents,
                    cost_basis=basis,
                )
            else:
                new_balance = self._ledger.deduct_idempotent(
                    rls_engine=rls_engine,
                    user_id=user_id,
                    amount=charged,
                    reason=reason,
                    billing_key=billing_key,
                    cost_cents=provider_cents,
                    cost_basis=basis,
                )
            return ChargeResult(
                charged=charged,
                cost_cents=provider_cents,
                cost_basis=basis,
                new_balance=new_balance,
                captured=None,
            )
        # capture mode
        if billing_key is None:
            captured, new_balance = self._ledger.capture_up_to(
                rls_engine=rls_engine,
                user_id=user_id,
                amount=charged,
                reason=reason,
                cost_cents=provider_cents,
                cost_basis=basis,
            )
        else:
            captured, new_balance = self._ledger.capture_up_to_idempotent(
                rls_engine=rls_engine,
                user_id=user_id,
                amount=charged,
                reason=reason,
                billing_key=billing_key,
                cost_cents=provider_cents,
                cost_basis=basis,
            )
        return ChargeResult(
            charged=charged,
            cost_cents=provider_cents,
            cost_basis=basis,
            new_balance=new_balance,
            captured=captured,
        )


class CoreCreditsLedger:
    """A :class:`LedgerPort` over ``persona.credits`` (voice + background injection).

    The direct-core ledger the latency-critical voice surface and the background
    workers use — no persona-api hop (D-M3-core-seam). Forwards the optional
    ``daily_cap`` (R7) it was constructed with; ``0`` = uncapped (the default,
    byte-identical to pre-R7).
    """

    def __init__(self, *, daily_cap: int = 0) -> None:
        self._daily_cap = daily_cap

    def deduct(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        amount: int,
        reason: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int:
        from persona.credits import deduct  # noqa: PLC0415 — avoid import cycle at module load

        return deduct(
            rls_engine=rls_engine,
            user_id=user_id,
            amount=amount,
            reason=reason,
            daily_cap=self._daily_cap,
            cost_cents=cost_cents,
            cost_basis=cost_basis,
        )

    def capture_up_to(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        amount: int,
        reason: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> tuple[int, int]:
        from persona.credits import capture_up_to  # noqa: PLC0415

        return capture_up_to(
            rls_engine=rls_engine,
            user_id=user_id,
            amount=amount,
            reason=reason,
            daily_cap=self._daily_cap,
            cost_cents=cost_cents,
            cost_basis=cost_basis,
        )

    def deduct_idempotent(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        amount: int,
        reason: str,
        billing_key: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int:
        from persona.credits import deduct_idempotent  # noqa: PLC0415

        return deduct_idempotent(
            rls_engine=rls_engine,
            user_id=user_id,
            amount=amount,
            reason=reason,
            billing_key=billing_key,
            daily_cap=self._daily_cap,
            cost_cents=cost_cents,
            cost_basis=cost_basis,
        )

    def capture_up_to_idempotent(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        amount: int,
        reason: str,
        billing_key: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> tuple[int, int]:
        from persona.credits import capture_up_to_idempotent  # noqa: PLC0415

        return capture_up_to_idempotent(
            rls_engine=rls_engine,
            user_id=user_id,
            amount=amount,
            reason=reason,
            billing_key=billing_key,
            daily_cap=self._daily_cap,
            cost_cents=cost_cents,
            cost_basis=cost_basis,
        )
