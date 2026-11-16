"""The account erasure's order, runner and external steps (R9-284 T9).

The SQL steps need Postgres and run in CI (tests/integration/test_account_erasure_postgres.py).
Here: the step order (the fence first, the users row last before the late writes), the runner
on a SQLite ledger (records, stops and fails at the first error, completes), and the Stripe and
Clerk steps' decisions with fakes. Nothing reaches Stripe or Clerk.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest
from persona_api.db.models import erasures
from persona_api.errors import ErasureRecordError
from persona_api.services import account_erasure as ae
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from sqlalchemy import create_engine

if TYPE_CHECKING:
    from persona_api.services.erasure_ledger import Erasure
    from sqlalchemy import Engine

_OWNER = "user_abc123"


@pytest.fixture
def engine() -> Engine:
    eng = create_engine("sqlite://")
    erasures.create(eng)
    return eng


def _deps(engine: Engine, *, stripe: Any = None, clerk: Any = None) -> ae.AccountEraseDeps:  # noqa: ANN401
    class _Persona:
        pass

    persona = _Persona()
    persona.engine = engine  # type: ignore[attr-defined]
    return ae.AccountEraseDeps(
        persona=persona,  # type: ignore[arg-type]
        stripe=stripe,
        clerk=clerk,
        retention=timedelta(days=365 * 5),
    )


def _claim(engine: Engine, captured: dict[str, list[str]] | None = None) -> Erasure:
    return ErasureLedger(engine).claim(
        owner_id=_OWNER,
        subject_kind=SubjectKind.ACCOUNT,
        subject_id=_OWNER,
        captured=captured or {"origin": [ae.ORIGIN_ROUTE]},
    )


# ---- the order -------------------------------------------------------------------------


def test_the_fence_comes_first_and_the_users_row_last_before_the_late_writes() -> None:
    names = [name for name, _ in ae.STEPS]

    assert names == [
        "fence",
        "stripe",
        "personas",
        "billing",
        "audit",
        "clerk",
        "row",
        "late_writes",
    ]


def test_nothing_that_needs_the_users_row_runs_after_it() -> None:
    names = [name for name, _ in ae.STEPS]
    row = names.index("row")
    for needs_the_row in ("stripe", "personas", "billing", "audit", "clerk"):
        assert names.index(needs_the_row) < row, needs_the_row


def test_the_late_writes_fence_rekey_and_delete_again(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[str] = []
    for name in ("_step_fence", "_step_audit", "_step_row", "_step_stripe"):
        monkeypatch.setattr(ae, name, lambda *_a, n=name: ran.append(n) or {n.strip("_"): 0})

    ae._step_late_writes(None, None)  # type: ignore[arg-type]  # noqa: SLF001

    assert ran == ["_step_fence", "_step_audit", "_step_row", "_step_stripe"], (
        "the stripe step again, after the row is gone (review M4)"
    )


# ---- the runner ------------------------------------------------------------------------


def _recording(monkeypatch: pytest.MonkeyPatch, fail_at: str | None = None) -> list[str]:
    ran: list[str] = []

    def _step(name: str) -> ae.Step:
        def run(_deps: ae.AccountEraseDeps, _erasure: Erasure) -> dict[str, int]:
            ran.append(name)
            if name == fail_at:
                raise RuntimeError(name)
            return {"done": 1}

        return run

    monkeypatch.setattr(ae, "STEPS", tuple((name, _step(name)) for name, _ in ae.STEPS))
    monkeypatch.setattr(ae.audit_service, "record", lambda **_k: None)
    return ran


def test_the_runner_runs_every_step_in_order_and_completes(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = _recording(monkeypatch)
    erasure = _claim(engine)

    ae.erase_account(_deps(engine), erasure)

    assert ran == [name for name, _ in ae.STEPS]
    done = ErasureLedger(engine).get(SubjectKind.ACCOUNT, _OWNER)
    assert done is not None
    assert done.state is ErasureState.COMPLETE
    assert set(done.counts) == set(ran)


def test_a_failed_step_stops_the_erasure_before_the_users_row(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = _recording(monkeypatch, fail_at="clerk")
    erasure = _claim(engine)

    with pytest.raises(RuntimeError):
        ae.erase_account(_deps(engine), erasure)

    assert "row" not in ran, "the users row is never deleted past a failed step"
    failed = ErasureLedger(engine).get(SubjectKind.ACCOUNT, _OWNER)
    assert failed is not None
    assert failed.state is ErasureState.FAILED
    assert failed.error_code == "clerk_failed"


def test_a_persona_erasure_is_refused(engine: Engine) -> None:
    erasure = ErasureLedger(engine).claim(
        owner_id=_OWNER, subject_kind=SubjectKind.PERSONA, subject_id="persona_x", captured={}
    )

    with pytest.raises(ErasureRecordError):
        ae.erase_account(_deps(engine), erasure)


def test_a_finished_erasure_is_not_run_again(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran = _recording(monkeypatch)
    erasure = _claim(engine)
    ErasureLedger(engine).complete(erasure.id)

    ae.erase_account(_deps(engine), erasure)

    assert ran == []


# ---- stripe and clerk ------------------------------------------------------------------


class _Stripe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def erase_customer(self, customer_id: str, *, erasure_id: str) -> dict[str, int]:
        self.calls.append((customer_id, erasure_id))
        return {"subscriptions_cancelled": 1}


class _Clerk:
    def __init__(self, *, gone: bool = False) -> None:
        self.gone = gone
        self.deleted: list[str] = []

    def delete_user(self, user_id: str) -> bool:
        self.deleted.append(user_id)
        return not self.gone


def test_no_customer_means_no_stripe_call(engine: Engine) -> None:
    stripe = _Stripe()

    counts = ae._step_stripe(_deps(engine, stripe=stripe), _claim(engine))  # noqa: SLF001

    assert counts == {"customers": 0}
    assert stripe.calls == []


def test_each_customer_is_erased_under_the_erasure_id(engine: Engine) -> None:
    stripe = _Stripe()
    erasure = _claim(engine, {"stripe_customers": ["cus_1"], "origin": [ae.ORIGIN_ROUTE]})

    counts = ae._step_stripe(_deps(engine, stripe=stripe), erasure)  # noqa: SLF001

    assert stripe.calls == [("cus_1", erasure.id)]
    assert counts == {"customers": 1, "subscriptions_cancelled": 1}


def test_a_customer_without_stripe_configured_refuses(engine: Engine) -> None:
    erasure = _claim(engine, {"stripe_customers": ["cus_1"]})

    with pytest.raises(ErasureRecordError):
        ae._step_stripe(_deps(engine), erasure)  # noqa: SLF001


def test_our_route_deletes_the_clerk_user(engine: Engine) -> None:
    clerk = _Clerk()

    counts = ae._step_clerk(_deps(engine, clerk=clerk), _claim(engine))  # noqa: SLF001

    assert clerk.deleted == [_OWNER]
    assert counts == {"deleted": 1}


def test_an_already_gone_clerk_user_counts_as_done(engine: Engine) -> None:
    counts = ae._step_clerk(_deps(engine, clerk=_Clerk(gone=True)), _claim(engine))  # noqa: SLF001

    assert counts == {"deleted": 0}


def test_clerks_own_deletion_is_not_repeated(engine: Engine) -> None:
    clerk = _Clerk()
    erasure = _claim(engine, {"origin": [ae.ORIGIN_CLERK]})

    counts = ae._step_clerk(_deps(engine, clerk=clerk), erasure)  # noqa: SLF001

    assert clerk.deleted == []
    assert counts == {"deleted": 0}


def test_our_route_without_clerk_configured_refuses(engine: Engine) -> None:
    with pytest.raises(ErasureRecordError):
        ae._step_clerk(_deps(engine), _claim(engine))  # noqa: SLF001


def test_an_unknown_origin_is_refused(engine: Engine) -> None:
    with pytest.raises(ErasureRecordError):
        ae.begin_account_erasure(engine, owner_id=_OWNER, origin="elsewhere")


# ---- the users row (H1, M19) and waiting for persona erasures (M7) ---------------------


class _RowConn:
    """Records each statement of the row step; ``personas`` is what the re-list finds."""

    def __init__(
        self, personas: list[str], *, user: bool = True, paid: list[str] | None = None
    ) -> None:
        from sqlalchemy.dialects import postgresql  # noqa: PLC0415

        self.dialect = postgresql.dialect()
        self.personas = personas
        self.user = user
        self.paid = paid or []
        self.sql: list[str] = []

    def execute(self, stmt: Any, params: Any = None) -> Any:  # noqa: ANN401, ARG002
        sql = str(stmt.compile(dialect=self.dialect)) if hasattr(stmt, "compile") else str(stmt)
        self.sql.append(" ".join(sql.split()))
        rows: list[Any] = []
        if sql.startswith("SELECT users.id"):
            rows = [("x",)] if self.user else []
        elif sql.startswith("SELECT personas.id"):
            rows = [(p,) for p in self.personas]
        elif sql.startswith("SELECT credit_transactions.id"):
            # Honour the reason filter: only rows whose reason the query asks for come back.
            asked = str(stmt.compile(dialect=self.dialect, compile_kwargs={"literal_binds": True}))
            rows = (
                []
                if "'auto_topup'" not in asked
                else [
                    SimpleNamespace(
                        id=i,
                        reason="auto_topup",
                        delta=1000,
                        billing_key="bk",
                        cost_cents=1000.0,
                        created_at=None,
                    )
                    for i in self.paid
                ]
            )
        added = 1
        if sql.startswith("INSERT INTO retained_billing_records"):
            rows = [(f"retained_{i}",) for i in self.paid]
            added = -1  # what the driver may report for this INSERT: never to be trusted

        class _R:
            rowcount = added

            def __iter__(self) -> Any:  # noqa: ANN401
                return iter(rows)

            def all(self) -> list[Any]:
                return list(rows)

            def first(self) -> Any:  # noqa: ANN401
                return rows[0] if rows else None

            def scalars(self) -> list[Any]:
                return [r[0] for r in rows]

        return _R()


class _RowEngine:
    def __init__(self, conn: _RowConn) -> None:
        self.conn = conn

    def begin(self) -> Any:  # noqa: ANN401
        from contextlib import nullcontext  # noqa: PLC0415

        return nullcontext(self.conn)


def _row_step(conn: _RowConn) -> dict[str, int]:
    deps = _deps(_RowEngine(conn))  # type: ignore[arg-type]
    erasure = SimpleNamespace(owner_id=_OWNER, id="erasure_1", captured={})
    return ae._step_row(deps, erasure)  # type: ignore[arg-type]  # noqa: SLF001


def test_the_row_step_locks_the_graph_then_the_user_then_relists_then_deletes() -> None:
    conn = _RowConn([])

    assert _row_step(conn) == {"users": 1, "retained": 0}

    assert conn.sql[0] == "SET LOCAL lock_timeout = '30s'", "it fails rather than hangs"
    assert "pg_advisory_xact_lock" in conn.sql[1], "the graph maintenance lock first"
    assert conn.sql[2].startswith("SELECT users.id")
    assert conn.sql[2].endswith("FOR UPDATE"), "no persona insert can commit past this"
    assert conn.sql[3].startswith("SELECT personas.id"), "the personas listed again under it"
    assert conn.sql[-1].startswith("DELETE FROM users"), "the delete is last"


def test_a_persona_created_during_the_erasure_stops_the_row_delete() -> None:
    conn = _RowConn(["persona_late"])

    with pytest.raises(ae.AccountErasureRaceError):
        _row_step(conn)

    assert not [s for s in conn.sql if s.startswith("DELETE")], "nothing cascaded away"


def test_a_row_already_gone_deletes_nothing() -> None:
    conn = _RowConn([], user=False)

    assert _row_step(conn) == {"users": 0}
    assert not [s for s in conn.sql if s.startswith("DELETE")]


def test_the_account_waits_for_an_unfinished_persona_erasure(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reviewer's survivor M7: a persona erasure held elsewhere is not finished, so the
    personas step raises and the users row is not reached."""
    ErasureLedger(engine).claim(
        owner_id=_OWNER, subject_kind=SubjectKind.PERSONA, subject_id="persona_x", captured={}
    )
    monkeypatch.setattr(ae, "erase_persona", lambda *_a: None)  # another run holds its lock

    with pytest.raises(ae.AccountErasurePendingError):
        ae._erase_one_persona(_deps(engine), _OWNER, "persona_x")  # noqa: SLF001


# ---- readiness (review H2) -------------------------------------------------------------


@pytest.fixture
def billing_engine(engine: Engine) -> Engine:
    from persona_api.db.models import subscription  # noqa: PLC0415
    from sqlalchemy import text  # noqa: PLC0415

    subscription.create(engine)
    with engine.begin() as conn:  # the capture reads only these two (the real one is Postgres)
        conn.execute(text("CREATE TABLE personas (id TEXT PRIMARY KEY, owner_id TEXT)"))
    return engine


def _with_customer(engine: Engine) -> None:
    from persona_api.db.models import subscription  # noqa: PLC0415
    from sqlalchemy import insert  # noqa: PLC0415

    with engine.begin() as conn:
        conn.execute(insert(subscription).values(user_id=_OWNER, stripe_customer_id="cus_1"))


def test_our_route_needs_clerk(billing_engine: Engine) -> None:
    deps = _deps(billing_engine, stripe=_Stripe())

    assert ae.unready_reason(deps, owner_id=_OWNER, origin=ae.ORIGIN_ROUTE) == (
        "clerk_not_configured"
    )
    assert ae.unready_reason(deps, owner_id=_OWNER, origin=ae.ORIGIN_CLERK) is None


def test_a_stripe_customer_needs_stripe(billing_engine: Engine) -> None:
    deps = _deps(billing_engine, clerk=_Clerk())
    assert ae.unready_reason(deps, owner_id=_OWNER, origin=ae.ORIGIN_ROUTE) is None

    _with_customer(billing_engine)

    assert ae.unready_reason(deps, owner_id=_OWNER, origin=ae.ORIGIN_ROUTE) == (
        "stripe_not_configured"
    )
    with_stripe = _deps(billing_engine, clerk=_Clerk(), stripe=_Stripe())
    assert ae.unready_reason(with_stripe, owner_id=_OWNER, origin=ae.ORIGIN_ROUTE) is None


# ---- the retained subscription (review M2) ---------------------------------------------


def test_the_retained_subscription_is_the_claims_snapshot(billing_engine: Engine) -> None:
    """Our cancel's webhook rewrites the live row to free between the stripe and billing steps;
    the retained record still shows the paid plan, under a fixed id (so it is copied once)."""
    from persona_api.db.models import subscription  # noqa: PLC0415
    from sqlalchemy import insert, update  # noqa: PLC0415

    with billing_engine.begin() as conn:
        conn.execute(
            insert(subscription).values(
                user_id=_OWNER,
                plan_code="pro",
                stripe_customer_id="cus_1",
                stripe_subscription_id="sub_1",
            )
        )
    erasure = ae.begin_account_erasure(billing_engine, owner_id=_OWNER, origin=ae.ORIGIN_ROUTE)
    with billing_engine.begin() as conn:  # the webhook's mark_canceled, after our cancel
        conn.execute(update(subscription).values(plan_code="free", status="canceled"))

    first = ae.retained_subscription(erasure)
    again = ae.retained_subscription(
        ErasureLedger(billing_engine).get(SubjectKind.ACCOUNT, _OWNER)  # type: ignore[arg-type]
    )

    assert first is not None
    assert first["reason"] == "pro", "the plan that was paid for, not the canceled row's"
    assert first["source_id"] == "subscription"
    assert first["stripe_subscription_id"] == "sub_1"
    assert again == first, "a resume builds the same record, which ON CONFLICT keeps once"


def test_no_paid_subscription_retains_nothing(billing_engine: Engine) -> None:
    erasure = ae.begin_account_erasure(billing_engine, owner_id=_OWNER, origin=ae.ORIGIN_ROUTE)

    assert ae.retained_subscription(erasure) is None


def test_a_paid_row_written_after_the_billing_step_is_copied_before_the_delete() -> None:
    """Review M3 (and the reviewer's survivor M5): an auto top-up that landed between the
    billing and row steps is retained, copied under the users-row lock before the cascade."""
    conn = _RowConn([], paid=["ct_late"])

    counts = _row_step(conn)

    assert counts == {"users": 1, "retained": 1}
    insert_at = next(
        i for i, s in enumerate(conn.sql) if s.startswith("INSERT INTO retained_billing_records")
    )
    delete_at = next(i for i, s in enumerate(conn.sql) if s.startswith("DELETE FROM users"))
    lock_at = next(i for i, s in enumerate(conn.sql) if s.endswith("FOR UPDATE"))
    assert lock_at < insert_at < delete_at, "copied under the lock, before the cascade"


# ---- the retained INSERT compiles on Postgres (CI failure on 030caf43) --------------------


def test_the_retained_insert_compiles_for_every_kind_of_row_together() -> None:
    """A credit row, a PAYG grant and the subscription carry different fields; the multi-row
    INSERT must still compile for Postgres (it did not, and only CI found it)."""
    from datetime import UTC, datetime  # noqa: PLC0415

    from sqlalchemy.dialects import postgresql  # noqa: PLC0415

    until = datetime(2031, 11, 16, tzinfo=UTC)
    common = {"erasure_id": "erasure_1", "stripe_customer_id": "cus_1", "retain_until": until}
    rows = [
        {
            **common,
            "source_table": "credit_transactions",
            "source_id": "ct_1",
            "reason": "payg_topup",
            "credits": 1000,
            "billing_key": "bk",
            "cost_cents": 1000.0,
            "occurred_at": until,
        },
        {
            **common,
            "source_table": "payg_grants",
            "source_id": "pg_1",
            "reason": "payg_grant",
            "credits": 1000,
            "billing_key": "bk",
            "occurred_at": until,
        },
        {
            **common,
            "source_table": "subscription",
            "source_id": "subscription",
            "reason": "pro",
            "stripe_subscription_id": "sub_1",
            "occurred_at": None,
        },
    ]

    sql = str(ae.retained_insert(rows).compile(dialect=postgresql.dialect()))

    assert sql.startswith("INSERT INTO retained_billing_records")
    assert "ON CONFLICT (erasure_id, source_table, source_id) DO NOTHING" in sql
    assert sql.rstrip().endswith("RETURNING retained_billing_records.id"), "counted by RETURNING"


def test_each_kind_of_row_alone_compiles_too() -> None:
    from sqlalchemy.dialects import postgresql  # noqa: PLC0415

    for row in (
        {"erasure_id": "e", "source_table": "subscription", "source_id": "subscription"},
        {"erasure_id": "e", "source_table": "payg_grants", "source_id": "p", "credits": 1},
    ):
        str(ae.retained_insert([row]).compile(dialect=postgresql.dialect()))


def test_a_resume_does_not_call_stripe_or_clerk_again(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review L4: external steps recorded done on an earlier run are skipped."""
    ran = _recording(monkeypatch)
    erasure = _claim(engine)
    ledger = ErasureLedger(engine)
    ledger.record_step(erasure.id, "stripe", {"customers": 1})
    ledger.record_step(erasure.id, "clerk", {"deleted": 1})
    ledger.fail(erasure.id, "billing_failed")

    ae.erase_account(_deps(engine), erasure)

    assert "stripe" not in ran
    assert "clerk" not in ran
    assert "late_writes" in ran, "late_writes still runs (and re-runs Stripe itself)"


# ---- survivors (review round 2: M07, M10, M44) -----------------------------------------


def test_a_resume_runs_every_internal_step_again(
    engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M07: only Stripe and Clerk are skipped once done; the personas step (and every other
    internal one) runs again, since a persona may have appeared since."""
    ran = _recording(monkeypatch)
    erasure = _claim(engine)
    ledger = ErasureLedger(engine)
    for name in ("fence", "stripe", "personas", "billing", "audit", "clerk", "row"):
        ledger.record_step(erasure.id, name, {"done": 1})
    ledger.fail(erasure.id, "late_writes_failed")

    ae.erase_account(_deps(engine), erasure)

    assert ran == ["fence", "personas", "billing", "audit", "row", "late_writes"]


def _subscription(engine: Engine, **values: object) -> None:
    from persona_api.db.models import subscription  # noqa: PLC0415
    from sqlalchemy import insert  # noqa: PLC0415

    with engine.begin() as conn:
        conn.execute(insert(subscription).values(user_id=_OWNER, **values))


def test_a_free_subscription_row_is_not_a_paid_snapshot(billing_engine: Engine) -> None:
    """M10: a subscription row with no Stripe ids is the free plan; nothing is retained."""
    _subscription(billing_engine, plan_code="free")

    erasure = ae.begin_account_erasure(billing_engine, owner_id=_OWNER, origin=ae.ORIGIN_ROUTE)

    assert erasure.captured["subscription_plan"] == []
    assert ae.retained_subscription(erasure) is None


def test_the_snapshot_carries_when_the_subscription_started(billing_engine: Engine) -> None:
    """M44: the start is captured with the plan, and the retained record carries it."""
    from datetime import datetime  # noqa: PLC0415

    started = datetime(2026, 3, 1, 9, 30)  # noqa: DTZ001 - SQLite hands timestamps back naive
    _subscription(
        billing_engine,
        plan_code="plus",
        stripe_customer_id="cus_1",
        stripe_subscription_id="sub_1",
        created_at=started,
    )

    erasure = ae.begin_account_erasure(billing_engine, owner_id=_OWNER, origin=ae.ORIGIN_ROUTE)

    assert erasure.captured["subscription_started"] == [started.isoformat()]
    record = ae.retained_subscription(erasure)
    assert record is not None
    assert record["occurred_at"] == started


# ---- every persona's kept log (review round 3 MEDIUM-1) --------------------------------


def test_the_audit_rekey_covers_every_persona_the_owner_ever_had(engine: Engine) -> None:
    """Captured at the claim, met later, and erased before the account (ledger rows)."""
    ledger = ErasureLedger(engine)
    old = ledger.claim(
        owner_id=_OWNER, subject_kind=SubjectKind.PERSONA, subject_id="persona_old", captured={}
    )
    ledger.complete(old.id)
    ledger.claim(
        owner_id="user_someone_else",
        subject_kind=SubjectKind.PERSONA,
        subject_id="persona_theirs",
        captured={},
    )
    erasure = _claim(engine, {"personas": ["persona_now"], "origin": [ae.ORIGIN_ROUTE]})

    assert ae.personas_ever_owned(_deps(engine), erasure) == ["persona_now", "persona_old"]


def test_the_personas_step_records_every_persona_it_met(
    billing_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A persona created after the claim is recorded, so the audit step re-keys its log."""
    from sqlalchemy import text  # noqa: PLC0415

    monkeypatch.setattr(ae, "_erase_one_persona", lambda *_a: 1)
    erasure = _claim(billing_engine, {"personas": ["persona_a"], "origin": [ae.ORIGIN_ROUTE]})
    with billing_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO personas (id, owner_id) VALUES ('persona_late', :o)"), {"o": _OWNER}
        )

    ae._step_personas(_deps(billing_engine), erasure)  # noqa: SLF001

    recorded = ErasureLedger(billing_engine).get(SubjectKind.ACCOUNT, _OWNER)
    assert recorded is not None
    assert recorded.captured["personas"] == ["persona_a", "persona_late"]
