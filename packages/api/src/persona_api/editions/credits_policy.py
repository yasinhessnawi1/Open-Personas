"""The credits seam (Spec 33, §2.2 / D-33-X-creditspolicy-di).

``CreditsPolicy`` is the interface every metered op calls — injected via
``app.state`` (mirroring how ``rls_engine`` is threaded), so call sites never
import the concrete service and there are no scattered ``if edition`` checks
(acceptance criterion 3).

- :class:`MeteredCreditsPolicy` delegates to the existing ``persona.credits``
  surface — cloud, behavior unchanged.
- :class:`UnlimitedCreditsPolicy` — community: every check passes, deduct/refund
  are no-ops, balance reads return a large constant. Never touches the DB
  (community has no credits ledger to consult).

The method surface mirrors ``persona.credits`` exactly (keyword-only,
``rls_engine`` + ``user_id`` …) so the swap is a drop-in at every call site.
"""

# The community no-op methods intentionally ignore their interface arguments
# (they must keep the exact parameter NAMES so keyword call sites are
# drop-in-compatible, so they cannot be renamed to ``_``).
# ruff: noqa: ARG002

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.credits import (
    capture_up_to as _capture_up_to,
)
from persona.credits import (
    capture_up_to_idempotent as _capture_up_to_idempotent,
)
from persona.credits import (
    deduct as _deduct,
)
from persona.credits import (
    deduct_idempotent as _deduct_idempotent,
)
from persona.credits import (
    get_balance as _get_balance,
)
from persona.credits import (
    grant_idempotent as _grant_idempotent,
)
from persona.credits import (
    grant_payg_lot_idempotent as _grant_payg_lot_idempotent,
)
from persona.credits import (
    list_turn_usage as _list_turn_usage,
)
from persona.credits import (
    list_usage as _list_usage,
)
from persona.credits import (
    refresh_free_allowance_lazy as _refresh_free_allowance_lazy,
)
from persona.credits import (
    refund as _refund,
)
from persona.credits import (
    require_credits as _require_credits,
)
from persona.credits import (
    reset_allowance_idempotent as _reset_allowance_idempotent,
)
from persona.credits import (
    wallet_snapshot as _wallet_snapshot,
)
from persona.errors import DailySpendCapExceededError

if TYPE_CHECKING:
    from sqlalchemy import Engine

__all__ = [
    "CreditsPolicy",
    "MeteredCreditsPolicy",
    "UnlimitedCreditsPolicy",
]

# The notional balance the community policy reports — large enough that any UI
# low-balance threshold reads "plenty", small enough to be obviously sentinel.
_UNLIMITED_BALANCE = 1_000_000_000


@runtime_checkable
class CreditsPolicy(Protocol):
    """Pre-flight gate + ledger moves for metered operations."""

    def require_credits(self, *, rls_engine: Engine, user_id: str) -> int:
        """Pre-flight check; raise ``CreditsExhaustedError`` (→ 402) if empty."""
        ...

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
        """Deduct ``amount`` + record a ledger row. Returns the new balance.

        Spec M3 (D-M3-12): ``cost_cents`` (true provider cost pre-markup) and
        ``cost_basis`` (provenance) are recorded on the row when supplied; both
        default ``None`` (pre-M3 callers write them as ``NULL``, byte-identical).
        """
        ...

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
        """Charge ``min(amount, balance)``, floored at 0. Returns ``(captured, new_balance)``.

        Spec M2 review (C1): the opt-in partial-capture path. ONLY the
        chat-turn worker's post-success billing calls this — every other
        metered caller keeps using :meth:`deduct`, whose all-or-nothing
        semantics this method does not alter. Spec M3 (D-M3-12): records
        ``cost_cents`` / ``cost_basis`` when supplied.
        """
        ...

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
        """Idempotent all-or-nothing deduct keyed on ``billing_key`` (Spec M3, D-M3-R5).

        For at-least-once callers (owner-billed avatar / background / task
        deducts): a re-delivered op with the same key does not double-charge.
        """
        ...

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
        """Idempotent partial-capture keyed on ``billing_key`` (Spec M3, D-M3-R5).

        The floored sibling of :meth:`deduct_idempotent` for POST-SUCCESS
        at-least-once billing of completed work (task legs): captures what the
        balance covers rather than hard-failing an already-done leg, and a
        re-delivery with the same key does not double-charge. Returns
        ``(captured, new_balance)``.
        """
        ...

    def grant_idempotent(
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
        """Idempotent positive-delta grant to the allowance bucket (Spec M4, T1b).

        The exactly-once grant the Stripe webhook rides — a subscription renewal
        (``grant_subscription``) or a monthly free refresh (``grant_free_refresh``)
        credits the allowance bucket; a re-delivered event (same ``billing_key``)
        grants nothing. Returns the new total spendable.
        """
        ...

    def reset_allowance_idempotent(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        allowance: int,
        allowance_period: str,
        reason: str,
        billing_key: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int:
        """Idempotent OVERWRITE of the allowance bucket to ``allowance`` (Spec M4, T3b).

        The subscription-renewal reset (owner Decision 1: overwrite, no rollover), keyed
        on the invoice id; a re-delivered ``invoice.paid`` resets exactly once. Returns
        the new total spendable.
        """
        ...

    def grant_payg_lot_idempotent(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        credit_amount: int,
        reason: str,
        source_billing_key: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int:
        """Idempotently grant a PAYG lot (12-mo expiry) keyed on the PI id (Spec M4, T4a).

        A one-time pack purchase grants a ``payg_grants`` lot (the SEPARATE PAYG bucket —
        allowance untouched); a re-delivered ``payment_intent.succeeded`` grants one lot.
        Returns the new total spendable.
        """
        ...

    def refund(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        amount: int,
        reason: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int:
        """Reverse-deduct via a ledger entry. Returns the new balance.

        Spec M3 (D-M3-12): records ``cost_cents`` / ``cost_basis`` when supplied
        (the image true-up overage refund carries the refunded charge's basis).
        """
        ...

    def get_balance(self, *, rls_engine: Engine, user_id: str) -> int:
        """The current balance."""
        ...

    def wallet_snapshot(self, *, rls_engine: Engine, user_id: str) -> dict[str, object]:
        """The two-bucket wallet read (Spec M4 T8): allowance + PAYG lots + total."""
        ...

    def list_usage(
        self, *, rls_engine: Engine, user_id: str, limit: int, offset: int
    ) -> list[dict[str, object]]:
        """The credit-transaction log (paginated)."""
        ...

    def list_turn_usage(
        self, *, rls_engine: Engine, limit: int, offset: int
    ) -> list[dict[str, object]]:
        """Per-turn token usage (paginated)."""
        ...


class MeteredCreditsPolicy:
    """Cloud: the existing metered ledger (delegates to ``persona.credits``).

    Spec R7 (R7-D-1/6): carries the per-UTC-day spend cap. ``deduct`` books the
    day-cap atomically-with the credit decrement (the core ``deduct`` does this in
    one transaction when ``daily_cap > 0``); an over-cap spend raises
    :class:`DailySpendCapExceededError` (→ 429) AND writes a durable ``audit_log``
    refusal row (R7-D-5 fail-loud + audited). ``daily_cap = 0`` (the community/uncapped
    default) leaves behaviour byte-identical to pre-R7.
    """

    def __init__(self, *, daily_cap: int = 0) -> None:
        self._daily_cap = daily_cap

    def _refresh_free_allowance(self, *, rls_engine: Engine, user_id: str) -> None:
        """Spec M4 T6: self-heal a free user's monthly allowance on access (cloud only).

        Lazily overwrites a free user's allowance to the free plan's amount ($3) once per
        UTC month (idempotent; a paid user is never touched — the core ``NOT EXISTS`` guard).

        Fired at the READ/pre-flight gates (``require_credits`` / ``get_balance``) AND the
        IDEMPOTENT ``deduct``/``capture`` variants — the background/owner-billed paths that
        skip the pre-flight (so a dormant free user self-heals even on a pure background
        deduct, closing that edge). Deliberately NOT fired in the plain ``deduct`` /
        ``capture_up_to``: every user-facing route reaching those runs a ``require_credits``
        pre-flight FIRST, which already refreshed — so a second call there is redundant and
        would clobber a paid/seeded balance in the policy-level unit tests. Community's
        :class:`UnlimitedCreditsPolicy` never runs this (it overrides every method as a
        no-op), so the refresh is cloud-only / edition-gated, community byte-identical.
        """
        _refresh_free_allowance_lazy(rls_engine=rls_engine, user_id=user_id)

    def require_credits(self, *, rls_engine: Engine, user_id: str) -> int:
        self._refresh_free_allowance(rls_engine=rls_engine, user_id=user_id)
        return _require_credits(rls_engine=rls_engine, user_id=user_id)

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
        # No free-allowance refresh here: every user-facing route that reaches the plain
        # ``deduct`` (chat post-success, imagegen pre-deduct, authoring) runs a
        # ``require_credits`` pre-flight FIRST — which already self-heals the free allowance
        # (Spec M4 T6). The refresh rides ``require_credits`` / ``get_balance`` + the
        # idempotent variants (the background/owner-billed paths that skip the pre-flight);
        # firing it again here would be redundant AND would clobber a paid/seeded balance in
        # the policy-level unit tests that deduct without a pre-flight.
        try:
            return _deduct(
                rls_engine=rls_engine,
                user_id=user_id,
                amount=amount,
                reason=reason,
                daily_cap=self._daily_cap,
                cost_cents=cost_cents,
                cost_basis=cost_basis,
            )
        except DailySpendCapExceededError as exc:
            # FAIL-LOUD + audited (R7-D-5): the spend rolled back in ``_deduct``'s
            # transaction; record the refusal durably in its OWN transaction so the
            # audit survives regardless. The audit write never masks the refusal —
            # any audit failure propagates (a money-guard that can't be audited must
            # not silently pass).
            self._audit_daily_cap_refusal(
                rls_engine=rls_engine, user_id=user_id, context=exc.context
            )
            raise

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
        """Spec M2 review (C1): semantics-free passthrough to ``persona.credits.capture_up_to``.

        This policy layer decides NOTHING about amounts, floors, or shortfall
        marking — that arithmetic lives entirely in the core service (F7
        precedent: the policy stays amount-agnostic). It only forwards the
        day-cap it already owns (mirroring :meth:`deduct`) and, on a day-cap
        refusal, writes the SAME durable audit row :meth:`deduct` writes
        (R7-D-5 fail-loud + audited) before re-raising. Spec M3 (D-M3-12):
        forwards ``cost_cents`` / ``cost_basis`` to the recorded row.

        No free-allowance refresh here (Spec M4 T6): the chat-turn worker's post-success
        ``capture_up_to`` runs AFTER the turn's ``require_credits`` pre-flight, which already
        self-healed the free allowance. The refresh rides ``require_credits`` / ``get_balance``
        + the idempotent variants (background paths); firing it here would clobber a seeded
        balance in the policy-level worker tests.
        """
        try:
            return _capture_up_to(
                rls_engine=rls_engine,
                user_id=user_id,
                amount=amount,
                reason=reason,
                daily_cap=self._daily_cap,
                cost_cents=cost_cents,
                cost_basis=cost_basis,
            )
        except DailySpendCapExceededError as exc:
            self._audit_daily_cap_refusal(
                rls_engine=rls_engine, user_id=user_id, context=exc.context
            )
            raise

    @staticmethod
    def _audit_daily_cap_refusal(
        *, rls_engine: Engine, user_id: str, context: dict[str, str]
    ) -> None:
        from sqlalchemy import insert

        from persona_api.db.models import audit_log

        with rls_engine.begin() as conn:
            conn.execute(
                insert(audit_log).values(
                    user_id=user_id,
                    action="daily_spend_cap_exceeded",
                    target=user_id,
                    metadata={
                        "cap": context.get("cap", ""),
                        "spent": context.get("spent", ""),
                        "requested_cost": context.get("requested_cost", ""),
                    },
                )
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
        self._refresh_free_allowance(rls_engine=rls_engine, user_id=user_id)
        try:
            return _deduct_idempotent(
                rls_engine=rls_engine,
                user_id=user_id,
                amount=amount,
                reason=reason,
                billing_key=billing_key,
                daily_cap=self._daily_cap,
                cost_cents=cost_cents,
                cost_basis=cost_basis,
            )
        except DailySpendCapExceededError as exc:
            self._audit_daily_cap_refusal(
                rls_engine=rls_engine, user_id=user_id, context=exc.context
            )
            raise

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
        self._refresh_free_allowance(rls_engine=rls_engine, user_id=user_id)
        try:
            return _capture_up_to_idempotent(
                rls_engine=rls_engine,
                user_id=user_id,
                amount=amount,
                reason=reason,
                billing_key=billing_key,
                daily_cap=self._daily_cap,
                cost_cents=cost_cents,
                cost_basis=cost_basis,
            )
        except DailySpendCapExceededError as exc:
            self._audit_daily_cap_refusal(
                rls_engine=rls_engine, user_id=user_id, context=exc.context
            )
            raise

    def grant_idempotent(
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
        # Grants are not spend — no day-cap (that guards deductions); a plain delegate.
        return _grant_idempotent(
            rls_engine=rls_engine,
            user_id=user_id,
            amount=amount,
            reason=reason,
            billing_key=billing_key,
            cost_cents=cost_cents,
            cost_basis=cost_basis,
        )

    def reset_allowance_idempotent(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        allowance: int,
        allowance_period: str,
        reason: str,
        billing_key: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int:
        # An overwrite reset, not spend — no day-cap; a plain delegate.
        return _reset_allowance_idempotent(
            rls_engine=rls_engine,
            user_id=user_id,
            allowance=allowance,
            allowance_period=allowance_period,
            reason=reason,
            billing_key=billing_key,
            cost_cents=cost_cents,
            cost_basis=cost_basis,
        )

    def grant_payg_lot_idempotent(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        credit_amount: int,
        reason: str,
        source_billing_key: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int:
        # A PAYG lot grant, not spend — no day-cap; a plain delegate.
        return _grant_payg_lot_idempotent(
            rls_engine=rls_engine,
            user_id=user_id,
            credit_amount=credit_amount,
            reason=reason,
            source_billing_key=source_billing_key,
            cost_cents=cost_cents,
            cost_basis=cost_basis,
        )

    def refund(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        amount: int,
        reason: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int:
        return _refund(
            rls_engine=rls_engine,
            user_id=user_id,
            amount=amount,
            reason=reason,
            cost_cents=cost_cents,
            cost_basis=cost_basis,
        )

    def get_balance(self, *, rls_engine: Engine, user_id: str) -> int:
        self._refresh_free_allowance(rls_engine=rls_engine, user_id=user_id)
        return _get_balance(rls_engine=rls_engine, user_id=user_id)

    def wallet_snapshot(self, *, rls_engine: Engine, user_id: str) -> dict[str, object]:
        # A balance-read surface (the settings wallet page) — self-heal the free monthly
        # allowance first (Spec M4 T6/T8, same as get_balance) so a dormant free user's
        # wallet always shows the current month's allowance.
        self._refresh_free_allowance(rls_engine=rls_engine, user_id=user_id)
        return _wallet_snapshot(rls_engine=rls_engine, user_id=user_id)

    def list_usage(
        self, *, rls_engine: Engine, user_id: str, limit: int, offset: int
    ) -> list[dict[str, object]]:
        return _list_usage(rls_engine=rls_engine, user_id=user_id, limit=limit, offset=offset)

    def list_turn_usage(
        self, *, rls_engine: Engine, limit: int, offset: int
    ) -> list[dict[str, object]]:
        return _list_turn_usage(rls_engine=rls_engine, limit=limit, offset=offset)


class UnlimitedCreditsPolicy:
    """Community: unmetered — every check passes, moves are no-ops (D-33-X-creditspolicy-di)."""

    def require_credits(self, *, rls_engine: Engine, user_id: str) -> int:
        return _UNLIMITED_BALANCE

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
        return _UNLIMITED_BALANCE

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
        # Never reached in practice (community's ``deduct`` never raises), but
        # implemented for Protocol completeness: a no-op that "fully captures".
        return amount, _UNLIMITED_BALANCE

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
        return _UNLIMITED_BALANCE

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
        return amount, _UNLIMITED_BALANCE

    def grant_idempotent(
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
        # Community: unmetered — no plans, no grants, no DB write (M4 is a cloud no-op).
        return _UNLIMITED_BALANCE

    def reset_allowance_idempotent(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        allowance: int,
        allowance_period: str,
        reason: str,
        billing_key: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int:
        # Community: unmetered — no plans, no allowance reset, no DB write (cloud no-op).
        return _UNLIMITED_BALANCE

    def grant_payg_lot_idempotent(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        credit_amount: int,
        reason: str,
        source_billing_key: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int:
        # Community: unmetered — no PAYG packs, no lot, no DB write (cloud no-op).
        return _UNLIMITED_BALANCE

    def refund(
        self,
        *,
        rls_engine: Engine,
        user_id: str,
        amount: int,
        reason: str,
        cost_cents: float | None = None,
        cost_basis: str | None = None,
    ) -> int:
        return _UNLIMITED_BALANCE

    def get_balance(self, *, rls_engine: Engine, user_id: str) -> int:
        return _UNLIMITED_BALANCE

    def wallet_snapshot(self, *, rls_engine: Engine, user_id: str) -> dict[str, object]:
        # Community: unmetered — no buckets, no lots, no DB read (the sentinel shape).
        return {
            "allowance_balance": _UNLIMITED_BALANCE,
            "allowance_period": None,
            "payg_lots": [],
            "total_balance": _UNLIMITED_BALANCE,
        }

    def list_usage(
        self, *, rls_engine: Engine, user_id: str, limit: int, offset: int
    ) -> list[dict[str, object]]:
        return []

    def list_turn_usage(
        self, *, rls_engine: Engine, limit: int, offset: int
    ) -> list[dict[str, object]]:
        return []
