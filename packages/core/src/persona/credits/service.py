"""Credits counter + deduction (relocated from ``persona_api.services.credits_service``).

Originally landed at spec 08, T12, §5.5, D-08-6; refunds added at spec 15 T13
(D-15-X-credit-flow-semantics). Relocated to persona-core at Spec 19 L6c
(D-19-X-credits-service-domain-relocation) so persona-voice can consume the
same surface without taking a persona-api dependency — the voice surface is
latency-critical (R-V1-1) and cannot afford an HTTP/RPC hop to the API.

The implementation is verbatim from the prior ``persona_api.services.credits_service``
with one structural change: the SQLAlchemy ``Table`` objects are defined here
on a private :class:`MetaData` (mirroring the pattern used by
:mod:`persona.stores.postgres` for ``memory_chunks``). Column names/types match
the api-owned canonical schema in ``persona_api.db.models``; the existing
api-side route integration tests act as the contract guard that the two views
agree.

A stub counter (100,000 default; no payment integration — spec 08 §2 out-of-scope)
so the architecture is forward-compatible. Deducted per successful turn AFTER
the stream completes (a failed/cancelled turn doesn't deduct — mirrors the
persist-after-final discipline) and a flat amount per authoring call (§11 risk).
Every deduction writes a ``credit_transactions`` row (the audit trail).

Both tables are RLS-scoped via ``user_id``, so all access runs under the
caller's tenant scope.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    Integer,
    MetaData,
    Table,
    Text,
    func,
    insert,
    select,
    text,
    update,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert

from persona.billing.plans import default_plan
from persona.errors import CreditsExhaustedError, DailySpendCapExceededError

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine

__all__ = [
    "book_day_spend",
    "capture_up_to",
    "capture_up_to_idempotent",
    "deduct",
    "deduct_idempotent",
    "ensure_balance",
    "get_balance",
    "grant_idempotent",
    "grant_payg_lot_idempotent",
    "list_turn_usage",
    "list_usage",
    "refresh_free_allowance_lazy",
    "refund",
    "require_credits",
    "reset_allowance_idempotent",
    "wallet_snapshot",
]

# Spec M4 T6 (Option A, D-M4-rename precedent): the pre-first-refresh SEED for a new
# ``credits`` row — NOT the free allowance. The real free allowance is the plans-catalog
# value (``default_plan().included_allowance_credits`` = 300 = $3) that the lazy monthly
# refresh (:func:`refresh_free_allowance_lazy`) overwrites onto the row on a free user's
# first metered access (their ``allowance_period`` is NULL ⇒ the refresh fires at once, so
# the 100_000 seed is never meaningfully spendable). Kept at 100_000 so the M3 credits
# parity + imagegen suites — which call this core surface DIRECTLY, below the policy that
# fires the refresh — stay pristine (zero-edit).
_DEFAULT_BALANCE = 100_000
# Spec M4 T8: the flat LOW_BALANCE_THRESHOLD (10_000, D-11-12) is RETIRED — the
# low-balance warning line is now PER-PLAN (20% of the plan's included allowance,
# ``persona.billing.plans.Plan.low_balance_threshold_credits``); the /v1/me routes
# resolve the caller's plan and compare against that.


# Module-private minimal table views. persona-core cannot import the api
# package, so we mirror the api-owned column shapes here (D-07-2 pattern;
# `stores/postgres.py` does the same for memory_chunks). The api-side route
# integration tests double as the contract guard that drift is caught early.
_md = MetaData()

_credits_t = Table(
    "credits",
    _md,
    Column("user_id", Text, primary_key=True),
    # Spec M4 T1a — THE ALLOWANCE BUCKET (monthly-reset, no rollover). NOT the total
    # spendable: total = this + Σ(unexpired ``payg_grants.credits_remaining``). Read the
    # total via :func:`get_balance`; NEVER read this column directly as "the balance".
    # The physical name stays ``balance`` (conceptually ``allowance_balance``) to keep
    # the M3 credits parity tests pristine (they encode this column name; D-M4-rename →
    # Option A). The bucket-aware deduct draws THIS bucket first, then FIFO PAYG lots.
    Column("balance", Integer, nullable=False),
    # Spec M4 T1a (D-M4-R3) — the lazy monthly-reset marker ('YYYY-MM' UTC); declared so
    # the core mirror matches the api-owned schema. Inert until T6 wires the reset.
    Column("allowance_period", Text),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

# Spec M4 T1a (D-M4-R6) — the PAYG "lots" bucket mirror. The core ledger writers draw
# these AFTER the allowance bucket, oldest-expiring first (FIFO). Column names/types
# match the api-owned canonical ``persona_api.db.models.payg_grants``; the api-side
# integration tests are the drift guard (the ``_credit_tx_t`` pattern).
_payg_grants_t = Table(
    "payg_grants",
    _md,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("credits_total", Integer, nullable=False),
    Column("credits_remaining", Integer, nullable=False),
    Column("granted_at", DateTime(timezone=True), nullable=False),
    Column("expires_at", DateTime(timezone=True), nullable=False),
    Column("source_billing_key", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

_credit_tx_t = Table(
    "credit_transactions",
    _md,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("delta", Integer, nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    # Spec M3 (migration 050): the mirror declares the 3 additive columns so the
    # core ledger writers can project them. ``cost_cents`` = true provider cost
    # pre-markup (Float — sub-cent surfaces must not round to 0); ``cost_basis``
    # = provenance (D-M3-12); ``billing_key`` = idempotency anchor (D-M3-R5). The
    # api-side route integration tests stay the drift guard against db/models.
    Column("cost_cents", Float),
    Column("cost_basis", Text),
    Column("billing_key", Text),
)

_conversations_t = Table(
    "conversations",
    _md,
    Column("id", Text, primary_key=True),
    Column("owner_id", Text, nullable=False),
    Column("persona_id", Text, nullable=False),
)

_turn_logs_t = Table(
    # Local minimal view mirrors the api-owned canonical schema at
    # packages/api/src/persona_api/db/models.py:244-272 (v0.1.1: column set
    # restored to include latency_ms / cost_cents / tool_calls / skill_used /
    # history_compacted so list_turn_usage's select() resolves all the
    # fields the /v1/me/usage route consumes).
    "turn_logs",
    _md,
    Column("id", Text, primary_key=True),
    Column("conversation_id", Text, nullable=False),
    Column("turn_index", Integer, nullable=False),
    Column("tier_used", Text, nullable=False),
    Column("model_name", Text, nullable=False),
    Column("provider", Text, nullable=False),
    Column("prompt_tokens", Integer, nullable=False),
    Column("completion_tokens", Integer, nullable=False),
    Column("latency_ms", Float, nullable=False),
    Column("cost_cents", Float, nullable=False),
    # Spec M2 (D-M2-4, migration 046): pricing provenance — the mirror must
    # declare it or ``list_turn_usage``'s select() won't project it and the
    # /v1/me/usage route can't surface it. NULL = legacy pre-M2 row.
    Column("cost_basis", Text),
    Column("tool_calls", Integer, nullable=False),
    Column("skill_used", Text),
    Column("history_compacted", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def ensure_balance(*, rls_engine: Engine, user_id: str) -> int:
    """Ensure the caller's ``credits`` row exists; return the ALLOWANCE bucket.

    Spec M4 T1a: the returned value is the allowance bucket (the ``credits.balance``
    column), NOT the total spendable — internal callers use this only to guarantee
    the row before a bucket-aware draw. The public "what's my balance" surface is
    :func:`get_balance` (allowance + unexpired PAYG). Behaviour is otherwise
    byte-identical to M3 (ensure row, default on first use).
    """
    with rls_engine.begin() as conn:
        row = conn.execute(
            select(_credits_t.c.balance).where(_credits_t.c.user_id == user_id)
        ).first()
        if row is not None:
            return int(row[0])
        conn.execute(insert(_credits_t).values(user_id=user_id, balance=_DEFAULT_BALANCE))
    return _DEFAULT_BALANCE


def _current_total(conn: Connection, *, user_id: str) -> int:
    """Total spendable = allowance bucket + Σ(unexpired PAYG lot remainders).

    Spec M4 T1a (owner Decision 1): the two-bucket balance, read on an OPEN
    transaction (no lock — a plain read). ``credits.balance`` is the allowance
    bucket; expired lots (``expires_at <= now()``) are excluded from the sum.
    """
    allowance = int(
        conn.execute(
            select(_credits_t.c.balance).where(_credits_t.c.user_id == user_id)
        ).scalar_one()
    )
    payg = conn.execute(
        select(func.coalesce(func.sum(_payg_grants_t.c.credits_remaining), 0)).where(
            _payg_grants_t.c.user_id == user_id,
            _payg_grants_t.c.credits_remaining > 0,
            _payg_grants_t.c.expires_at > func.now(),
        )
    ).scalar_one()
    return allowance + int(payg)


def _draw_from_buckets(
    conn: Connection, *, user_id: str, amount: int, allow_partial: bool
) -> tuple[int, int]:
    """Allowance-first then FIFO-PAYG draw on an OPEN transaction (Spec M4 T1a).

    The lock-then-allocate core of the two-bucket deduct (owner Decision 1). Locks
    the ``credits`` row (the allowance bucket) and the caller's unexpired non-empty
    PAYG lots ``FOR UPDATE`` (oldest-expiring first — FIFO), computes
    ``spendable = allowance + Σ lots``, then draws from the allowance bucket FIRST
    and the lots FIFO. Never overdraws (each take is floored by that bucket's own
    remainder); the ``FOR UPDATE`` on the ``credits`` row serialises concurrent
    same-user draws exactly as M3's conditional decrement did (the double-spend
    guarantee is preserved).

    ``allow_partial=False`` (strict): an ``amount`` exceeding ``spendable`` raises
    :class:`CreditsExhaustedError` and writes NOTHING (the caller's transaction
    rolls back). ``allow_partial=True`` (capture): draws ``min(amount, spendable)``,
    floored at 0. Returns ``(taken, new_total)`` where ``new_total`` is the post-draw
    total spendable. **Allowance-only path (no lots) is byte-identical to M3's
    single-counter decrement** — ``spendable == allowance``, one ``credits`` UPDATE,
    same exhaustion raise.
    """
    allowance = int(
        conn.execute(
            select(_credits_t.c.balance).where(_credits_t.c.user_id == user_id).with_for_update()
        ).scalar_one()
    )
    lots = conn.execute(
        select(_payg_grants_t.c.id, _payg_grants_t.c.credits_remaining)
        .where(
            _payg_grants_t.c.user_id == user_id,
            _payg_grants_t.c.credits_remaining > 0,
            _payg_grants_t.c.expires_at > func.now(),
        )
        .order_by(
            _payg_grants_t.c.expires_at.asc(),
            _payg_grants_t.c.granted_at.asc(),
            _payg_grants_t.c.id.asc(),
        )
        .with_for_update()
    ).all()
    payg_total = sum(int(r[1]) for r in lots)
    spendable = allowance + payg_total
    if not allow_partial and amount > spendable:
        # Spec M4 T7a (Tension-5): NO product copy in core — the user-facing upgrade prompt
        # lives at each SURFACE (the api 402 handler's ``CREDITS_EXHAUSTED_DETAIL``; voice's
        # own "insufficient credits"). Core raises the bare typed error + machine context.
        raise CreditsExhaustedError(
            context={"amount": str(amount), "spendable": str(spendable)},
        )
    take = amount if not allow_partial else min(amount, spendable)
    if take <= 0:
        return 0, spendable
    # Allowance bucket first.
    allowance_take = min(allowance, take)
    if allowance_take > 0:
        conn.execute(
            update(_credits_t)
            .where(_credits_t.c.user_id == user_id)
            .values(balance=_credits_t.c.balance - allowance_take, updated_at=text("now()"))
        )
    # Remainder from PAYG lots, FIFO oldest-expiring.
    remainder = take - allowance_take
    for lot_id, lot_remaining in lots:
        if remainder <= 0:
            break
        lot_take = min(int(lot_remaining), remainder)
        conn.execute(
            update(_payg_grants_t)
            .where(_payg_grants_t.c.id == lot_id)
            .values(credits_remaining=_payg_grants_t.c.credits_remaining - lot_take)
        )
        remainder -= lot_take
    return take, spendable - take


def get_balance(*, rls_engine: Engine, user_id: str) -> int:
    """Current TOTAL spendable = allowance bucket + unexpired PAYG (Spec M4 T1a).

    The enforced total accessor (owner Decision 1): never read ``credits.balance``
    directly as "the balance" — it is only the allowance bucket. Creates the default
    ``credits`` row if absent.
    """
    ensure_balance(rls_engine=rls_engine, user_id=user_id)
    with rls_engine.begin() as conn:
        return _current_total(conn, user_id=user_id)


def wallet_snapshot(*, rls_engine: Engine, user_id: str) -> dict[str, object]:
    """The two-bucket wallet, read-only (Spec M4, T8): allowance + PAYG lots + total.

    One consistent read (a single transaction, no locks — CQS: pure read) of the
    caller's wallet: the allowance bucket (``credits.balance`` + its
    ``allowance_period`` month stamp) and every LIVE PAYG lot (unexpired,
    ``credits_remaining > 0``) in the FIFO spend order (:func:`_draw_from_buckets`'s
    oldest-expiring-first), plus the derived total. Creates the default ``credits``
    row if absent (mirrors :func:`get_balance`).

    Returns keys: ``allowance_balance`` (int), ``allowance_period`` (str | None),
    ``payg_lots`` (list of ``{credits_remaining, credits_total, expires_at}`` dicts,
    FIFO order), ``total_balance`` (int).
    """
    ensure_balance(rls_engine=rls_engine, user_id=user_id)
    with rls_engine.begin() as conn:
        row = conn.execute(
            select(_credits_t.c.balance, _credits_t.c.allowance_period).where(
                _credits_t.c.user_id == user_id
            )
        ).one()
        allowance = int(row[0])
        period = str(row[1]) if row[1] is not None else None
        lots = conn.execute(
            select(
                _payg_grants_t.c.credits_remaining,
                _payg_grants_t.c.credits_total,
                _payg_grants_t.c.expires_at,
            )
            .where(
                _payg_grants_t.c.user_id == user_id,
                _payg_grants_t.c.credits_remaining > 0,
                _payg_grants_t.c.expires_at > func.now(),
            )
            .order_by(
                _payg_grants_t.c.expires_at.asc(),
                _payg_grants_t.c.granted_at.asc(),
                _payg_grants_t.c.id.asc(),
            )
        ).all()
    lot_dicts: list[dict[str, object]] = [
        {
            "credits_remaining": int(lot[0]),
            "credits_total": int(lot[1]),
            "expires_at": lot[2],
        }
        for lot in lots
    ]
    return {
        "allowance_balance": allowance,
        "allowance_period": period,
        "payg_lots": lot_dicts,
        "total_balance": allowance + sum(int(lot[0]) for lot in lots),
    }


def require_credits(*, rls_engine: Engine, user_id: str) -> int:
    """Pre-flight credit check: raise :class:`CreditsExhaustedError` (→ 402) if
    the caller has no credits left. Returns the total spendable balance.

    Called at the **top** of every generation endpoint — chat, agentic runs,
    persona authoring and refinement — *before* the SSE stream / run starts.
    Raising inside the SSE generator yields the spec-08 "response already
    started" trap, so the pre-flight gate is the right place (D-11-12).
    The post-success ``deduct`` (D-08-6) is unchanged.

    Spec M4 T1a: the gate now checks the TWO-BUCKET total (allowance + unexpired
    PAYG) via :func:`get_balance`, so a user with a spent allowance but live PAYG
    lots passes.
    """
    balance = get_balance(rls_engine=rls_engine, user_id=user_id)
    if balance <= 0:
        # Spec M4 T7a (Tension-5): no product copy in core — the surface owns the prompt
        # (api 402 handler / voice handler). Core raises the bare typed error + context.
        raise CreditsExhaustedError(context={"balance": str(balance)})
    return balance


# --- Spec R7 per-UTC-day spend cap (R7-D-1/2/3) --------------------------------
#
# The primary denial-of-wallet guard: a per-user counter of credits spent in the
# current UTC calendar day, enforced with the SAME conditional-before-booking
# discipline R2 used for the credit floor — so two concurrent near-cap requests
# can't both pass (the TOCTOU class R2 closed).
#
# ``utc_day`` is computed IN SQL as ``(now() AT TIME ZONE 'UTC')::date``. ``now()``
# is the transaction timestamp (fixed for the life of a transaction), so the
# ensure-row INSERT and the conditional UPDATE below target the SAME row within
# one ``begin()`` — and the day boundary is DB-authoritative, immune to app/DB
# clock skew (R7-D-2 fixed UTC-day reset).
_DAY = "(now() AT TIME ZONE 'UTC')::date"

# 1. Ensure the counter row exists at 0 (race-safe: ``ON CONFLICT DO NOTHING``).
_ENSURE_DAY_ROW_SQL = text(
    f"INSERT INTO day_spend (user_id, utc_day, spent) VALUES (:uid, {_DAY}, 0) "
    f"ON CONFLICT (user_id, utc_day) DO NOTHING"
)
# 2. The R2 conditional-before-booking write: increment ONLY ``WHERE spent + cost
#    <= cap``. A booking that would breach the cap matches no row → RETURNING
#    yields nothing → refuse. Race-safe under READ COMMITTED: Postgres re-evaluates
#    the WHERE against the latest row version after taking the row write-lock, so
#    the second of two concurrent near-cap bookings sees the first's increment and
#    is rejected. (Diverges from the literal R7-D-3 ``INSERT … ON CONFLICT DO
#    UPDATE … WHERE`` because that leaves the initial no-conflict INSERT UNGUARDED
#    — a first-of-day op that alone exceeds the cap would book over-cap and fail
#    OPEN. The ensure-row + guarded-UPDATE shape closes that money-guard hole:
#    ``spent`` starts at 0 and the UPDATE's WHERE covers ``0 + cost <= cap`` too.)
_BOOK_DAY_SPEND_SQL = text(
    f"UPDATE day_spend SET spent = spent + :cost, updated_at = now() "
    f"WHERE user_id = :uid AND utc_day = {_DAY} AND spent + :cost <= :cap "
    f"RETURNING spent"
)
# The current day's spend, for the refusal's error context (informational only —
# NOT part of the guard; the conditional UPDATE above is the single point of truth).
_CURRENT_DAY_SPENT_SQL = text(
    f"SELECT spent FROM day_spend WHERE user_id = :uid AND utc_day = {_DAY}"
)


def _book_day_spend_conn(conn: Connection, *, user_id: str, cost: int, cap: int) -> int | None:
    """Book ``cost`` against the user's UTC-day counter on an OPEN transaction.

    Returns the new day-spend total on success, or ``None`` when the booking would
    breach ``cap`` (over-cap ⇒ refuse). The two statements run on the caller's
    ``conn`` so a day-cap booking composes ATOMICALLY with the credit decrement in
    :func:`deduct` (both roll back together on any refusal — fail-closed).

    ``cap <= 0`` means *unlimited* (the config default 0 = uncapped, community
    no-ops entirely) and ``cost <= 0`` is nothing to book: both short-circuit to
    "allowed" (a non-``None`` sentinel) WITHOUT writing, so an uncapped deployment
    is byte-identical to pre-R7 behaviour.
    """
    if cap <= 0 or cost <= 0:
        return 0
    conn.execute(_ENSURE_DAY_ROW_SQL, {"uid": user_id})
    return conn.execute(
        _BOOK_DAY_SPEND_SQL, {"uid": user_id, "cost": cost, "cap": cap}
    ).scalar_one_or_none()


def _current_day_spent(conn: Connection, *, user_id: str) -> int:
    row = conn.execute(_CURRENT_DAY_SPENT_SQL, {"uid": user_id}).scalar_one_or_none()
    return int(row) if row is not None else 0


def _next_utc_midnight_epoch() -> int:
    """Epoch seconds of the next UTC midnight — when the day counter resets (R7-D-5).

    The API edge turns this into ``Retry-After`` (seconds-to-reset), the honest
    "capped for today, back tomorrow" hint. A hair of app/DB clock skew vs the
    DB-authoritative ``utc_day`` boundary is immaterial for a back-off hint.
    """
    now = datetime.now(UTC)
    midnight = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def book_day_spend(*, rls_engine: Engine, user_id: str, cost: int, cap: int) -> bool:
    """Atomically book ``cost`` against the user's per-UTC-day spend cap (R7-D-3).

    The standalone conditional-write surface — the load-bearing race-safe primitive
    the TOCTOU proof (T3) fires N parallel requests at. Returns ``True`` if the
    booking landed within ``cap``, ``False`` if it would breach the cap (over-cap ⇒
    the caller refuses; :func:`deduct` composes this INTO its own transaction so the
    day-cap and the credit decrement move atomically).

    Runs in its own ``rls_engine.begin()`` on a fresh pooled connection (R2-D-7 — no
    new lock scope, no deadlock with a caller's transaction). ``cap <= 0`` (unlimited)
    or ``cost <= 0`` allow without writing.
    """
    if cap <= 0 or cost <= 0:
        return True
    with rls_engine.begin() as conn:
        return _book_day_spend_conn(conn, user_id=user_id, cost=cost, cap=cap) is not None


def deduct(
    *,
    rls_engine: Engine,
    user_id: str,
    amount: int,
    reason: str,
    daily_cap: int = 0,
    cost_cents: float | None = None,
    cost_basis: str | None = None,
) -> int:
    """Deduct ``amount`` credits and record a transaction. Returns the new balance.

    Spec M3 (D-M3-12): ``cost_cents`` (true provider cost pre-markup) and
    ``cost_basis`` (provenance) are recorded on the ledger row when supplied.
    Both default ``None`` — the pre-M3 call shape writes them as ``NULL``,
    byte-identical to before (the columns are additive + nullable). The
    ``MeteredBilling`` seam passes them; direct M2/legacy callers do not.

    Spec R2 R2-D-3 (F-04): the decrement is **conditional and atomic** — the
    ``UPDATE`` carries ``WHERE balance >= :amount`` so a decrement that would
    overdraw matches no row and is rejected (``RETURNING`` yields nothing). This
    closes the double-spend race: the pre-flight :func:`require_credits` gate
    runs in a separate transaction, so two concurrent turns could each pass it at
    balance=1; the floor on the decrement itself is the single point of truth.

    Spec R7 (R7-D-1/3): when ``daily_cap > 0`` the deduct ALSO books ``amount``
    against the per-UTC-day spend counter — in the SAME transaction as the credit
    decrement, so the day-cap and the floor move ATOMICALLY. The day-cap is booked
    FIRST (:func:`_book_day_spend_conn`); an over-cap booking raises
    :class:`DailySpendCapExceededError` (→ 429) BEFORE the balance is touched, and
    because it shares the transaction the whole thing rolls back — no credit spent,
    no day-spend booked (FAIL-CLOSED, R7-D-5). ``daily_cap <= 0`` is uncapped and
    leaves the pre-R7 behaviour byte-identical.

    On insufficient balance this raises :class:`CreditsExhaustedError` (→ 402)
    and writes **no** ledger row (CQS: a failed decrement records nothing). The
    deduct runs in its own transaction on a fresh pooled connection (R2-D-7), so
    the conditional predicate adds no new lock scope and cannot deadlock with the
    caller's transaction. A DB-level ``CHECK (balance >= 0)`` constraint
    (migration 024) is the belt-and-braces durable guard.
    """
    ensure_balance(rls_engine=rls_engine, user_id=user_id)
    with rls_engine.begin() as conn:
        # R7: book the day-cap FIRST, atomically-with the two-bucket draw below.
        # Over-cap ⇒ raise before any spend is booked; the ``with`` rolls back the txn.
        if daily_cap > 0 and amount > 0:
            booked = _book_day_spend_conn(conn, user_id=user_id, cost=amount, cap=daily_cap)
            if booked is None:
                spent = _current_day_spent(conn, user_id=user_id)
                raise DailySpendCapExceededError(
                    "Daily spend cap reached. This resets at UTC midnight.",
                    context={
                        "cap": str(daily_cap),
                        "spent": str(spent),
                        "requested_cost": str(amount),
                        "reset_epoch": str(_next_utc_midnight_epoch()),
                    },
                )
        # Spec M4 T1a: allowance-first then FIFO-PAYG draw (strict). Unaffordable ⇒
        # ``CreditsExhaustedError``, writes nothing (the ``with`` rolls back the
        # day-spend booked above too). The allowance-only path (no PAYG lots) is
        # byte-identical to M3's conditional decrement — same exhaustion raise, same
        # single-counter move (test_credits_double_spend is the regression floor).
        _taken, new_total = _draw_from_buckets(
            conn, user_id=user_id, amount=amount, allow_partial=False
        )
        conn.execute(
            insert(_credit_tx_t).values(
                id=f"ctx_{uuid.uuid4().hex}",
                user_id=user_id,
                delta=-amount,
                reason=reason,
                cost_cents=cost_cents,
                cost_basis=cost_basis,
            )
        )
    return int(new_total)


def capture_up_to(
    *,
    rls_engine: Engine,
    user_id: str,
    amount: int,
    reason: str,
    daily_cap: int = 0,
    cost_cents: float | None = None,
    cost_basis: str | None = None,
) -> tuple[int, int]:
    """Charge ``min(amount, balance)`` — PARTIAL when the balance is short (Spec M2 review, C1).

    Spec M3 (D-M3-12): records ``cost_cents`` / ``cost_basis`` on the ledger row
    when supplied (default ``None`` → ``NULL``, byte-identical to pre-M3).

    The opt-in sibling of :func:`deduct`, for ONE caller only: the chat-turn
    worker's post-success billing (``persona_api.background.chat_turn_worker``).
    Every other metered caller (authoring, imagegen, sandbox code execution)
    keeps calling :func:`deduct` — its all-or-nothing semantics are UNCHANGED
    by this function's existence.

    The bug this closes: ``deduct``'s conditional decrement is all-or-nothing
    (``WHERE balance >= :amount``), so a charge that exceeds the remaining
    balance deducts NOTHING and raises — repeatedly, forever, once the balance
    sits below the turn's cost. A completed (already-rendered, already-paid-for
    upstream) turn would never get billed again: the balance never reaches 0,
    so the pre-flight ``require_credits`` gate (``balance > 0``) keeps passing
    turns that can no longer be charged their true cost.

    This charges ``captured = min(amount, spendable)`` via the two-bucket
    :func:`_draw_from_buckets` (``allow_partial=True``) that floors at 0 — a
    short charge is captured in full up to what remains, rather than rejected
    outright. The ledger row records the CAPTURED delta (never the requested
    ``amount``); when ``captured < amount`` the ``reason`` is suffixed
    ``":shortfall"`` so the audit trail is honest about the underpayment —
    e.g. ``"chat_turn:estimate_static"`` becomes
    ``"chat_turn:estimate_static:shortfall"``. A full capture (``captured ==
    amount``) keeps the bare ``reason``, byte-identical to what ``deduct``
    would have written. ``amount <= 0`` captures nothing (a no-op, no ledger
    row) and returns the current balance.

    Spec R7 (R7-D-1/3): when ``daily_cap > 0`` the CAPTURED amount (never the
    requested one) is booked against the per-UTC-day spend counter, in the
    SAME transaction as the decrement — so an over-cap booking raises
    :class:`DailySpendCapExceededError` and rolls back the WHOLE capture
    (fail-closed, the identical discipline :func:`deduct` already uses); the
    chat-turn worker treats this as "billing skipped this turn, no exception
    escapes" (spec M2 review I3), never a retry loop.

    Returns ``(captured, new_balance)``.
    """
    ensure_balance(rls_engine=rls_engine, user_id=user_id)
    if amount <= 0:
        return 0, get_balance(rls_engine=rls_engine, user_id=user_id)
    with rls_engine.begin() as conn:
        # Spec M4 T1a: allowance-first then FIFO-PAYG partial draw, floored at 0.
        # The allowance-only path is byte-identical to M3's ``LEAST(balance, :amount)``
        # capture (test_credits_capture_up_to is the regression floor).
        captured, new_total = _draw_from_buckets(
            conn, user_id=user_id, amount=amount, allow_partial=True
        )
        if daily_cap > 0 and captured > 0:
            booked = _book_day_spend_conn(conn, user_id=user_id, cost=captured, cap=daily_cap)
            if booked is None:
                spent = _current_day_spent(conn, user_id=user_id)
                raise DailySpendCapExceededError(
                    "Daily spend cap reached. This resets at UTC midnight.",
                    context={
                        "cap": str(daily_cap),
                        "spent": str(spent),
                        "requested_cost": str(captured),
                        "reset_epoch": str(_next_utc_midnight_epoch()),
                    },
                )
        if captured > 0:
            final_reason = reason if captured >= amount else f"{reason}:shortfall"
            conn.execute(
                insert(_credit_tx_t).values(
                    id=f"ctx_{uuid.uuid4().hex}",
                    user_id=user_id,
                    delta=-captured,
                    reason=final_reason,
                    cost_cents=cost_cents,
                    cost_basis=cost_basis,
                )
            )
    return captured, new_total


def deduct_idempotent(
    *,
    rls_engine: Engine,
    user_id: str,
    amount: int,
    reason: str,
    billing_key: str,
    daily_cap: int = 0,
    cost_cents: float | None = None,
    cost_basis: str | None = None,
) -> int:
    """Idempotent all-or-nothing ``deduct`` keyed on ``billing_key`` (Spec M3, D-M3-R5).

    The strict sibling of :func:`deduct` for at-least-once callers (the T4b/T5
    background + task deducts). Insert-first idempotency gate: the ledger row is
    inserted with ``ON CONFLICT (billing_key) DO NOTHING RETURNING id`` **before**
    the balance mutation; a re-delivered op (same key) inserts nothing → returns
    the current balance without touching it (no double-charge). A first delivery
    inserts the row and then applies the conditional decrement — **in the SAME
    transaction**, so an exhausted-balance raise rolls the ledger insert back too
    (no orphan row), and an over-``daily_cap`` booking rolls back everything
    (fail-closed, the discipline :func:`deduct` already uses).

    Returns the new balance (or the unchanged current balance on a re-delivery
    no-op). Raises :class:`CreditsExhaustedError` when a FIRST delivery cannot be
    afforded, :class:`DailySpendCapExceededError` when it would breach the cap.
    """
    ensure_balance(rls_engine=rls_engine, user_id=user_id)
    with rls_engine.begin() as conn:
        # Insert-first gate: consume the billing_key or detect the re-delivery.
        inserted_id = conn.execute(
            pg_insert(_credit_tx_t)
            .values(
                id=f"ctx_{uuid.uuid4().hex}",
                user_id=user_id,
                delta=-amount,
                reason=reason,
                cost_cents=cost_cents,
                cost_basis=cost_basis,
                billing_key=billing_key,
            )
            .on_conflict_do_nothing(
                index_elements=["billing_key"],
                index_where=_credit_tx_t.c.billing_key.isnot(None),
            )
            .returning(_credit_tx_t.c.id)
        ).scalar_one_or_none()
        if inserted_id is None:
            # Already billed for this key — skip the draw entirely; return the total.
            return _current_total(conn, user_id=user_id)
        # Day-cap FIRST, atomically-with the two-bucket draw (over-cap ⇒ raise ⇒ the
        # whole txn, incl. the insert above, rolls back).
        if daily_cap > 0 and amount > 0:
            booked = _book_day_spend_conn(conn, user_id=user_id, cost=amount, cap=daily_cap)
            if booked is None:
                spent = _current_day_spent(conn, user_id=user_id)
                raise DailySpendCapExceededError(
                    "Daily spend cap reached. This resets at UTC midnight.",
                    context={
                        "cap": str(daily_cap),
                        "spent": str(spent),
                        "requested_cost": str(amount),
                        "reset_epoch": str(_next_utc_midnight_epoch()),
                    },
                )
        # Spec M4 T1a: allowance-first then FIFO-PAYG draw (strict). An unaffordable
        # FIRST delivery ⇒ ``CreditsExhaustedError``; the ``with`` rolls back the ledger
        # insert (no orphan row) + any day-spend booked above.
        _taken, new_total = _draw_from_buckets(
            conn, user_id=user_id, amount=amount, allow_partial=False
        )
    return int(new_total)


def capture_up_to_idempotent(
    *,
    rls_engine: Engine,
    user_id: str,
    amount: int,
    reason: str,
    billing_key: str,
    daily_cap: int = 0,
    cost_cents: float | None = None,
    cost_basis: str | None = None,
) -> tuple[int, int]:
    """Idempotent partial-capture keyed on ``billing_key`` (Spec M3, D-M3-R5).

    The floored sibling of :func:`capture_up_to` for at-least-once post-success /
    incremental callers (T4b/T5 owner deducts). Because the captured amount is
    only known after the balance is locked, the idempotency gate is a **claim
    row** (``delta=0``) inserted with ``ON CONFLICT (billing_key) DO NOTHING
    RETURNING id``; a re-delivery inserts nothing → returns ``(0, balance)`` with
    no capture. A first delivery then runs the atomic ``capture_up_to`` decrement
    and **updates the same claim row** to the captured delta (+ ``:shortfall``
    suffix when partial) — all in ONE transaction, so an over-``daily_cap``
    booking rolls back the claim + the capture together (fail-closed).

    Note (intentional divergence from :func:`capture_up_to`): a first delivery
    that captures ``0`` (balance already 0) still leaves the ``delta=0`` claim
    row — the ``billing_key`` must be durably consumed so a later retry after a
    top-up does not re-bill an op that already ran (exactly-once semantics).

    Returns ``(captured, new_balance)``.
    """
    ensure_balance(rls_engine=rls_engine, user_id=user_id)
    with rls_engine.begin() as conn:
        claim_id = conn.execute(
            pg_insert(_credit_tx_t)
            .values(
                id=f"ctx_{uuid.uuid4().hex}",
                user_id=user_id,
                delta=0,
                reason=reason,
                cost_cents=cost_cents,
                cost_basis=cost_basis,
                billing_key=billing_key,
            )
            .on_conflict_do_nothing(
                index_elements=["billing_key"],
                index_where=_credit_tx_t.c.billing_key.isnot(None),
            )
            .returning(_credit_tx_t.c.id)
        ).scalar_one_or_none()
        if claim_id is None:
            # Already billed for this key — no capture. Return the two-bucket total.
            return 0, _current_total(conn, user_id=user_id)
        if amount <= 0:
            # Nothing to capture; the claim (delta 0) durably consumes the key.
            return 0, _current_total(conn, user_id=user_id)
        # Spec M4 T1a: allowance-first then FIFO-PAYG partial draw, floored at 0.
        captured, new_total = _draw_from_buckets(
            conn, user_id=user_id, amount=amount, allow_partial=True
        )
        if daily_cap > 0 and captured > 0:
            booked = _book_day_spend_conn(conn, user_id=user_id, cost=captured, cap=daily_cap)
            if booked is None:
                spent = _current_day_spent(conn, user_id=user_id)
                raise DailySpendCapExceededError(
                    "Daily spend cap reached. This resets at UTC midnight.",
                    context={
                        "cap": str(daily_cap),
                        "spent": str(spent),
                        "requested_cost": str(captured),
                        "reset_epoch": str(_next_utc_midnight_epoch()),
                    },
                )
        final_reason = reason if captured >= amount else f"{reason}:shortfall"
        conn.execute(
            update(_credit_tx_t)
            .where(_credit_tx_t.c.id == claim_id)
            .values(delta=-captured, reason=final_reason)
        )
    return captured, new_total


def grant_idempotent(
    *,
    rls_engine: Engine,
    user_id: str,
    amount: int,
    reason: str,
    billing_key: str,
    cost_cents: float | None = None,
    cost_basis: str | None = None,
) -> int:
    """Idempotent positive-delta grant to the ALLOWANCE bucket (Spec M4, T1b).

    The exactly-once grant primitive the Stripe webhook rides — a subscription
    renewal (``invoice.paid`` → ``grant_subscription``) or a monthly free refresh
    (``grant_free_refresh``) credits the **allowance bucket** (``credits.balance``).
    The mirror-image of :func:`deduct_idempotent`: insert-first ``billing_key`` gate
    (``pg_insert(...).on_conflict_do_nothing(billing_key) RETURNING id``), then a
    POSITIVE ``UPDATE credits SET balance = balance + :amount`` in the SAME
    transaction. Because Stripe delivers at-least-once, a re-delivered event carries
    the same ``billing_key`` → the insert is a no-op → the balance is left untouched
    (grants exactly once, no double-grant). ``billing_key`` = the Stripe event /
    invoice id.

    The PAYG **lot** grant (``topup_payg`` → a ``payg_grants`` row, idempotent on
    T1a's ``UNIQUE(source_billing_key)``) is a SEPARATE T4 primitive — this grants
    only the allowance counter.

    Returns the new TOTAL spendable (allowance + unexpired PAYG) after the grant, or
    the unchanged current total on a re-delivery no-op.
    """
    ensure_balance(rls_engine=rls_engine, user_id=user_id)
    with rls_engine.begin() as conn:
        # Insert-first gate: consume the billing_key or detect the re-delivery. The
        # positive ``delta`` is the grant (the mirror of deduct_idempotent's ``-amount``).
        inserted_id = conn.execute(
            pg_insert(_credit_tx_t)
            .values(
                id=f"ctx_{uuid.uuid4().hex}",
                user_id=user_id,
                delta=amount,
                reason=reason,
                cost_cents=cost_cents,
                cost_basis=cost_basis,
                billing_key=billing_key,
            )
            .on_conflict_do_nothing(
                index_elements=["billing_key"],
                index_where=_credit_tx_t.c.billing_key.isnot(None),
            )
            .returning(_credit_tx_t.c.id)
        ).scalar_one_or_none()
        if inserted_id is None:
            # Already granted for this key — grant nothing; return the current total.
            return _current_total(conn, user_id=user_id)
        # First delivery: credit the ALLOWANCE bucket in the SAME transaction, so a
        # rollback would undo the ledger insert too (no orphan grant row).
        conn.execute(
            update(_credits_t)
            .where(_credits_t.c.user_id == user_id)
            .values(balance=_credits_t.c.balance + amount, updated_at=text("now()"))
        )
        return _current_total(conn, user_id=user_id)


def reset_allowance_idempotent(
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
    """Idempotently OVERWRITE the allowance bucket to ``allowance`` (Spec M4, T3b).

    The subscription-renewal reset (owner Decision 1: **overwrite, no rollover** — NOT
    a top-up). Keyed on the Stripe invoice id: the insert-first ``ON CONFLICT
    (billing_key) DO NOTHING`` gate makes a re-delivered ``invoice.paid`` reset **exactly
    once**. This gate is load-bearing — a *bare* overwrite is NOT idempotent: if the user
    spends part of the allowance between two deliveries, a second overwrite would
    re-inflate the balance (wiping the spend, a double-grant). With the gate, only the
    FIRST delivery overwrites.

    On the first delivery, in ONE transaction: lock + read the old allowance, claim the
    ``billing_key`` (a ledger row whose ``delta = allowance - old_balance`` — the honest
    net change, which may be negative), then **SET balance = allowance** (overwrite the
    allowance bucket; PAYG lots are untouched) and stamp ``allowance_period`` (the UTC
    month, so T6's lazy monthly free-reset sees a matching period and no-ops for a paid
    user). A re-delivery inserts nothing → returns the current total without overwriting.

    Returns the new TOTAL spendable (allowance + unexpired PAYG).
    """
    ensure_balance(rls_engine=rls_engine, user_id=user_id)
    with rls_engine.begin() as conn:
        # Lock the allowance row + read the pre-reset value (for the honest ledger delta).
        old_allowance = int(
            conn.execute(
                select(_credits_t.c.balance)
                .where(_credits_t.c.user_id == user_id)
                .with_for_update()
            ).scalar_one()
        )
        # Insert-first gate: claim the invoice's billing_key or detect the re-delivery.
        inserted_id = conn.execute(
            pg_insert(_credit_tx_t)
            .values(
                id=f"ctx_{uuid.uuid4().hex}",
                user_id=user_id,
                delta=allowance - old_allowance,
                reason=reason,
                cost_cents=cost_cents,
                cost_basis=cost_basis,
                billing_key=billing_key,
            )
            .on_conflict_do_nothing(
                index_elements=["billing_key"],
                index_where=_credit_tx_t.c.billing_key.isnot(None),
            )
            .returning(_credit_tx_t.c.id)
        ).scalar_one_or_none()
        if inserted_id is None:
            # Already reset for this invoice — NO overwrite (exactly-once; the spend the
            # user made after the first delivery is preserved).
            return _current_total(conn, user_id=user_id)
        # First delivery: OVERWRITE the allowance bucket + stamp the period.
        conn.execute(
            update(_credits_t)
            .where(_credits_t.c.user_id == user_id)
            .values(balance=allowance, allowance_period=allowance_period, updated_at=text("now()"))
        )
        return _current_total(conn, user_id=user_id)


# --- Spec M4 T6: lazy monthly free-allowance refresh (on-access self-heal) ----
#
# DB-authoritative UTC month key ``'YYYY-MM'`` — the SAME DB-clock discipline as the R7
# day counter (``now()`` is the transaction timestamp, so the boundary is immune to
# app/DB skew; no cron/leader needed — a dormant free user self-heals on their next touch).
_UTC_MONTH = "to_char((now() AT TIME ZONE 'UTC'), 'YYYY-MM')"

# The guarded conditional overwrite (mirrors the R7 conditional-write discipline): reset the
# allowance bucket to the free plan's allowance + stamp the current month ONLY when BOTH
#   (a) the stored period differs from the current month — ``IS DISTINCT FROM`` treats a
#       NULL/never-stamped period as different, so a fresh row refreshes on first access; and
#   (b) the user is NOT on a paid plan — no ``subscription`` row with ``plan_code <> 'free'``.
#       An ABSENT subscription row = free (consistent with T5c) and DOES get the refresh; a
#       paid ``plus``/``pro`` row is NEVER touched (its allowance resets on ``invoice.paid``
#       via :func:`reset_allowance_idempotent`, which stamps the period — so even a free→paid
#       race converges: whichever stamps the current month first, the other's guard no-ops).
# Re-run in the same month ⇒ the WHERE matches no row ⇒ no-op (idempotent-per-period). The
# PAYG lots are a SEPARATE bucket, untouched. RLS scopes both the UPDATE and the subquery to
# the caller (``:uid`` is the current user), so no cross-tenant read/write is possible.
_REFRESH_FREE_ALLOWANCE_SQL = text(
    "UPDATE credits SET balance = :allowance, "
    f"allowance_period = {_UTC_MONTH}, updated_at = now() "
    f"WHERE user_id = :uid AND allowance_period IS DISTINCT FROM {_UTC_MONTH} "
    "AND NOT EXISTS ("
    "SELECT 1 FROM subscription s WHERE s.user_id = :uid AND s.plan_code <> 'free'"
    ") RETURNING balance"
)


def refresh_free_allowance_lazy(*, rls_engine: Engine, user_id: str) -> bool:
    """Lazily reset a FREE user's monthly allowance on access (Spec M4, T6).

    The cron-free monthly free-tier refresh: overwrite the allowance bucket to the free
    plan's ``included_allowance_credits`` (the plans catalog — 300 = $3, owner-locked) and
    stamp the current UTC month, but ONLY on a free user's FIRST access in a new month (the
    guarded UPDATE in :data:`_REFRESH_FREE_ALLOWANCE_SQL`). A paid user is never touched; a
    re-access in the same month is a no-op. NOT a top-up — an OVERWRITE (no rollover, owner
    Decision 1); the PAYG lots are a separate bucket and are untouched.

    Called from the cloud credits POLICY layer at the top of every metered access
    (``require_credits`` / ``get_balance`` / the ``deduct``/``capture`` methods), so a free
    user's balance is always the current month's allowance without a scheduler — and the
    100_000 seed (:data:`_DEFAULT_BALANCE`) is corrected to $3 before the first spend. The
    community ``UnlimitedCreditsPolicy`` never calls this (edition-gated: no plans, no reset).

    Returns ``True`` if a reset landed (a free user's first access this UTC month), else
    ``False`` (a no-op: already refreshed this month, or a paid user).
    """
    ensure_balance(rls_engine=rls_engine, user_id=user_id)  # guarantee the row before the guard
    allowance = default_plan().included_allowance_credits
    with rls_engine.begin() as conn:
        landed = conn.execute(
            _REFRESH_FREE_ALLOWANCE_SQL, {"uid": user_id, "allowance": allowance}
        ).scalar_one_or_none()
    return landed is not None


def grant_payg_lot_idempotent(
    *,
    rls_engine: Engine,
    user_id: str,
    credit_amount: int,
    reason: str,
    source_billing_key: str,
    cost_cents: float | None = None,
    cost_basis: str | None = None,
) -> int:
    """Idempotently grant a PAYG lot with a 12-month expiry (Spec M4, T4a).

    A one-time pack purchase (``payment_intent.succeeded``) grants a ``payg_grants`` lot —
    the SEPARATE PAYG bucket, so this NEVER touches the allowance (``credits.balance``).
    Idempotency rides T1a's ``UNIQUE(source_billing_key)``: the insert-first ``ON CONFLICT
    DO NOTHING`` on the Stripe payment_intent id makes a re-delivered PI grant exactly ONE
    lot. ``credits_total = credits_remaining = credits`` (``$X`` buys ``X*100`` credits,
    1:1); ``expires_at = now() + 12 months`` (DB-authoritative). On a first delivery an
    audit ledger row is also written (``delta = +credits``, basis ``topup_payg``) so the
    purchase shows in ``/v1/me/*/ledger``; the running total is still computed from the
    lots, not the ledger. Returns the new TOTAL spendable.
    """
    ensure_balance(rls_engine=rls_engine, user_id=user_id)
    with rls_engine.begin() as conn:
        # Insert-first gate on the lot's UNIQUE source_billing_key (the PI id).
        lot_id = conn.execute(
            pg_insert(_payg_grants_t)
            .values(
                id=f"payg_{uuid.uuid4().hex}",
                user_id=user_id,
                credits_total=credit_amount,
                credits_remaining=credit_amount,
                expires_at=text("now() + interval '12 months'"),
                source_billing_key=source_billing_key,
            )
            .on_conflict_do_nothing(index_elements=["source_billing_key"])
            .returning(_payg_grants_t.c.id)
        ).scalar_one_or_none()
        if lot_id is None:
            # Already granted for this payment_intent — exactly one lot.
            return _current_total(conn, user_id=user_id)
        # Audit ledger row (belt-and-braces idempotent on billing_key too).
        conn.execute(
            pg_insert(_credit_tx_t)
            .values(
                id=f"ctx_{uuid.uuid4().hex}",
                user_id=user_id,
                delta=credit_amount,
                reason=reason,
                cost_cents=cost_cents,
                cost_basis=cost_basis,
                billing_key=source_billing_key,
            )
            .on_conflict_do_nothing(
                index_elements=["billing_key"],
                index_where=_credit_tx_t.c.billing_key.isnot(None),
            )
        )
        return _current_total(conn, user_id=user_id)


def refund(
    *,
    rls_engine: Engine,
    user_id: str,
    amount: int,
    reason: str,
    cost_cents: float | None = None,
    cost_basis: str | None = None,
) -> int:
    """Refund ``amount`` credits via a reverse-deduct ledger entry. Returns the new balance.

    Spec M3 (D-M3-12): records ``cost_cents`` / ``cost_basis`` when supplied
    (default ``None`` → ``NULL``, byte-identical to pre-M3) — the image true-up
    overage refund (T3) carries the basis of the refunded charge.

    Pattern (a) per D-15-X-credit-flow-semantics (spec 15 T13): writes
    ``INSERT INTO credit_transactions (delta=+amount, reason=...)`` and runs
    ``UPDATE credits SET balance = balance + amount`` in a single
    ``rls_engine.begin()`` transaction so the ledger and the running balance
    move atomically. Schema-compatible with the existing ``credit_transactions``
    table (``delta`` is ``Integer, nullable=False`` with no ``CheckConstraint``,
    so positive deltas are physically allowed — research §0.1); no Alembic
    migration required.

    Composed by ``persona_api.imagegen.service.generate`` (T15) on provider
    failure after the pre-deduct gate has fired, so a denial-of-wallet attacker
    cannot burn credits with parallel-fire failed generations
    (D-15-X-pre-deduct-credits; T17 is the binary proof). ``amount = 0`` is a
    no-op — no ledger row, no balance change, returns the current balance.
    """
    if amount == 0:
        return get_balance(rls_engine=rls_engine, user_id=user_id)
    ensure_balance(rls_engine=rls_engine, user_id=user_id)
    with rls_engine.begin() as conn:
        # Spec M4 T1a (D-M4-refund→allowance): a refund credits the ALLOWANCE bucket
        # (``credits.balance``), never a PAYG lot. Documented edge: when the original
        # charge drew from PAYG (the allowance was empty), refunding to the allowance
        # converts a 12-month PAYG credit into a monthly-reset allowance credit — a
        # deliberate, approved choice. Refund magnitude is small (the image true-up
        # overage, a few credits seconds after the charge, which drew allowance-first
        # anyway → an exact reversal in the common case); per-charge source-lot tracking
        # is not worth it (YAGNI).
        conn.execute(
            update(_credits_t)
            .where(_credits_t.c.user_id == user_id)
            .values(balance=_credits_t.c.balance + amount, updated_at=text("now()"))
        )
        conn.execute(
            insert(_credit_tx_t).values(
                id=f"ctx_{uuid.uuid4().hex}",
                user_id=user_id,
                delta=amount,
                reason=reason,
                cost_cents=cost_cents,
                cost_basis=cost_basis,
            )
        )
        new_total = _current_total(conn, user_id=user_id)
    return int(new_total)


def list_usage(
    *, rls_engine: Engine, user_id: str, limit: int, offset: int
) -> list[dict[str, object]]:
    """The user's credit-transaction log (paginated)."""
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(
                select(_credit_tx_t)
                .where(_credit_tx_t.c.user_id == user_id)
                .order_by(_credit_tx_t.c.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


def list_turn_usage(*, rls_engine: Engine, limit: int, offset: int) -> list[dict[str, object]]:
    """Per-turn token usage (§5.5) — turn_logs joined to the caller's
    conversations (RLS-scoped via conversations.owner_id), with the persona id."""
    with rls_engine.begin() as conn:
        rows = (
            conn.execute(
                select(
                    _turn_logs_t,
                    _conversations_t.c.persona_id.label("persona_id"),
                )
                .select_from(
                    _turn_logs_t.join(
                        _conversations_t,
                        _turn_logs_t.c.conversation_id == _conversations_t.c.id,
                    )
                )
                .order_by(_turn_logs_t.c.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]
