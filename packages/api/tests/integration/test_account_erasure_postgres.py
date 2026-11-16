"""Erasing an account on Postgres, end to end (R9-284 T9, D-R9-284-account-erase).

Seeds two owners (alice with two personas, bob with one), alice's billing and audit rows, and
erases alice's account as production does: on the ``persona_platform`` engine (BYPASSRLS, not a
superuser), with Stripe and Clerk faked. Then: nothing names alice in any owner- or user-keyed
table, her personas went through the persona erasure, her paid rows are retained under the
erasure id and her usage rows are gone, her audit rows are re-keyed and scrubbed, the id is
fenced for good, and bob keeps everything. Runs in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import pytest
from _erase_seed import OTHER_OWNER, OWNER, PERSONA, SIBLING, STRANGER, Seeded, row, seed_into
from persona.stores.postgres import PostgresBackend
from persona_api.db.models import metadata
from persona_api.errors import AccountErasedError
from persona_api.services import account_erasure as ae
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_service import PersonaEraseDeps
from persona_api.services.user_service import ensure_user
from sqlalchemy import create_engine, event, text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

_GRANTS = (
    "GRANT USAGE ON SCHEMA public TO persona_platform",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO persona_platform",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO persona_platform",
)
_RETENTION = timedelta(days=366 * 5)


@pytest.fixture
def platform(migrated_engine: Engine, database_url: str) -> Iterator[Engine]:
    """``persona_platform`` (BYPASSRLS, not a superuser), as the erasure runs in production."""
    with migrated_engine.begin() as conn:
        if (
            conn.execute(text("SELECT 1 FROM pg_roles WHERE rolname = 'persona_platform'")).first()
            is None
        ):
            conn.execute(text("CREATE ROLE persona_platform NOLOGIN BYPASSRLS"))
        conn.execute(text("GRANT persona_platform TO CURRENT_USER"))
        for grant in _GRANTS:
            conn.execute(text(grant))
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def _become_platform(dbapi_connection: Any, _record: Any) -> None:  # noqa: ANN401
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET ROLE persona_platform")

    yield engine
    engine.dispose()


@pytest.fixture
def seeded(migrated_engine: Engine, embedder: object, tmp_path: Path) -> Seeded:
    memory = PostgresBackend(engine=migrated_engine, embedder=embedder)  # type: ignore[arg-type]
    seeded = seed_into(migrated_engine, memory, tmp_path)
    with migrated_engine.begin() as conn:
        row(
            conn,
            "subscription",
            user_id=OWNER,
            plan_code="pro",
            stripe_customer_id="cus_alice",
            stripe_subscription_id="sub_alice",
        )
        row(
            conn,
            "credit_transactions",
            id="ct_paid",
            user_id=OWNER,
            delta=1000,
            reason="payg_topup",
            billing_key="bk_alice",
            cost_cents=1000.0,
        )
        row(conn, "credit_transactions", id="ct_usage", user_id=OWNER, delta=-5, reason="chat")
        row(
            conn,
            "payg_grants",
            id="pg_alice",
            user_id=OWNER,
            credits_total=1000,
            credits_remaining=900,
            source_billing_key="bk_alice",
            expires_at=datetime.now(UTC) + timedelta(days=365),
        )
        row(
            conn,
            "credit_transactions",
            id="ct_bob",
            user_id=OTHER_OWNER,
            delta=1000,
            reason="payg_topup",
            billing_key="bk_bob",
        )
        row(
            conn,
            "audit_log",
            id="au_alice",
            user_id=OWNER,
            action="ask",
            target="x",
            metadata={"verbatim_ask": "what the user typed", "kept": "label"},
        )
    return seeded


class _Stripe:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def erase_customer(self, customer_id: str, *, erasure_id: str) -> dict[str, int]:
        self.calls.append((customer_id, erasure_id))
        return {"subscriptions_cancelled": 1, "payment_methods_detached": 1}


class _Clerk:
    def __init__(self, *, fail: bool = False) -> None:
        self.deleted: list[str] = []
        self.fail = fail

    def delete_user(self, user_id: str) -> bool:
        if self.fail:
            raise RuntimeError("clerk is down")
        self.deleted.append(user_id)
        return True


def _deps(seeded: Seeded, platform: Engine, embedder: Any, **kw: Any) -> ae.AccountEraseDeps:  # noqa: ANN401
    return ae.AccountEraseDeps(
        persona=PersonaEraseDeps(
            engine=platform,
            memory_backend=PostgresBackend(engine=platform, embedder=embedder),
            audit_logger=seeded.audit,
            file_storage=seeded.storage,
            workspace_root=seeded.workspace_root,
            audit_root=seeded.audit_root,
        ),
        stripe=kw.get("stripe", _Stripe()),
        clerk=kw.get("clerk", _Clerk()),
        retention=_RETENTION,
    )


def _naming(engine: Engine, owner: str) -> dict[str, int]:
    """Rows in every owner- or user-keyed table that still name ``owner`` (the ledger aside)."""
    found: dict[str, int] = {}
    with engine.connect() as conn:
        for table in metadata.sorted_tables:
            if table.name == "erasures":
                continue
            for column in ("owner_id", "user_id"):
                if column in table.columns:
                    n = conn.execute(
                        text(f"SELECT count(*) FROM {table.name} WHERE {column} = :o"),  # noqa: S608
                        {"o": owner},
                    ).scalar_one()
                    if n:
                        found[f"{table.name}.{column}"] = int(n)
        users = conn.execute(
            text("SELECT count(*) FROM users WHERE id = :o"), {"o": owner}
        ).scalar_one()
    if users:
        found["users.id"] = int(users)
    return found


def _retained(engine: Engine) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [
            dict(r)
            for r in conn.execute(
                text("SELECT * FROM retained_billing_records ORDER BY source_table, source_id")
            ).mappings()
        ]


def test_nothing_of_the_account_remains_and_the_neighbour_keeps_all(
    seeded: Seeded,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    bob_before = _naming(seeded.engine, OTHER_OWNER)
    stripe, clerk = _Stripe(), _Clerk()
    erasure = ae.begin_account_erasure(platform, owner_id=OWNER, origin=ae.ORIGIN_ROUTE)

    ae.erase_account(_deps(seeded, platform, embedder, stripe=stripe, clerk=clerk), erasure)

    assert _naming(seeded.engine, OWNER) == {}, "nothing names the erased account"
    assert _naming(seeded.engine, OTHER_OWNER) == bob_before, "the neighbour keeps all"
    ledger = ErasureLedger(seeded.engine)
    done = ledger.get(SubjectKind.ACCOUNT, OWNER)
    assert done is not None
    assert done.state is ErasureState.COMPLETE
    for pid in (PERSONA, SIBLING):
        persona = ledger.get(SubjectKind.PERSONA, pid)
        assert persona is not None
        assert persona.state is ErasureState.COMPLETE, pid
    assert ledger.get(SubjectKind.PERSONA, STRANGER) is None
    assert stripe.calls == [("cus_alice", erasure.id)]
    assert clerk.deleted == [OWNER]


def test_paid_rows_are_retained_under_the_erasure_id_and_usage_goes(
    seeded: Seeded,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    erasure = ae.begin_account_erasure(platform, owner_id=OWNER, origin=ae.ORIGIN_ROUTE)

    ae.erase_account(_deps(seeded, platform, embedder), erasure)

    kept = _retained(seeded.engine)
    assert {(r["source_table"], r["source_id"]) for r in kept} == {
        ("credit_transactions", "ct_paid"),
        ("payg_grants", "pg_alice"),
        ("subscription", "sub_alice"),
    }
    for r in kept:
        assert r["erasure_id"] == erasure.id
        assert r["stripe_customer_id"] == "cus_alice"
        assert r["retain_until"] >= datetime.now(UTC) + timedelta(days=365 * 5)
        assert OWNER not in str(r), "no column carries the user id"
    with seeded.engine.connect() as conn:
        bob = conn.execute(
            text("SELECT count(*) FROM credit_transactions WHERE id = 'ct_bob'")
        ).scalar_one()
    assert bob == 1


def test_audit_rows_are_rekeyed_and_scrubbed(
    seeded: Seeded,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    erasure = ae.begin_account_erasure(platform, owner_id=OWNER, origin=ae.ORIGIN_ROUTE)

    ae.erase_account(_deps(seeded, platform, embedder), erasure)

    with seeded.engine.connect() as conn:
        mine = conn.execute(
            text("SELECT user_id, metadata FROM audit_log WHERE id = 'au_alice'")
        ).one()
        completion = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'account.erase' AND user_id = :e"),
            {"e": erasure.id},
        ).scalar_one()
    assert mine.user_id == erasure.id
    assert mine.metadata == {"kept": "label"}
    assert completion == 1


def test_the_erased_id_is_fenced_for_good(
    seeded: Seeded,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    erasure = ae.begin_account_erasure(platform, owner_id=OWNER, origin=ae.ORIGIN_ROUTE)
    with pytest.raises(AccountErasedError):  # from the claim on, before any step ran
        ensure_user(seeded.engine, user_id=OWNER, email="alice@x.test")

    ae.erase_account(_deps(seeded, platform, embedder), erasure)

    with pytest.raises(AccountErasedError):
        ensure_user(seeded.engine, user_id=OWNER, email="alice@x.test")
    assert _naming(seeded.engine, OWNER) == {}


def test_a_failed_erasure_resumes_to_the_same_end_without_doubling_anything(
    seeded: Seeded,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    erasure = ae.begin_account_erasure(platform, owner_id=OWNER, origin=ae.ORIGIN_ROUTE)
    with pytest.raises(RuntimeError):
        ae.erase_account(_deps(seeded, platform, embedder, clerk=_Clerk(fail=True)), erasure)
    failed = ErasureLedger(seeded.engine).get(SubjectKind.ACCOUNT, OWNER)
    assert failed is not None
    assert failed.state is ErasureState.FAILED
    with seeded.engine.connect() as conn:
        still = conn.execute(
            text("SELECT count(*) FROM users WHERE id = :o"), {"o": OWNER}
        ).scalar_one()
    assert still == 1, "the users row is never deleted past a failed step"

    ae.erase_account(_deps(seeded, platform, embedder), failed)

    assert _naming(seeded.engine, OWNER) == {}
    assert len(_retained(seeded.engine)) == 3, "the resumed copy added nothing twice"


def test_clerks_own_deletion_skips_the_clerk_call(
    seeded: Seeded,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    clerk = _Clerk()
    erasure = ae.begin_account_erasure(platform, owner_id=OWNER, origin=ae.ORIGIN_CLERK)

    ae.erase_account(_deps(seeded, platform, embedder, clerk=clerk), erasure)

    assert clerk.deleted == []
    assert _naming(seeded.engine, OWNER) == {}
