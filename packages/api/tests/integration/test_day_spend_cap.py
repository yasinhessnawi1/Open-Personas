"""Per-UTC-day spend cap — correctness + the load-bearing TOCTOU proof (Spec R7, T2/T3).

``persona.credits.service.book_day_spend`` books ``cost`` against a per-user
per-UTC-day counter ONLY ``WHERE spent + cost <= cap`` (the R2 conditional-before-
booking family). This closes the denial-of-wallet gap: a user can't burn the whole
budget in a day, and — the load-bearing property — two concurrent near-cap requests
can't BOTH pass (the TOCTOU class R2 closed for the credit floor).

- T2 (single-thread): over/under-cap correctness + CQS (no counter movement when
  over-cap) + the first-of-day op that alone exceeds the cap is refused (the
  fail-open hole the naive ``ON CONFLICT DO UPDATE … WHERE`` would leave).
- T3 (REAL parallel): N threads each booking one unit below the cap → EXACTLY ONE
  succeeds, the rest are refused (mirrors imagegen T17's parallel-fire binary proof).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from persona.credits.service import book_day_spend
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_USER = "u_day_cap"


@pytest.fixture
def seeded_engine(pg_engine: Engine) -> Engine:
    """Insert the FK target user row so day_spend writes satisfy the constraint."""
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
            {"i": _USER, "e": f"{_USER}@x.test"},
        )
    return pg_engine


def _day_spent(engine: Engine, user_id: str) -> int:
    with engine.begin() as conn:
        row = conn.execute(
            text(
                "SELECT spent FROM day_spend "
                "WHERE user_id = :u AND utc_day = (now() AT TIME ZONE 'UTC')::date"
            ),
            {"u": user_id},
        ).scalar_one_or_none()
    return int(row) if row is not None else 0


# --- T2: single-thread correctness ---------------------------------------------


def test_under_cap_books_and_accumulates(seeded_engine: Engine) -> None:
    engine = seeded_engine
    assert book_day_spend(rls_engine=engine, user_id=_USER, cost=100, cap=1000) is True
    assert _day_spent(engine, _USER) == 100
    assert book_day_spend(rls_engine=engine, user_id=_USER, cost=250, cap=1000) is True
    assert _day_spent(engine, _USER) == 350


def test_at_cap_boundary_is_allowed(seeded_engine: Engine) -> None:
    """spent + cost == cap is within the cap (``<=``)."""
    engine = seeded_engine
    assert book_day_spend(rls_engine=engine, user_id=_USER, cost=1000, cap=1000) is True
    assert _day_spent(engine, _USER) == 1000
    # One more credit now breaches the cap.
    assert book_day_spend(rls_engine=engine, user_id=_USER, cost=1, cap=1000) is False


def test_over_cap_refuses_and_books_nothing_cqs(seeded_engine: Engine) -> None:
    """CQS: an over-cap booking moves the counter by NOTHING (fail-closed)."""
    engine = seeded_engine
    assert book_day_spend(rls_engine=engine, user_id=_USER, cost=900, cap=1000) is True
    assert _day_spent(engine, _USER) == 900
    # 900 + 200 > 1000 → refused, counter unchanged.
    assert book_day_spend(rls_engine=engine, user_id=_USER, cost=200, cap=1000) is False
    assert _day_spent(engine, _USER) == 900, "an over-cap booking must not move the counter"


def test_first_of_day_op_alone_over_cap_is_refused(seeded_engine: Engine) -> None:
    """The fail-open hole guard: a first-of-day op whose cost ALONE exceeds the cap
    is refused (no prior row exists — the ensure-row-at-0 + guarded UPDATE rejects
    ``0 + cost <= cap`` too; the naive unguarded INSERT would have booked over-cap)."""
    engine = seeded_engine
    assert _day_spent(engine, _USER) == 0
    assert book_day_spend(rls_engine=engine, user_id=_USER, cost=5000, cap=1000) is False
    assert _day_spent(engine, _USER) == 0, "no counter row should be booked over-cap"


def test_unlimited_and_zero_cost_allow_without_booking(seeded_engine: Engine) -> None:
    engine = seeded_engine
    # cap <= 0 → unlimited: allowed, nothing written.
    assert book_day_spend(rls_engine=engine, user_id=_USER, cost=10_000, cap=0) is True
    assert _day_spent(engine, _USER) == 0
    # cost <= 0 → nothing to book: allowed, nothing written.
    assert book_day_spend(rls_engine=engine, user_id=_USER, cost=0, cap=1000) is True
    assert _day_spent(engine, _USER) == 0


# --- T3: the load-bearing REAL-parallel TOCTOU proof ---------------------------


def test_concurrent_near_cap_bookings_admit_exactly_one(seeded_engine: Engine) -> None:
    """The headline adversarial case: the counter sits one unit below the cap and
    N threads each try to book 1 unit in parallel → EXACTLY ONE succeeds, the rest
    are refused. Proves two concurrent near-cap requests can't both pass (TOCTOU)."""
    engine = seeded_engine
    cap = 1000
    n_threads = 12
    # Seed the counter at cap-1 so exactly one more unit fits.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO day_spend (user_id, utc_day, spent) "
                "VALUES (:u, (now() AT TIME ZONE 'UTC')::date, :s)"
            ),
            {"u": _USER, "s": cap - 1},
        )

    def _attempt(_: int) -> bool:
        return book_day_spend(rls_engine=engine, user_id=_USER, cost=1, cap=cap)

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        results = list(pool.map(_attempt, range(n_threads)))

    successes = sum(results)
    assert successes == 1, f"expected exactly ONE admitted near-cap booking, got {successes}"
    assert _day_spent(engine, _USER) == cap, "the counter must land exactly at the cap, never over"
