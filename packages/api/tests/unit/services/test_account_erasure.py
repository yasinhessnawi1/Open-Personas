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
    for name in ("_step_fence", "_step_audit", "_step_row"):
        monkeypatch.setattr(ae, name, lambda *_a, n=name: ran.append(n) or {n.strip("_"): 0})

    ae._step_late_writes(None, None)  # type: ignore[arg-type]  # noqa: SLF001

    assert ran == ["_step_fence", "_step_audit", "_step_row"]


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

    def __init__(self, personas: list[str], *, user: bool = True) -> None:
        from sqlalchemy.dialects import postgresql  # noqa: PLC0415

        self.dialect = postgresql.dialect()
        self.personas = personas
        self.user = user
        self.sql: list[str] = []

    def execute(self, stmt: Any, params: Any = None) -> Any:  # noqa: ANN401, ARG002
        sql = str(stmt.compile(dialect=self.dialect)) if hasattr(stmt, "compile") else str(stmt)
        self.sql.append(" ".join(sql.split()))
        rows: list[Any] = []
        if sql.startswith("SELECT users.id"):
            rows = [("x",)] if self.user else []
        elif sql.startswith("SELECT personas.id"):
            rows = [(p,) for p in self.personas]

        class _R:
            rowcount = 1

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
    erasure = SimpleNamespace(owner_id=_OWNER)
    return ae._step_row(deps, erasure)  # type: ignore[arg-type]  # noqa: SLF001


def test_the_row_step_locks_the_graph_then_the_user_then_relists_then_deletes() -> None:
    conn = _RowConn([])

    assert _row_step(conn) == {"users": 1}

    assert "pg_advisory_xact_lock" in conn.sql[0], "the graph maintenance lock first"
    assert conn.sql[1].startswith("SELECT users.id")
    assert conn.sql[1].endswith("FOR UPDATE"), "no persona insert can commit past this"
    assert conn.sql[2].startswith("SELECT personas.id"), "the personas listed again under it"
    assert conn.sql[3].startswith("DELETE FROM users")


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
