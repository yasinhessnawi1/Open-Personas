"""The account erasure's order, runner and external steps (R9-284 T9).

The SQL steps need Postgres and run in CI (tests/integration/test_account_erasure_postgres.py).
Here: the step order (the fence first, the users row last before the late writes), the runner
on a SQLite ledger (records, stops and fails at the first error, completes), and the Stripe and
Clerk steps' decisions with fakes. Nothing reaches Stripe or Clerk.
"""

from __future__ import annotations

from datetime import timedelta
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
