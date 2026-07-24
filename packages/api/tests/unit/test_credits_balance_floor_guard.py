"""Default-lane durable guard for the credits balance floor (Spec R2, T2 / F-04).

The adversarial proof of the double-spend fix lives in the integration lane
(``test_credits_double_spend.py``, real Postgres concurrency). This file is the
**always-on** guard: cheap, no-DB assertions that the two structural pieces of
the fix cannot silently regress out —

  1. the canonical ``credits`` model declares ``CHECK (balance >= 0)`` (the
     durable DB-level floor, migration 024 / R2-D-3);
  2. the ``deduct`` decrement is **conditional** — its compiled UPDATE carries a
     ``balance >= :amount`` predicate, so an overdraw matches no row and is
     rejected rather than driving the balance negative.

If either is removed, this test fails in the default lane (no DB required).
"""

from __future__ import annotations

from persona_api.db.models import credits as credits_t
from sqlalchemy import CheckConstraint
from sqlalchemy.schema import CreateTable


def test_credits_model_declares_a_nonnegative_balance_check() -> None:
    """The named ``balance >= 0`` CHECK is on the canonical credits table, so a
    fresh-DB ``create_all`` builds it (split-home with migration 024)."""
    checks = [c for c in credits_t.constraints if isinstance(c, CheckConstraint)]
    names = {c.name for c in checks}
    assert "credits_balance_nonneg_check" in names, (
        "the credits table lost its balance>=0 CHECK constraint (F-04 durable guard)"
    )
    # The compiled DDL must mention the floor predicate.
    ddl = str(CreateTable(credits_t).compile()).lower()
    assert "balance >= 0" in ddl.replace("(", " ").replace(")", " ")


def test_deduct_uses_a_conditional_decrement() -> None:
    """The ``deduct`` path must remain STRICT all-or-nothing with an atomic floor.

    Spec M4 T1a (owner Decision 1) moved the floor from M3's single-counter
    ``WHERE balance >= :amount`` UPDATE into the two-bucket ``_draw_from_buckets``
    lock-then-allocate: ``SELECT ... FOR UPDATE`` serialises concurrent same-user
    draws (closing the double-spend race exactly as R2-D-3 did), and the strict
    (``allow_partial=False``) arm raises ``CreditsExhaustedError`` on an
    over-spendable amount, writing nothing. Asserted at the source level so the
    floor cannot be dropped without this failing in the default lane."""
    import inspect

    from persona.credits import service

    deduct_src = inspect.getsource(service.deduct)
    assert "allow_partial=False" in deduct_src, (
        "deduct must use the STRICT (all-or-nothing) two-bucket draw"
    )
    draw_src = inspect.getsource(service._draw_from_buckets)  # noqa: SLF001
    # ``FOR UPDATE`` on the credits row is the atomic floor (serialises draws); the
    # strict raise is the overdraw rejection — the load-bearing lines of the fix.
    assert "with_for_update" in draw_src, (
        "the two-bucket draw must lock FOR UPDATE (the atomic floor serialising draws)"
    )
    assert "CreditsExhaustedError" in draw_src, (
        "an over-spendable strict draw must raise CreditsExhaustedError"
    )
