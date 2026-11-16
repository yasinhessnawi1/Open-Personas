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
        row(
            conn,
            "store_audit_events",
            id="sa_alice",
            persona_id=OWNER,
            action="write",
            store="graph",
            source="system",
            written_by=PERSONA,
            reason="the model's words",
            timestamp=datetime.now(UTC),
            metadata={"verbatim_ask": "typed", "label": "kept"},
        )
        row(
            conn,
            "store_audit_events",
            id="sa_author",
            persona_id=PERSONA,
            action="write",
            store="identity",
            source="user",
            written_by=OWNER,
            reason="initial persona load",
            timestamp=datetime.now(UTC),
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
        # Review M1: the graph's store-audit events carry the owner's id as their persona id.
        store = conn.execute(
            text("SELECT count(*) FROM store_audit_events WHERE persona_id = :o"), {"o": owner}
        ).scalar_one()
    if users:
        found["users.id"] = int(users)
    if store:
        found["store_audit_events.persona_id"] = int(store)
    return found


def _anywhere(engine: Engine, needle: str) -> dict[str, int]:
    """Every column of every table (the ledger aside) whose text holds ``needle``: ids inside
    node ids, JSON, trails, anything. Cheap on a test database."""
    found: dict[str, int] = {}
    with engine.connect() as conn:
        for table in metadata.sorted_tables:
            if table.name == "erasures":
                continue
            for column in table.columns:
                n = conn.execute(
                    text(
                        f'SELECT count(*) FROM {table.name} WHERE CAST("{column.name}" AS text) '  # noqa: S608
                        "LIKE :pattern"
                    ),
                    {"pattern": f"%{needle}%"},
                ).scalar_one()
                if n:
                    found[f"{table.name}.{column.name}"] = int(n)
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
    assert _anywhere(seeded.engine, OWNER) == {}, "the id is nowhere, not even inside a value"
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
    # The stripe step, then again in late_writes after the row is gone (review M4).
    assert stripe.calls == [("cus_alice", erasure.id)] * 2
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
        ("subscription", "subscription"),  # the fixed id of the claim snapshot (M2)
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


_LATE = "persona_dddddddddddddddddddddddddddddddd"


def test_a_persona_created_during_the_erasure_is_erased_before_the_account_row(
    seeded: Seeded,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review H1: a persona committed after the personas step must not be cascaded away with
    its memory, audit and files left behind. The row step finds it under the users-row lock and
    stops; the resumed run erases it through the persona erasure, then deletes the row."""
    real = dict(ae.STEPS)

    def _billing_then_a_new_persona(deps: ae.AccountEraseDeps, erasure: Any) -> dict[str, int]:  # noqa: ANN401
        with seeded.engine.begin() as conn:
            row(conn, "personas", id=_LATE, owner_id=OWNER)
        return real["billing"](deps, erasure)

    monkeypatch.setattr(
        ae,
        "STEPS",
        tuple((n, _billing_then_a_new_persona if n == "billing" else s) for n, s in ae.STEPS),
    )
    erasure = ae.begin_account_erasure(platform, owner_id=OWNER, origin=ae.ORIGIN_ROUTE)

    with pytest.raises(ae.AccountErasureRaceError):
        ae.erase_account(_deps(seeded, platform, embedder), erasure)
    with seeded.engine.connect() as conn:
        still = conn.execute(
            text("SELECT count(*) FROM users WHERE id = :o"), {"o": OWNER}
        ).scalar_one()
    assert still == 1, "the row was not deleted with the new persona under it"

    monkeypatch.setattr(ae, "STEPS", tuple(real.items()))
    failed = ErasureLedger(seeded.engine).get(SubjectKind.ACCOUNT, OWNER)
    assert failed is not None
    ae.erase_account(_deps(seeded, platform, embedder), failed)

    late = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, _LATE)
    assert late is not None
    assert late.state is ErasureState.COMPLETE, "it went through the persona erasure"
    assert _naming(seeded.engine, OWNER) == {}


def test_the_owners_store_audit_is_rekeyed_and_scrubbed(
    seeded: Seeded,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Review M1: the graph's events ride the owner's id as their persona id."""
    erasure = ae.begin_account_erasure(platform, owner_id=OWNER, origin=ae.ORIGIN_ROUTE)

    ae.erase_account(_deps(seeded, platform, embedder), erasure)

    with seeded.engine.connect() as conn:
        mine = conn.execute(
            text(
                "SELECT persona_id, reason, metadata FROM store_audit_events WHERE id = 'sa_alice'"
            )
        ).one()
    assert mine.persona_id == erasure.id
    assert mine.reason is None
    assert mine.metadata == {"label": "kept"}
    assert not (seeded.audit_root / f"{OWNER}.jsonl").exists()


def test_a_paid_row_written_between_billing_and_row_is_retained(
    seeded: Seeded,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review M3: an auto top-up landing after the billing step is not lost to the cascade."""
    real = dict(ae.STEPS)

    def _audit_after_a_top_up(deps: ae.AccountEraseDeps, erasure: Any) -> dict[str, int]:  # noqa: ANN401
        with seeded.engine.begin() as conn:
            row(
                conn,
                "credit_transactions",
                id="ct_late",
                user_id=OWNER,
                delta=1000,
                reason="auto_topup",
                billing_key="bk_late",
            )
        return real["audit"](deps, erasure)

    monkeypatch.setattr(
        ae, "STEPS", tuple((n, _audit_after_a_top_up if n == "audit" else s) for n, s in ae.STEPS)
    )
    erasure = ae.begin_account_erasure(platform, owner_id=OWNER, origin=ae.ORIGIN_ROUTE)

    ae.erase_account(_deps(seeded, platform, embedder), erasure)

    kept = {(r["source_table"], r["source_id"]) for r in _retained(seeded.engine)}
    assert ("credit_transactions", "ct_late") in kept
    assert _naming(seeded.engine, OWNER) == {}


def test_no_store_audit_column_names_the_account(
    seeded: Seeded,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Review M-1: the owner as written_by (an initial persona load, a tool consent)."""
    erasure = ae.begin_account_erasure(platform, owner_id=OWNER, origin=ae.ORIGIN_ROUTE)

    ae.erase_account(_deps(seeded, platform, embedder), erasure)

    with seeded.engine.connect() as conn:
        naming = conn.execute(
            text(
                "SELECT count(*) FROM store_audit_events WHERE persona_id = :o OR written_by = :o"
            ),
            {"o": OWNER},
        ).scalar_one()
    assert naming == 0


_OLD = "persona_eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"


def test_no_kept_audit_file_names_the_account(
    seeded: Seeded,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    """Review round 3 MEDIUM-1: a persona deleted before the account keeps its JSONL log,
    with the owner as an author in it; the account erasure re-keys that one too."""
    import json  # noqa: PLC0415

    old = ErasureLedger(seeded.engine).claim(
        owner_id=OWNER, subject_kind=SubjectKind.PERSONA, subject_id=_OLD, captured={}
    )
    ErasureLedger(seeded.engine).complete(old.id)
    event = {"persona_id": _OLD, "written_by": OWNER, "action": "write", "store": "identity"}
    (seeded.audit_root / f"{_OLD}.jsonl").write_text(json.dumps(event) + "\n", encoding="utf-8")
    erasure = ae.begin_account_erasure(platform, owner_id=OWNER, origin=ae.ORIGIN_ROUTE)

    ae.erase_account(_deps(seeded, platform, embedder), erasure)

    naming = [
        path.name
        for path in seeded.audit_root.rglob("*")
        if path.is_file() and OWNER.encode() in path.read_bytes()
    ]
    assert naming == [], "no kept audit file names the account"
