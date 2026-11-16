"""The account fence on Postgres: an erased id is never provisioned again (R9-284 T9).

D-R9-284-account-erase: the per-request upsert refuses an id with any account erasure, open or
finished, and leaves an existing row untouched while the erasure runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona_api.errors import AccountErasedError
from persona_api.services.erasure_ledger import ErasureLedger, SubjectKind
from persona_api.services.user_service import account_erased, ensure_user
from sqlalchemy import text

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration


def _user(engine: Engine, user_id: str) -> dict[str, object] | None:
    with engine.connect() as conn:
        row = (
            conn.execute(text("SELECT id, first_name FROM users WHERE id = :id"), {"id": user_id})
            .mappings()
            .one_or_none()
        )
    return None if row is None else dict(row)


def _erase(engine: Engine, user_id: str, *, finish: bool) -> None:
    ledger = ErasureLedger(engine)
    erasure = ledger.claim(
        owner_id=user_id, subject_kind=SubjectKind.ACCOUNT, subject_id=user_id, captured={}
    )
    if finish:
        ledger.complete(erasure.id)


def test_a_live_id_is_provisioned(migrated_engine: Engine) -> None:
    ensure_user(migrated_engine, user_id="user_live", email="live@x.test", first_name="Ann")

    assert _user(migrated_engine, "user_live") == {"id": "user_live", "first_name": "Ann"}


def test_an_id_being_erased_is_refused_and_its_row_left_alone(migrated_engine: Engine) -> None:
    ensure_user(migrated_engine, user_id="user_open", email="open@x.test")
    _erase(migrated_engine, "user_open", finish=False)

    with pytest.raises(AccountErasedError):
        ensure_user(migrated_engine, user_id="user_open", email="open@x.test", first_name="Bo")

    assert _user(migrated_engine, "user_open") == {"id": "user_open", "first_name": None}


def test_an_erased_id_is_never_provisioned_again(migrated_engine: Engine) -> None:
    _erase(migrated_engine, "user_done", finish=True)

    with pytest.raises(AccountErasedError):
        ensure_user(migrated_engine, user_id="user_done", email="done@x.test")

    assert _user(migrated_engine, "user_done") is None


def test_a_persona_erasure_with_the_same_id_does_not_fence_the_account(
    migrated_engine: Engine,
) -> None:
    ErasureLedger(migrated_engine).claim(
        owner_id="someone", subject_kind=SubjectKind.PERSONA, subject_id="user_x", captured={}
    )

    ensure_user(migrated_engine, user_id="user_x", email="x@x.test")

    assert _user(migrated_engine, "user_x") is not None


def test_the_fence_probe_uses_the_subject_index(migrated_engine: Engine) -> None:
    with migrated_engine.connect() as conn:
        conn.execute(text("SET LOCAL enable_seqscan = off"))
        plan = "\n".join(
            conn.execute(
                text(
                    "EXPLAIN SELECT 1 FROM erasures WHERE subject_kind = 'account' "
                    "AND subject_id = 'user_y' LIMIT 1"
                )
            ).scalars()
        )
    assert "idx_erasures_subject" in plan


def test_the_shared_fence_query_answers_for_open_and_finished_erasures(
    migrated_engine: Engine,
) -> None:
    """Review L6: the connector service's check, the same probe as the upsert's."""
    assert account_erased(migrated_engine, "user_q") is False
    _erase(migrated_engine, "user_q", finish=False)
    assert account_erased(migrated_engine, "user_q") is True
    _erase(migrated_engine, "user_r", finish=True)
    assert account_erased(migrated_engine, "user_r") is True
