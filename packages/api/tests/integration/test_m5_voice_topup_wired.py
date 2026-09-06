"""Voice enqueues, the REAL worker drains, and only then does a top-up happen (Spec M5, B5).

This is the test the whole task turns on. B5 is cross-process composition: the voice
process observes a balance crossing and enqueues a durable job; the api worker claims it
and calls ``maybe_auto_topup``. That is exactly the shape where a capability exists, its
unit tests are green, and it never fires in production, because nothing proved the job
ever reaches the handler.

So nothing here is invoked by hand:

* the enqueue is the REAL voice-side writer (``persona_voice.billing.topup_enqueue``),
  a peer process's raw INSERT into the real ``jobs`` table;
* the drain is the REAL A0 ``Worker.run_once()``, claiming from that table;
* the charge is observed as a CONSEQUENCE, through the gateway the handler was
  registered with.

If the conditional registration in ``worker_root`` is wrong, or the writer's job type
drifts from the handler's, these go red. A test that called ``AutoTopupHandler`` directly
would prove the handler works while proving nothing about whether it is ever reached.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

import pytest
from persona.jobs import AUTO_TOPUP_JOB_TYPE, JobRegistry
from persona_api.jobs.handlers.auto_topup import register_auto_topup_handler
from persona_api.jobs.worker import Worker
from persona_api.middleware.rls_context import make_rls_engine
from persona_voice.billing.topup_enqueue import INSERTED_COLUMNS, enqueue_auto_topup
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_OWNER = "u_m5_voice_topup"
_CALL = "call_m5_topup"


class _FakeGateway:
    """Records off-session charges. No Stripe network; test mode only."""

    publishable_key = "pk_test"

    def __init__(self) -> None:
        self.topups: list[dict[str, object]] = []

    def create_off_session_topup(
        self, *, customer_id: str, credit_amount: int, user_id: str, idempotency_key: str
    ) -> tuple[str, str]:
        self.topups.append(
            {
                "customer_id": customer_id,
                "credit_amount": credit_amount,
                "user_id": user_id,
                "idempotency_key": idempotency_key,
            }
        )
        return (f"pi_{idempotency_key}", "succeeded")


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    """A Pro, opted-in caller with a saved card: the only shape that reaches Stripe."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'vt@example.com')"), {"o": _OWNER}
        )
        conn.execute(
            text(
                "INSERT INTO subscription "
                "(user_id, plan_code, status, stripe_customer_id, auto_topup_enabled) "
                "VALUES (:o, 'pro', 'active', 'cus_voice_topup', true)"
            ),
            {"o": _OWNER},
        )
    return migrated_engine


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:  # noqa: ARG001 — migrations first
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL (non-superuser role) not set")
    return make_rls_engine(app_url.replace("+asyncpg", "+psycopg"))


def _worker(app_engine: Engine, dispatch_engine: Engine, gateway: _FakeGateway) -> Worker:
    """The REAL A0 worker, with the auto-top-up tenant registered as worker_root does."""
    registry = JobRegistry()
    register_auto_topup_handler(
        registry,
        rls_engine=app_engine,
        gateway=gateway,  # type: ignore[arg-type]
    )
    return Worker(
        dispatch_engine=dispatch_engine,
        rls_engine=app_engine,
        registry=registry,
        worker_id="w-topup",
    )


def _enqueue_as_owner(app_engine: Engine, **kwargs: object) -> str | None:
    """Enqueue under the owner's RLS scope.

    In production the voice process owns a session engine already pinned to the caller
    (its checkout GUC). Here the app engine is the shared non-superuser one, so the scope
    is set explicitly around the write. RLS is genuinely enforced on this path: without
    the GUC the INSERT is refused, which is the behaviour we want to keep.
    """
    from persona_api.middleware.rls_context import current_user_id

    token = current_user_id.set(str(kwargs["owner_id"]))
    try:
        return enqueue_auto_topup(app_engine, **kwargs)  # type: ignore[arg-type]
    finally:
        current_user_id.reset(token)


def _queued(engine: Engine) -> list[dict[str, object]]:
    with engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT type, owner_id, idempotency_key, state FROM jobs "
                    "WHERE owner_id = :o ORDER BY created_at"
                ),
                {"o": _OWNER},
            )
            .mappings()
            .all()
        )
    return [dict(r) for r in rows]


# --- the full chain -----------------------------------------------------------


def test_voice_enqueue_drains_through_the_real_worker_and_charges(
    seeded: Engine, app_engine: Engine
) -> None:
    """The whole crossing: voice writes, the real worker claims, the charge happens.

    No hand-invoked handler anywhere. If the job never reaches the handler, the gateway
    records nothing and this fails, which is the exact production failure B5 exists to
    prevent.
    """
    gateway = _FakeGateway()
    worker = _worker(app_engine, seeded, gateway)

    # 1. VOICE side: the peer-process writer, over an owner-scoped engine.
    job_id = _enqueue_as_owner(
        app_engine,
        owner_id=_OWNER,
        old_balance=250,
        new_balance=150,  # a genuine crossing of the $2 line
        call_id=_CALL,
        turn_seq=7,
    )
    assert job_id is not None
    assert _queued(seeded)[0]["type"] == AUTO_TOPUP_JOB_TYPE

    # 2. API side: the REAL worker claims and processes exactly one job.
    assert asyncio.run(worker.run_once()) == 1

    # 3. The charge is a CONSEQUENCE of the drain, not of a direct call.
    assert len(gateway.topups) == 1
    assert gateway.topups[0]["user_id"] == _OWNER


def test_no_worker_drain_means_no_charge(seeded: Engine, app_engine: Engine) -> None:
    """The enqueue alone must not charge.

    Guards against a future refactor that "helpfully" charges at enqueue time, which
    would put Stripe credentials back in the voice process.
    """
    gateway = _FakeGateway()
    _worker(app_engine, seeded, gateway)  # registered but never run

    _enqueue_as_owner(
        app_engine,
        owner_id=_OWNER,
        old_balance=250,
        new_balance=150,
        call_id=_CALL,
        turn_seq=1,
    )
    assert gateway.topups == []


def test_a_non_crossing_drains_but_charges_nothing(seeded: Engine, app_engine: Engine) -> None:
    """The DECISION is the api's (D-M5-16).

    Voice reports balances without judging them, so a non-crossing pair still produces a
    job; the handler runs and correctly declines to charge. This is what keeps the
    threshold in one process.
    """
    gateway = _FakeGateway()
    worker = _worker(app_engine, seeded, gateway)

    _enqueue_as_owner(
        app_engine,
        owner_id=_OWNER,
        old_balance=150,
        new_balance=100,  # already below the line: not a crossing
        call_id=_CALL,
        turn_seq=2,
    )
    assert asyncio.run(worker.run_once()) == 1  # the job IS processed
    assert gateway.topups == []  # and correctly charges nothing


def test_an_ineligible_caller_drains_but_never_reaches_stripe(
    migrated_engine: Engine, app_engine: Engine
) -> None:
    """A Free user's crossing is evaluated and refused before any Stripe call."""
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'vf@example.com')"), {"o": _OWNER}
        )
        conn.execute(
            text(
                "INSERT INTO subscription (user_id, plan_code, status, auto_topup_enabled) "
                "VALUES (:o, 'free', 'active', true)"
            ),
            {"o": _OWNER},
        )
    gateway = _FakeGateway()
    worker = _worker(app_engine, migrated_engine, gateway)

    _enqueue_as_owner(
        app_engine,
        owner_id=_OWNER,
        old_balance=250,
        new_balance=150,
        call_id=_CALL,
        turn_seq=3,
    )
    assert asyncio.run(worker.run_once()) == 1
    assert gateway.topups == []


# --- dedup: the first of the four layers --------------------------------------


def test_a_re_fired_trigger_for_one_crossing_enqueues_once(
    seeded: Engine, app_engine: Engine
) -> None:
    """The meter can re-fire for one turn; the queue's ON CONFLICT collapses it.

    Layer one of four (D-M5-19). Without it a retried tick would queue a second job, and
    while the later layers would still prevent a second CHARGE, the queue would carry
    work that exists only to be discarded.
    """
    for _ in range(4):
        _enqueue_as_owner(
            app_engine,
            owner_id=_OWNER,
            old_balance=250,
            new_balance=150,
            call_id=_CALL,
            turn_seq=9,
        )
    assert len(_queued(seeded)) == 1


def test_a_different_turn_is_a_different_trigger(seeded: Engine, app_engine: Engine) -> None:
    """Dedup must not be so broad it swallows a genuinely later crossing."""
    _enqueue_as_owner(
        app_engine, owner_id=_OWNER, old_balance=250, new_balance=150, call_id=_CALL, turn_seq=1
    )
    _enqueue_as_owner(
        app_engine, owner_id=_OWNER, old_balance=250, new_balance=150, call_id=_CALL, turn_seq=2
    )
    assert len(_queued(seeded)) == 2


def test_repeated_drains_of_one_crossing_charge_once(seeded: Engine, app_engine: Engine) -> None:
    """At-least-once delivery: a re-run job must not produce a second charge.

    The outbound hourly key means both attempts resolve to the SAME PaymentIntent, so
    Stripe charges once and the webhook grants one lot.
    """
    gateway = _FakeGateway()
    worker = _worker(app_engine, seeded, gateway)

    _enqueue_as_owner(
        app_engine, owner_id=_OWNER, old_balance=250, new_balance=150, call_id=_CALL, turn_seq=5
    )
    assert asyncio.run(worker.run_once()) == 1

    # Re-deliver the same crossing as a fresh job (a later turn of the same episode).
    _enqueue_as_owner(
        app_engine, owner_id=_OWNER, old_balance=250, new_balance=150, call_id=_CALL, turn_seq=6
    )
    assert asyncio.run(worker.run_once()) == 1

    assert len({str(t["idempotency_key"]) for t in gateway.topups}) == 1


# --- anti-drift: the writer and the api table cannot diverge ------------------


def test_writer_columns_match_the_api_jobs_table(migrated_engine: Engine) -> None:
    """Bidirectional column parity, the A9-D-5 guard (D-M5-29).

    The voice writer is a SECOND writer of an api-owned table, on a money path. Its
    columns must be a subset of the real table's (no phantom column) AND a superset of
    the NOT-NULL-without-default columns (nothing required left unset). A renamed column
    and a newly-required column both fail here, statically, instead of at 3am.
    """
    with migrated_engine.begin() as conn:
        rows = (
            conn.execute(
                text(
                    "SELECT column_name, is_nullable, column_default "
                    "FROM information_schema.columns WHERE table_name = 'jobs'"
                )
            )
            .mappings()
            .all()
        )
    all_columns = {str(r["column_name"]) for r in rows}
    required = {
        str(r["column_name"])
        for r in rows
        if r["is_nullable"] == "NO" and r["column_default"] is None
    }

    assert all_columns >= INSERTED_COLUMNS, "the writer names a column the table lacks"
    assert required <= INSERTED_COLUMNS, "the table requires a column the writer omits"


def test_the_writer_and_the_handler_share_one_job_type(seeded: Engine, app_engine: Engine) -> None:
    """The row the writer lands is the type the registry serves (D-M5-29).

    Renaming the shared constant moves both sides together, so that alone cannot drift.
    What CAN drift is someone hand-writing the type string on one side. This asserts the
    enqueued row's ``type`` against the registry key the handler is registered under, so
    a hand-copied literal that diverges dead-letters here instead of in production.
    """
    gateway = _FakeGateway()
    registry = JobRegistry()
    register_auto_topup_handler(
        registry,
        rls_engine=app_engine,
        gateway=gateway,  # type: ignore[arg-type]
    )

    _enqueue_as_owner(
        app_engine, owner_id=_OWNER, old_balance=250, new_balance=150, call_id=_CALL, turn_seq=4
    )
    enqueued_type = str(_queued(seeded)[0]["type"])

    assert enqueued_type in registry.types(), (
        f"the writer enqueues {enqueued_type!r}, which the worker's registry cannot serve"
    )
