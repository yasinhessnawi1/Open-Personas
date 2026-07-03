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
    insert,
    select,
    text,
    update,
)

from persona.errors import CreditsExhaustedError, DailySpendCapExceededError

if TYPE_CHECKING:
    from sqlalchemy import Connection, Engine

__all__ = [
    "LOW_BALANCE_THRESHOLD",
    "book_day_spend",
    "deduct",
    "ensure_balance",
    "get_balance",
    "list_turn_usage",
    "list_usage",
    "refund",
    "require_credits",
]

_DEFAULT_BALANCE = 100_000
# Below this threshold the web app surfaces a low-balance warning (D-11-12).
LOW_BALANCE_THRESHOLD = 10_000


# Module-private minimal table views. persona-core cannot import the api
# package, so we mirror the api-owned column shapes here (D-07-2 pattern;
# `stores/postgres.py` does the same for memory_chunks). The api-side route
# integration tests double as the contract guard that drift is caught early.
_md = MetaData()

_credits_t = Table(
    "credits",
    _md,
    Column("user_id", Text, primary_key=True),
    Column("balance", Integer, nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
)

_credit_tx_t = Table(
    "credit_transactions",
    _md,
    Column("id", Text, primary_key=True),
    Column("user_id", Text, nullable=False),
    Column("delta", Integer, nullable=False),
    Column("reason", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
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
    Column("tool_calls", Integer, nullable=False),
    Column("skill_used", Text),
    Column("history_compacted", Boolean, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)


def ensure_balance(*, rls_engine: Engine, user_id: str) -> int:
    """Return the user's balance, creating the row with the default on first use."""
    with rls_engine.begin() as conn:
        row = conn.execute(
            select(_credits_t.c.balance).where(_credits_t.c.user_id == user_id)
        ).first()
        if row is not None:
            return int(row[0])
        conn.execute(insert(_credits_t).values(user_id=user_id, balance=_DEFAULT_BALANCE))
    return _DEFAULT_BALANCE


def get_balance(*, rls_engine: Engine, user_id: str) -> int:
    """Current balance (creates the default row if absent)."""
    return ensure_balance(rls_engine=rls_engine, user_id=user_id)


def require_credits(*, rls_engine: Engine, user_id: str) -> int:
    """Pre-flight credit check: raise :class:`CreditsExhaustedError` (→ 402) if
    the caller has no credits left. Returns the balance.

    Called at the **top** of every generation endpoint — chat, agentic runs,
    persona authoring and refinement — *before* the SSE stream / run starts.
    Raising inside the SSE generator yields the spec-08 "response already
    started" trap, so the pre-flight gate is the right place (D-11-12).
    The post-success ``deduct`` (D-08-6) is unchanged.
    """
    balance = ensure_balance(rls_engine=rls_engine, user_id=user_id)
    if balance <= 0:
        raise CreditsExhaustedError(
            "Your free credits are used up. Top-up coming soon — contact support.",
            context={"balance": str(balance)},
        )
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
    *, rls_engine: Engine, user_id: str, amount: int, reason: str, daily_cap: int = 0
) -> int:
    """Deduct ``amount`` credits and record a transaction. Returns the new balance.

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
        # R7: book the day-cap FIRST, atomically-with the decrement below. Over-cap
        # ⇒ raise before any spend is booked; the ``with`` rolls back the whole txn.
        if daily_cap > 0 and amount > 0:
            booked = _book_day_spend_conn(conn, user_id=user_id, cost=amount, cap=daily_cap)
            if booked is None:
                spent = _current_day_spent(conn, user_id=user_id)
                raise DailySpendCapExceededError(
                    "Daily spend cap reached — this resets at UTC midnight.",
                    context={
                        "cap": str(daily_cap),
                        "spent": str(spent),
                        "requested_cost": str(amount),
                        "reset_epoch": str(_next_utc_midnight_epoch()),
                    },
                )
        new_balance = conn.execute(
            update(_credits_t)
            .where(_credits_t.c.user_id == user_id, _credits_t.c.balance >= amount)
            .values(balance=_credits_t.c.balance - amount, updated_at=text("now()"))
            .returning(_credits_t.c.balance)
        ).scalar_one_or_none()
        if new_balance is None:
            # No row matched ``balance >= amount`` → insufficient funds. Raise
            # WITHOUT writing a ledger row; the rolled-back transaction records
            # nothing (the ``with`` block rolls back on the exception — including
            # any day-spend booked just above, so an unaffordable turn never
            # consumes the day counter either).
            raise CreditsExhaustedError(
                "Your free credits are used up. Top-up coming soon — contact support.",
                context={"amount": str(amount), "reason": reason},
            )
        conn.execute(
            insert(_credit_tx_t).values(
                id=f"ctx_{uuid.uuid4().hex}",
                user_id=user_id,
                delta=-amount,
                reason=reason,
            )
        )
    return int(new_balance)


def refund(*, rls_engine: Engine, user_id: str, amount: int, reason: str) -> int:
    """Refund ``amount`` credits via a reverse-deduct ledger entry. Returns the new balance.

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
        return ensure_balance(rls_engine=rls_engine, user_id=user_id)
    ensure_balance(rls_engine=rls_engine, user_id=user_id)
    with rls_engine.begin() as conn:
        new_balance = conn.execute(
            update(_credits_t)
            .where(_credits_t.c.user_id == user_id)
            .values(balance=_credits_t.c.balance + amount, updated_at=text("now()"))
            .returning(_credits_t.c.balance)
        ).scalar_one()
        conn.execute(
            insert(_credit_tx_t).values(
                id=f"ctx_{uuid.uuid4().hex}",
                user_id=user_id,
                delta=amount,
                reason=reason,
            )
        )
    return int(new_balance)


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
