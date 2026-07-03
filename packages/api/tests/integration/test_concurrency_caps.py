"""Shared per-user concurrency caps — both forms, REAL parallelism (Spec R7, T7/T8).

``persona.concurrency`` is the ONE consolidated helper (imagegen + voice re-export it).

- T7 (bounded, N-slot advisory): N+1 concurrent holders against N slots → EXACTLY N
  admitted (the T3 real-parallel discipline, barrier-coordinated so all hold at once);
  ``slots=1`` preserves cap-1; ``slots<=0`` is the unlimited community no-op; the lock
  is xact-scoped (auto-released on commit/rollback — no leak on a mid-flight raise).
- T8 (long-running, durable count): N+1 concurrent starts against cap N → EXACTLY N
  admitted; the release path frees a slot (next admit succeeds); crash-safety — a
  leaked row aged past the TTL is reclaimed so a killed process can't eat a slot
  forever; ``max_concurrent<=0`` is the unlimited community no-op.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING

import pytest
from persona.concurrency import acquire_user_concurrency, admit_long_op, release_long_op
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration


@pytest.fixture
def seeded_engine(pg_engine: Engine) -> Engine:
    with pg_engine.begin() as conn:
        for uid in ("u_bounded", "u_bounded1", "u_unl", "u_long", "u_rel", "u_crash", "u_uncap"):
            conn.execute(
                text("INSERT INTO users (id, email) VALUES (:i, :e) ON CONFLICT DO NOTHING"),
                {"i": uid, "e": f"{uid}@x.test"},
            )
    return pg_engine


def _inflight_count(engine: Engine, user_id: str, op_class: str) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM inflight_ops WHERE user_id=:u AND op_class=:c"),
                {"u": user_id, "c": op_class},
            ).scalar_one()
        )


# --- T7: bounded N-slot advisory -----------------------------------------------


def test_n_plus_one_concurrent_holders_admit_exactly_n(seeded_engine: Engine) -> None:
    """N+1 threads each open a txn and try to hold one of N slots AT THE SAME TIME
    (a barrier keeps every acquirer's transaction open while the others attempt) →
    exactly N acquire, 1 is refused. The bounded-op parallel-spend proof."""
    engine = seeded_engine
    n_slots = 3
    n_threads = n_slots + 1
    barrier = threading.Barrier(n_threads, timeout=30)

    def _hold(_: int) -> bool:
        with (
            engine.begin() as conn,
            acquire_user_concurrency(conn=conn, user_id="u_bounded", slots=n_slots) as acquired,
        ):
            # Hold the slot (transaction stays open) until everyone has attempted.
            barrier.wait()
            return acquired

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        results = list(pool.map(_hold, range(n_threads)))

    assert sum(results) == n_slots, f"expected exactly {n_slots} admitted, got {sum(results)}"


def test_slots_one_preserves_cap_of_one(seeded_engine: Engine) -> None:
    engine = seeded_engine
    n_threads = 5
    barrier = threading.Barrier(n_threads, timeout=30)

    def _hold(_: int) -> bool:
        with (
            engine.begin() as conn,
            acquire_user_concurrency(conn=conn, user_id="u_bounded1", slots=1) as acquired,
        ):
            barrier.wait()
            return acquired

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        results = list(pool.map(_hold, range(n_threads)))
    assert sum(results) == 1, "slots=1 admits exactly one (byte-preserved cap-1)"


def test_slot_auto_releases_on_transaction_exit(seeded_engine: Engine) -> None:
    """The slot is xact-scoped: after the holding transaction commits OR rolls back,
    the slot is free again (no leak on a mid-flight raise)."""
    engine = seeded_engine
    # Commit path: acquire in a txn, exit → slot free → re-acquire succeeds.
    with (
        engine.begin() as conn,
        acquire_user_concurrency(conn=conn, user_id="u_bounded", slots=1) as first,
    ):
        assert first is True
    with (
        engine.begin() as conn,
        acquire_user_concurrency(conn=conn, user_id="u_bounded", slots=1) as second,
    ):
        assert second is True, "a committed holder must have released its slot"

    # Rollback path: a raise inside the holding txn must still free the slot.
    def _acquire_then_raise() -> None:
        with (
            engine.begin() as conn,
            acquire_user_concurrency(conn=conn, user_id="u_bounded", slots=1) as held,
        ):
            assert held is True
            raise RuntimeError("mid-flight")

    with pytest.raises(RuntimeError):
        _acquire_then_raise()
    with (
        engine.begin() as conn,
        acquire_user_concurrency(conn=conn, user_id="u_bounded", slots=1) as after_raise,
    ):
        assert after_raise is True, "a rolled-back holder must have released its slot"


def test_slots_zero_is_unlimited_no_op(seeded_engine: Engine) -> None:
    """slots<=0 (community/uncapped) always admits without taking any lock."""
    engine = seeded_engine
    barrier = threading.Barrier(6, timeout=30)

    def _hold(_: int) -> bool:
        with (
            engine.begin() as conn,
            acquire_user_concurrency(conn=conn, user_id="u_unl", slots=0) as acquired,
        ):
            barrier.wait()
            return acquired

    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(_hold, range(6)))
    assert all(results), "slots<=0 is the unlimited no-op — every attempt admits"


# --- T8: durable-count long-op admission ---------------------------------------


def test_long_op_n_plus_one_starts_admit_exactly_n(seeded_engine: Engine) -> None:
    """N+1 concurrent long-op starts against cap N → exactly N tokens issued, 1 None.
    Serialized count-then-insert under the admission lock — no over-admit race."""
    engine = seeded_engine
    cap = 3
    n_threads = cap + 1

    def _start(_: int) -> str | None:
        return admit_long_op(
            rls_engine=engine, user_id="u_long", op_class="chat", max_concurrent=cap
        )

    with ThreadPoolExecutor(max_workers=n_threads) as pool:
        tokens = list(pool.map(_start, range(n_threads)))

    admitted = [t for t in tokens if t is not None]
    assert len(admitted) == cap, f"expected exactly {cap} admitted long ops, got {len(admitted)}"
    assert _inflight_count(engine, "u_long", "chat") == cap, "durable rows match admits"


def test_long_op_release_frees_a_slot(seeded_engine: Engine) -> None:
    """The release path: completion deletes the row → the next admit succeeds."""
    engine = seeded_engine
    cap = 2
    t1 = admit_long_op(rls_engine=engine, user_id="u_rel", op_class="agentic", max_concurrent=cap)
    t2 = admit_long_op(rls_engine=engine, user_id="u_rel", op_class="agentic", max_concurrent=cap)
    assert t1 is not None
    assert t2 is not None
    # Cap full → refused.
    refused = admit_long_op(
        rls_engine=engine, user_id="u_rel", op_class="agentic", max_concurrent=cap
    )
    assert refused is None
    # Release one → a slot frees → next admit succeeds.
    release_long_op(rls_engine=engine, user_id="u_rel", op_id=t1)
    t3 = admit_long_op(rls_engine=engine, user_id="u_rel", op_class="agentic", max_concurrent=cap)
    assert t3 is not None, "releasing a slot must let the next long op start"
    assert _inflight_count(engine, "u_rel", "agentic") == cap


def test_long_op_crash_safety_stale_row_reclaimed(seeded_engine: Engine) -> None:
    """A leaked inflight_ops row from a killed process must NOT eat a slot forever:
    a row aged past the TTL is swept at the next admit, freeing the slot."""
    engine = seeded_engine
    # Simulate a crashed op's leaked row, started well beyond the TTL.
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO inflight_ops (id, user_id, op_class, started_at) "
                "VALUES ('leaked', 'u_crash', 'chat', now() - make_interval(secs => 7200))"
            )
        )
    # Cap 1, TTL 60s: the 2h-old leaked row is stale → swept → admit succeeds.
    token = admit_long_op(
        rls_engine=engine, user_id="u_crash", op_class="chat", max_concurrent=1, ttl_seconds=60
    )
    assert token is not None, "a stale (crashed) row must be reclaimed, not eat the slot forever"
    # The leaked row is gone; only the fresh admit remains.
    assert _inflight_count(engine, "u_crash", "chat") == 1

    # A FRESH row (not stale) is NOT reclaimed — the cap still holds.
    fresh_refused = admit_long_op(
        rls_engine=engine, user_id="u_crash", op_class="chat", max_concurrent=1, ttl_seconds=60
    )
    assert fresh_refused is None, "a live (fresh) row must still count against the cap"


def test_long_op_uncapped_is_no_op(seeded_engine: Engine) -> None:
    """max_concurrent<=0 (community/uncapped) always admits, writes no durable row."""
    engine = seeded_engine
    for _ in range(5):
        token = admit_long_op(
            rls_engine=engine, user_id="u_uncap", op_class="chat", max_concurrent=0
        )
        assert token is not None
    assert _inflight_count(engine, "u_uncap", "chat") == 0, "uncapped admission writes no row"
    # Releasing the sentinel is a harmless no-op.
    release_long_op(rls_engine=engine, user_id="u_uncap", op_id="unlimited")
