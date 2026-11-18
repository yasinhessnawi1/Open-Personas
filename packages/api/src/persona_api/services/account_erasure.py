"""Erasing an account: everything the user had, in one resumable ledger erasure (R9-284 T9).

D-R9-284-erase and D-R9-284-account-erase, cloud only. The account's ``erasures`` row is the
fence (the per-request upsert refuses its id from the moment it exists), and then, in order:

1. ``fence``: the owner's schedules and jobs, so nothing starts for it;
2. ``stripe``: the subscription cancelled at once, payment methods detached, the Customer's
   ``metadata.user_id`` replaced by the erasure id (the Customer and invoices are kept);
3. ``personas``: each persona through the persona erasure, unchanged, under its own ledger
   row and lock (so the graph mutex, write guard and node CAS hold as they do for one persona);
4. ``billing``: the paid rows copied to ``retained_billing_records`` under the erasure id;
5. ``audit``: the account's ``audit_log`` rows re-keyed to the erasure id, content keys gone;
6. ``clerk``: the Clerk user deleted, unless Clerk's own ``user.deleted`` started this;
7. ``row``: the ``users`` row, whose delete cascades through every table that names the owner,
   under the owner's graph maintenance lock so no consolidation interleaves with it;
8. ``late_writes``: the fence, the audit re-key and the row again, for anything in flight.

Every step is safe to repeat, so a failed erasure is resumed from the start, as the persona
erasure is.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from persona.graph.maintenance_lock import take_graph_maintenance_lock
from persona.logging import get_logger
from sqlalchemy import delete, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from persona_api.db.models import audit_log as audit_log_t
from persona_api.db.models import credit_transactions as credit_transactions_t
from persona_api.db.models import erasures as erasures_t
from persona_api.db.models import jobs as jobs_t
from persona_api.db.models import payg_grants as payg_grants_t
from persona_api.db.models import personas as personas_t
from persona_api.db.models import retained_billing_records as retained_t
from persona_api.db.models import schedules as schedules_t
from persona_api.db.models import subscription as subscription_t
from persona_api.db.models import users as users_t
from persona_api.errors import ErasureRecordError
from persona_api.schedules.store import ScheduleStore
from persona_api.services import audit_service
from persona_api.services.account_erasure_audit import (
    rekey_persona_audit_files,
    rekey_store_audit_file,
    rekey_store_audit_rows,
)
from persona_api.services.erasure_audit import scrub_setting_changes
from persona_api.services.erasure_ledger import Erasure, ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_lock import erasure_lock
from persona_api.services.erasure_service import (
    AUDIT_CONTENT_KEYS,
    PersonaEraseDeps,
    capture_persona,
    erase_persona,
)

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

__all__ = [
    "ORIGIN_CLERK",
    "ORIGIN_ROUTE",
    "PAID_REASONS",
    "STEPS",
    "AccountEraseDeps",
    "AccountErasurePendingError",
    "AccountErasureRaceError",
    "begin_account_erasure",
    "unready_reason",
    "erase_account",
]

_log = get_logger("api.erasure")

#: Who asked: our route (so the Clerk user is deleted too), or Clerk's ``user.deleted``.
ORIGIN_ROUTE = "route"
ORIGIN_CLERK = "clerk"

#: How long the row step waits for a lock (review L-7): a consolidation or a writer holding the
#: owner's rows makes it fail and back off rather than hang the runner's one thread.
ROW_LOCK_TIMEOUT = "30s"

#: ``credit_transactions`` reasons that record money paid (kept); usage rows go with the account.
PAID_REASONS = frozenset({"subscription_renewal", "payg_topup", "auto_topup"})


class StripeEraser(Protocol):
    def erase_customer(self, customer_id: str, *, erasure_id: str) -> dict[str, int]: ...


class ClerkDeleter(Protocol):
    def delete_user(self, user_id: str) -> bool: ...


@dataclass(frozen=True)
class AccountEraseDeps:
    """What an account erasure works through; ``persona.engine`` is the platform engine."""

    persona: PersonaEraseDeps
    stripe: StripeEraser | None
    clerk: ClerkDeleter | None
    retention: timedelta

    @property
    def engine(self) -> Engine:
        return self.persona.engine


class AccountErasurePendingError(RuntimeError):
    """A persona erasure the account waits on has not finished (another run holds it)."""


class AccountErasureRaceError(RuntimeError):
    """A persona appeared after the personas step; the next run erases it before the row."""


Step = Callable[[AccountEraseDeps, Erasure], dict[str, int]]


def _refuse(reason: str) -> ErasureRecordError:
    return ErasureRecordError("the account erasure was refused", context={"reason": reason})


def _ids(conn: Connection, stmt: Any) -> list[str]:  # noqa: ANN401 - a select
    return sorted(str(v) for v in conn.execute(stmt).scalars() if v)


def capture_account(conn: Connection, *, owner_id: str, origin: str) -> dict[str, list[str]]:
    """The ids later steps need, read while the rows exist.

    The subscription is snapshotted here, plan and start included (review M2): our own cancel
    makes Stripe send a webhook that rewrites the live row to the free plan, and the retained
    record must show what was paid for, so it is built from this snapshot, never the live row.
    """
    sub = subscription_t.c
    snapshot = conn.execute(
        select(sub.plan_code, sub.created_at).where(
            sub.user_id == owner_id,
            (sub.stripe_customer_id.is_not(None)) | (sub.stripe_subscription_id.is_not(None)),
        )
    ).first()
    return {
        "personas": _ids(conn, select(personas_t.c.id).where(personas_t.c.owner_id == owner_id)),
        "stripe_customers": _ids(
            conn, select(sub.stripe_customer_id).where(sub.user_id == owner_id)
        ),
        "stripe_subscriptions": _ids(
            conn, select(sub.stripe_subscription_id).where(sub.user_id == owner_id)
        ),
        "subscription_plan": [] if snapshot is None else [str(snapshot.plan_code)],
        "subscription_started": (
            []
            if snapshot is None or snapshot.created_at is None
            else [snapshot.created_at.isoformat()]
        ),
        "origin": [origin],
    }


def retained_subscription(erasure: Erasure) -> dict[str, Any] | None:
    """The paid subscription as it stood at the claim, or ``None`` when there was none.

    Its ``source_id`` is the fixed ``"subscription"``, so a resumed copy finds it and adds
    nothing, and nothing a later webhook writes to the live row can change it (review M2).
    """
    plan = erasure.captured.get("subscription_plan", [])
    if not plan:
        return None
    started = erasure.captured.get("subscription_started", [])
    return {
        "source_table": "subscription",
        "source_id": "subscription",
        "reason": plan[0],
        "stripe_subscription_id": next(
            iter(erasure.captured.get("stripe_subscriptions", [])), None
        ),
        "occurred_at": datetime.fromisoformat(started[0]) if started else None,
    }


def unready_reason(deps: AccountEraseDeps, *, owner_id: str, origin: str) -> str | None:
    """Why this process could not finish this account's erasure, or ``None`` when it can.

    Asked before anything is claimed (review H2), so a deletion is never accepted that the
    runner would only fail at the Clerk or Stripe step: the Clerk delete is needed when our
    route started it, and the Stripe eraser when the account has a Stripe customer.
    """
    if origin == ORIGIN_ROUTE and deps.clerk is None:
        return "clerk_not_configured"
    if deps.stripe is None:
        sub = subscription_t.c
        with deps.engine.connect() as conn:
            customer = conn.execute(
                select(sub.stripe_customer_id).where(
                    sub.user_id == owner_id, sub.stripe_customer_id.is_not(None)
                )
            ).first()
        if customer is not None:
            return "stripe_not_configured"
    return None


def begin_account_erasure(engine: Engine, *, owner_id: str, origin: str) -> Erasure:
    """Claim the account's erasure (or return the open one): from here its id is fenced."""
    if origin not in (ORIGIN_ROUTE, ORIGIN_CLERK):
        raise _refuse("unknown_origin")
    with engine.connect() as conn:
        captured = capture_account(conn, owner_id=owner_id, origin=origin)
    return ErasureLedger(engine).claim(
        owner_id=owner_id,
        subject_kind=SubjectKind.ACCOUNT,
        subject_id=owner_id,
        captured=captured,
    )


# ---- the steps --------------------------------------------------------------------------


def _step_fence(deps: AccountEraseDeps, erasure: Erasure) -> dict[str, int]:
    owner = erasure.owner_id
    with deps.engine.begin() as conn:
        schedule_ids = _ids(conn, select(schedules_t.c.id).where(schedules_t.c.owner_id == owner))
        schedules = ScheduleStore.delete_for_erasure(
            conn, owner_id=owner, schedule_ids=schedule_ids
        )
        jobs = conn.execute(delete(jobs_t).where(jobs_t.c.owner_id == owner)).rowcount
    return {"schedules": schedules, "jobs": int(jobs or 0)}


def _step_stripe(deps: AccountEraseDeps, erasure: Erasure) -> dict[str, int]:
    customers = erasure.captured.get("stripe_customers", [])
    totals: dict[str, int] = {"customers": len(customers)}
    if not customers:
        return totals
    if deps.stripe is None:
        raise _refuse("stripe_not_configured")
    for customer_id in customers:
        for name, n in deps.stripe.erase_customer(customer_id, erasure_id=erasure.id).items():
            totals[name] = totals.get(name, 0) + n
    return totals


def _erase_one_persona(deps: AccountEraseDeps, owner_id: str, persona_id: str) -> int:
    ledger = ErasureLedger(deps.engine)
    erasure = ledger.get(SubjectKind.PERSONA, persona_id)
    if erasure is None or erasure.state is ErasureState.COMPLETE:
        try:
            with deps.engine.connect() as conn:
                captured = capture_persona(conn, owner_id=owner_id, persona_id=persona_id)
        except ErasureRecordError:
            return 0  # the row is gone and no open erasure names it: nothing left here
        erasure = ledger.claim(
            owner_id=owner_id,
            subject_kind=SubjectKind.PERSONA,
            subject_id=persona_id,
            captured=captured,
        )
    erase_persona(deps.persona, erasure)
    done = ledger.get(SubjectKind.PERSONA, persona_id)
    if done is None or done.id != erasure.id or done.state is not ErasureState.COMPLETE:
        raise AccountErasurePendingError("a persona erasure has not finished")
    return 1


def _step_personas(deps: AccountEraseDeps, erasure: Erasure) -> dict[str, int]:
    owner = erasure.owner_id
    with deps.engine.connect() as conn:
        now = _ids(conn, select(personas_t.c.id).where(personas_t.c.owner_id == owner))
    if now:
        # Recorded, so the later steps (the audit re-key) know every persona this run met,
        # one created since the claim included (review round 3 MEDIUM-1).
        ErasureLedger(deps.engine).add_captured(erasure.id, "personas", now)
    ids = sorted(set(erasure.captured.get("personas", [])) | set(now))
    return {"personas": sum(_erase_one_persona(deps, owner, pid) for pid in ids)}


def personas_ever_owned(deps: AccountEraseDeps, erasure: Erasure) -> list[str]:
    """Every persona whose kept audit log may name the owner (review round 3 MEDIUM-1).

    The personas the claim captured, those the personas step met (recorded in the ledger),
    and every persona of this owner the ledger ever erased: one deleted before the account
    keeps its ``{persona}.jsonl`` too (ruling 5), with the owner as an author in it.
    """
    with deps.engine.connect() as conn:
        erased = _ids(
            conn,
            select(erasures_t.c.subject_id).where(
                erasures_t.c.subject_kind == SubjectKind.PERSONA.value,
                erasures_t.c.owner_id == erasure.owner_id,
            ),
        )
    return sorted(set(erasure.captured.get("personas", [])) | set(erased))


def _retained_rows(conn: Connection, erasure: Erasure, until: datetime) -> list[dict[str, Any]]:
    owner = erasure.owner_id
    customer = next(iter(erasure.captured.get("stripe_customers", [])), None)
    ct, pg = credit_transactions_t.c, payg_grants_t.c
    rows: list[dict[str, Any]] = [
        {
            "source_table": "credit_transactions",
            "source_id": r.id,
            "reason": r.reason,
            "credits": r.delta,
            "billing_key": r.billing_key,
            "cost_cents": r.cost_cents,
            "occurred_at": r.created_at,
        }
        for r in conn.execute(
            select(ct.id, ct.reason, ct.delta, ct.billing_key, ct.cost_cents, ct.created_at).where(
                ct.user_id == owner, ct.reason.in_(sorted(PAID_REASONS))
            )
        )
    ]
    rows += [
        {
            "source_table": "payg_grants",
            "source_id": r.id,
            "reason": "payg_grant",
            "credits": r.credits_total,
            "billing_key": r.source_billing_key,
            "occurred_at": r.granted_at,
        }
        for r in conn.execute(
            select(pg.id, pg.credits_total, pg.source_billing_key, pg.granted_at).where(
                pg.user_id == owner
            )
        )
    ]
    subscription = retained_subscription(erasure)
    if subscription is not None:
        rows.append(subscription)
    return [
        {**row, "erasure_id": erasure.id, "stripe_customer_id": customer, "retain_until": until}
        for row in rows
    ]


def _copy_billing(conn: Connection, erasure: Erasure, retention: timedelta) -> int:
    """Copy the paid rows into the retained table; how many were new (ON CONFLICT: repeatable).

    The retention runs from the erasure, the later of the two bounds.
    """
    rows = _retained_rows(conn, erasure, datetime.now(UTC) + retention)
    if not rows:
        return 0
    # Counted from RETURNING: the rowcount of a multi-row INSERT ... ON CONFLICT can come back
    # as -1 from the driver, which the ledger rightly refuses as a count (CI on 8913376a).
    return len(conn.execute(retained_insert(rows)).all())


#: Every column a retained row is written with (the table's own id and timestamps aside).
_RETAINED_COLUMNS = (
    "erasure_id",
    "source_table",
    "source_id",
    "reason",
    "credits",
    "billing_key",
    "cost_cents",
    "stripe_customer_id",
    "stripe_subscription_id",
    "occurred_at",
    "retain_until",
)


def retained_insert(rows: list[dict[str, Any]]) -> Any:  # noqa: ANN401 - an Insert
    """The INSERT for the retained rows, idempotent on (erasure, source table, source id).

    A multi-row VALUES needs every row to name the same columns: the three kinds of row carry
    different fields (a PAYG grant has no cost, a subscription no credits), so each is given
    every column, ``None`` where it has none. Without that Postgres refuses the statement at
    compile time (the CI failure on 030caf43).
    """
    full = [{name: row.get(name) for name in _RETAINED_COLUMNS} for row in rows]
    return (
        pg_insert(retained_t)
        .values(full)
        .on_conflict_do_nothing(index_elements=["erasure_id", "source_table", "source_id"])
        .returning(retained_t.c.id)
    )


def _step_billing(deps: AccountEraseDeps, erasure: Erasure) -> dict[str, int]:
    with deps.engine.begin() as conn:
        return {"retained": _copy_billing(conn, erasure, deps.retention)}


def _step_audit(deps: AccountEraseDeps, erasure: Erasure) -> dict[str, int]:
    audit = audit_log_t.c
    scrubbed = 0
    with deps.engine.begin() as conn:
        for audit_id, meta in conn.execute(
            select(audit.id, audit.metadata).where(audit.user_id == erasure.owner_id)
        ).all():
            clean = meta if isinstance(meta, Mapping) else {}
            kept = {k: v for k, v in clean.items() if k not in AUDIT_CONTENT_KEYS}
            if kept != clean:
                conn.execute(update(audit_log_t).where(audit.id == audit_id).values(metadata=kept))
                scrubbed += 1
        # PR #44 review MEDIUM-2: before the re-key, the control's audit loses what it names.
        settings = scrub_setting_changes(conn, owner_id=erasure.owner_id)
        rekeyed = conn.execute(
            update(audit_log_t).where(audit.user_id == erasure.owner_id).values(user_id=erasure.id)
        ).rowcount
        # Review M1: the graph's store-audit events carry the owner's id as their persona id.
        store_rekeyed = rekey_store_audit_rows(
            conn, owner_id=erasure.owner_id, erasure_id=erasure.id
        )
    store_lines = rekey_store_audit_file(
        deps.persona.audit_root, owner_id=erasure.owner_id, erasure_id=erasure.id
    ) + rekey_persona_audit_files(
        deps.persona.audit_root,
        personas_ever_owned(deps, erasure),
        owner_id=erasure.owner_id,
        erasure_id=erasure.id,
    )
    return {
        "rekeyed": int(rekeyed or 0),
        "scrubbed": scrubbed,
        "settings_scrubbed": settings,
        "store_rekeyed": store_rekeyed,
        "store_lines": store_lines,
    }


def _step_clerk(deps: AccountEraseDeps, erasure: Erasure) -> dict[str, int]:
    if erasure.captured.get("origin") == [ORIGIN_CLERK]:
        return {"deleted": 0}  # Clerk deleted the user itself: that is how this began
    if deps.clerk is None:
        raise _refuse("clerk_not_configured")
    return {"deleted": int(deps.clerk.delete_user(erasure.owner_id))}


def _step_row(deps: AccountEraseDeps, erasure: Erasure) -> dict[str, int]:
    """Delete the ``users`` row, and with it every row that names the owner (the cascade).

    In one transaction: the owner's graph maintenance lock first (no consolidation interleaves
    with the cascade), then the users row ``FOR UPDATE``. A persona insert needs a key share on
    that row, so from here none can commit; the personas are listed again under the lock, and
    if one was created after the personas step it is not cascaded away with its memory, audit
    and files left behind: the step raises, and the resumed run erases it first.
    """
    owner = erasure.owner_id
    with deps.engine.begin() as conn:
        if conn.dialect.name == "postgresql":
            conn.execute(text(f"SET LOCAL lock_timeout = '{ROW_LOCK_TIMEOUT}'"))
        take_graph_maintenance_lock(conn, owner)
        held = conn.execute(
            select(users_t.c.id).where(users_t.c.id == owner).with_for_update()
        ).first()
        if held is None:
            return {"users": 0}
        if _ids(conn, select(personas_t.c.id).where(personas_t.c.owner_id == owner)):
            raise AccountErasureRaceError("a persona was created during the erasure")
        # Review M3: a paid row written after the billing step (an auto top-up that landed in
        # between) is copied here, under the same lock, before the cascade would take it.
        retained = _copy_billing(conn, erasure, deps.retention)
        users = conn.execute(delete(users_t).where(users_t.c.id == owner)).rowcount
    return {"users": int(users or 0), "retained": retained}


def _step_late_writes(deps: AccountEraseDeps, erasure: Erasure) -> dict[str, int]:
    # The stripe step again, after the row is gone (review M4): a Checkout that completed or a
    # subscription that appeared while the erasure ran is expired or cancelled now.
    return {
        **_step_fence(deps, erasure),
        **_step_audit(deps, erasure),
        **_step_row(deps, erasure),
        **_step_stripe(deps, erasure),
    }


#: Steps that call another service (Stripe, Clerk). On a resume, one already recorded done is
#: not called again (review L4); late_writes still re-runs the Stripe step on purpose.
EXTERNAL_STEPS = frozenset({"stripe", "clerk"})

STEPS: tuple[tuple[str, Step], ...] = (
    ("fence", _step_fence),
    ("stripe", _step_stripe),
    ("personas", _step_personas),
    ("billing", _step_billing),
    ("audit", _step_audit),
    ("clerk", _step_clerk),
    ("row", _step_row),
    ("late_writes", _step_late_writes),
)


# ---- the runner -------------------------------------------------------------------------


def _flatten(counts: Mapping[str, Mapping[str, int]]) -> dict[str, str]:
    return {f"{step}_{name}": str(n) for step, named in counts.items() for name, n in named.items()}


def erase_account(deps: AccountEraseDeps, erasure: Erasure) -> None:
    """Run every step, record its counts, complete the erasure and audit it.

    A no-op for an erasure that has finished, and for one another run holds the lock on. Raises
    what a step raised, after marking the erasure failed, so the runner can resume it later.
    """
    if erasure.subject_kind is not SubjectKind.ACCOUNT:
        raise _refuse("not_an_account_erasure")
    with erasure_lock(deps.engine, erasure.id) as held:
        if not held:
            _log.info("account erasure already running elsewhere erasure={eid}", eid=erasure.id)
            return
        _erase_holding_the_lock(deps, erasure)


def _erase_holding_the_lock(deps: AccountEraseDeps, erasure: Erasure) -> None:
    ledger = ErasureLedger(deps.engine)
    current = ledger.get(SubjectKind.ACCOUNT, erasure.subject_id)
    if current is None or current.id != erasure.id or current.state is ErasureState.COMPLETE:
        return
    for name, step in STEPS:
        current = ledger.get(SubjectKind.ACCOUNT, erasure.subject_id) or current
        if name in EXTERNAL_STEPS and name in current.counts:
            continue  # done on an earlier run: not called again
        try:
            counts = step(deps, current)
        except Exception as exc:
            _log.warning(
                "account erasure step failed erasure={eid} step={step} error={err}",
                eid=current.id,
                step=name,
                err=type(exc).__name__,
            )
            ledger.fail(current.id, f"{name}_failed")
            raise
        ledger.record_step(current.id, name, counts)
    ledger.complete(current.id)
    done = ledger.get(SubjectKind.ACCOUNT, current.subject_id)
    # Keyed by the erasure id, not the user's: the account it describes no longer exists.
    audit_service.record(
        engine=deps.engine,
        user_id=current.id,
        action="account.erase",
        target=current.id,
        metadata=_flatten(done.counts if done is not None else {}),
    )
