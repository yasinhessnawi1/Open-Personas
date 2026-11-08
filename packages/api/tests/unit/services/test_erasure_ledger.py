"""The erasure ledger: what is being erased, how far it got, in ids and counts (R9-284).

Deleting a persona or an account takes many steps across the database, stored files and
other stores, and any of them can fail. The ledger is what makes that resumable and provable:
one row per subject, written before anything is removed, outliving the persona and the user
it names. It holds ids, step names and counts, and refuses anything shaped like content.

Runs on the community engine, the same SQL as Postgres for everything this module does; the
RLS policy, the migration and the row outliving its user are pinned on Postgres in
test_migration_061_erasures.py.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from persona_api.db.community import create_community_schema, make_community_engine
from persona_api.db.models import erasures as erasures_t
from persona_api.errors import ErasureRecordError
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from sqlalchemy import insert, update
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

_OWNER = "user_alice"
_PERSONA = "persona_aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
_CAPTURED = {"conversations": ["c1", "c2"], "tasks": ["t1"]}


@pytest.fixture
def engine(tmp_path: Path) -> Engine:
    eng = make_community_engine(tmp_path / "community.db")
    create_community_schema(eng)
    return eng


@pytest.fixture
def ledger(engine: Engine) -> ErasureLedger:
    return ErasureLedger(engine)


def _claim(ledger: ErasureLedger) -> str:
    return ledger.claim(
        owner_id=_OWNER,
        subject_kind=SubjectKind.PERSONA,
        subject_id=_PERSONA,
        captured=_CAPTURED,
    ).id


def test_a_claim_records_the_subject_before_anything_is_removed(ledger: ErasureLedger) -> None:
    row = ledger.claim(
        owner_id=_OWNER,
        subject_kind=SubjectKind.PERSONA,
        subject_id=_PERSONA,
        captured=_CAPTURED,
    )
    assert row.owner_id == _OWNER
    assert row.subject_kind is SubjectKind.PERSONA
    assert row.subject_id == _PERSONA
    assert row.state is ErasureState.PENDING
    assert row.captured == _CAPTURED
    assert row.counts == {}
    assert row.error_code is None
    assert row.completed_at is None
    assert row.requested_at is not None


def test_a_second_claim_is_the_same_erasure_and_keeps_what_was_captured(
    ledger: ErasureLedger,
) -> None:
    """The ids were read while the rows existed; a retry after they are gone sees nothing."""
    first = _claim(ledger)
    again = ledger.claim(
        owner_id=_OWNER,
        subject_kind=SubjectKind.PERSONA,
        subject_id=_PERSONA,
        captured={"conversations": []},
    )
    assert again.id == first
    assert again.captured == _CAPTURED


def test_the_same_id_as_a_different_kind_is_a_different_erasure(ledger: ErasureLedger) -> None:
    first = _claim(ledger)
    other = ledger.claim(
        owner_id=_OWNER, subject_kind=SubjectKind.ACCOUNT, subject_id=_PERSONA, captured={}
    )
    assert other.id != first


def test_a_step_records_its_counts_and_the_erasure_is_running(ledger: ErasureLedger) -> None:
    erasure = _claim(ledger)
    ledger.record_step(erasure, "memory", {"episodic": 3, "episodic_gist": 1})
    ledger.record_step(erasure, "files", {"objects": 7})

    row = ledger.get(SubjectKind.PERSONA, _PERSONA)
    assert row is not None
    assert row.state is ErasureState.RUNNING
    assert row.counts == {"memory": {"episodic": 3, "episodic_gist": 1}, "files": {"objects": 7}}


def test_a_resumed_step_adds_what_it_removed_and_a_rerun_that_found_nothing_keeps_the_count(
    ledger: ErasureLedger,
) -> None:
    """The doors report rows really removed, so adding is right, and a 0 must not erase a 3."""
    erasure = _claim(ledger)
    ledger.record_step(erasure, "memory", {"episodic": 3, "episodic_gist": 1})
    ledger.record_step(erasure, "memory", {"episodic": 2, "core_memory": 4})
    ledger.record_step(erasure, "memory", {"episodic": 0})

    row = ledger.get(SubjectKind.PERSONA, _PERSONA)
    assert row is not None
    assert row.counts == {"memory": {"episodic": 5, "episodic_gist": 1, "core_memory": 4}}


@pytest.mark.parametrize(
    "counts",
    [
        {"memory": "my dog is called Balto"},
        {"memory": -1},
        {"memory": True},
        {"memory": 1.5},
        {"what the user said": 1},
    ],
)
def test_counts_are_numbers_under_plain_names(
    ledger: ErasureLedger, counts: dict[str, object]
) -> None:
    erasure = _claim(ledger)
    with pytest.raises(ErasureRecordError):
        ledger.record_step(erasure, "memory", counts)  # type: ignore[arg-type]


@pytest.mark.parametrize("step", ["", "two words", "Memory", "x" * 65, "a\nb"])
def test_a_step_name_is_a_plain_name(ledger: ErasureLedger, step: str) -> None:
    erasure = _claim(ledger)
    with pytest.raises(ErasureRecordError):
        ledger.record_step(erasure, step, {"n": 1})


@pytest.mark.parametrize(
    "captured",
    [
        {"conversations": "c1"},
        {"conversations": ["my dog is called Balto"]},
        {"conversations": [1]},
        {"two words": ["c1"]},
        {"conversations": ["x" * 201]},
    ],
)
def test_what_is_captured_is_lists_of_ids(
    ledger: ErasureLedger, captured: dict[str, object]
) -> None:
    with pytest.raises(ErasureRecordError):
        ledger.claim(
            owner_id=_OWNER,
            subject_kind=SubjectKind.PERSONA,
            subject_id=_PERSONA,
            captured=captured,  # type: ignore[arg-type]
        )
    assert ledger.get(SubjectKind.PERSONA, _PERSONA) is None


def test_a_failure_keeps_a_code_and_the_counts_so_far(ledger: ErasureLedger) -> None:
    erasure = _claim(ledger)
    ledger.record_step(erasure, "memory", {"episodic": 3})
    ledger.fail(erasure, "files_unavailable")

    row = ledger.get(SubjectKind.PERSONA, _PERSONA)
    assert row is not None
    assert row.state is ErasureState.FAILED
    assert row.error_code == "files_unavailable"
    assert row.counts == {"memory": {"episodic": 3}}


@pytest.mark.parametrize("code", ["", "PermissionError: /home/alice/secret.txt", "a b"])
def test_a_failure_code_is_a_code_not_a_message(ledger: ErasureLedger, code: str) -> None:
    erasure = _claim(ledger)
    with pytest.raises(ErasureRecordError):
        ledger.fail(erasure, code)


def test_a_resumed_erasure_is_running_again_and_the_old_failure_is_cleared(
    ledger: ErasureLedger,
) -> None:
    erasure = _claim(ledger)
    ledger.fail(erasure, "files_unavailable")
    ledger.record_step(erasure, "files", {"objects": 7})

    row = ledger.get(SubjectKind.PERSONA, _PERSONA)
    assert row is not None
    assert row.state is ErasureState.RUNNING
    assert row.error_code is None


def test_complete_is_final(ledger: ErasureLedger) -> None:
    erasure = _claim(ledger)
    ledger.complete(erasure)

    row = ledger.get(SubjectKind.PERSONA, _PERSONA)
    assert row is not None
    assert row.state is ErasureState.COMPLETE
    assert row.completed_at is not None

    with pytest.raises(ErasureRecordError):
        ledger.record_step(erasure, "memory", {"episodic": 1})
    with pytest.raises(ErasureRecordError):
        ledger.fail(erasure, "too_late")


def test_completing_twice_keeps_when_it_finished(ledger: ErasureLedger, engine: Engine) -> None:
    """The finish time is the record of when the data went, so a repeat must not move it."""
    erasure = _claim(ledger)
    ledger.complete(erasure)
    long_ago = datetime(2026, 1, 1, tzinfo=UTC)
    with engine.begin() as conn:
        conn.execute(
            update(erasures_t).where(erasures_t.c.id == erasure).values(completed_at=long_ago)
        )

    ledger.complete(erasure)

    row = ledger.get(SubjectKind.PERSONA, _PERSONA)
    assert row is not None
    assert row.completed_at is not None
    assert row.completed_at.replace(tzinfo=UTC) == long_ago


def test_a_finished_erasure_does_not_stop_the_subject_being_erased_again(
    ledger: ErasureLedger,
) -> None:
    """Community is always ``local-owner``, and a hosted identity can be provisioned again."""
    first = _claim(ledger)
    ledger.record_step(first, "memory", {"episodic": 3})
    ledger.complete(first)

    second = ledger.claim(
        owner_id=_OWNER,
        subject_kind=SubjectKind.PERSONA,
        subject_id=_PERSONA,
        captured={"conversations": ["c9"]},
    )

    assert second.id != first
    assert second.state is ErasureState.PENDING
    assert second.captured == {"conversations": ["c9"]}
    assert second.counts == {}


def test_get_prefers_the_erasure_still_open(ledger: ErasureLedger) -> None:
    first = _claim(ledger)
    ledger.complete(first)
    second = _claim(ledger)

    row = ledger.get(SubjectKind.PERSONA, _PERSONA)

    assert row is not None
    assert row.id == second != first


def test_get_finds_a_finished_erasure_when_none_is_open(ledger: ErasureLedger) -> None:
    first = _claim(ledger)
    ledger.complete(first)

    row = ledger.get(SubjectKind.PERSONA, _PERSONA)

    assert row is not None
    assert row.id == first
    assert row.state is ErasureState.COMPLETE


def test_only_one_erasure_of_a_subject_can_be_open(ledger: ErasureLedger, engine: Engine) -> None:
    """The database holds this, so two racing requests cannot both open one."""
    first = _claim(ledger)
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.execute(
            insert(erasures_t).values(
                id="erasure_racing",
                owner_id=_OWNER,
                subject_kind=SubjectKind.PERSONA.value,
                subject_id=_PERSONA,
            )
        )
    assert _claim(ledger) == first


def test_an_unknown_erasure_is_refused(ledger: ErasureLedger) -> None:
    with pytest.raises(ErasureRecordError):
        ledger.record_step("erasure_missing", "memory", {"episodic": 1})
    with pytest.raises(ErasureRecordError):
        ledger.complete("erasure_missing")


def test_an_erasure_that_never_happened_is_none(ledger: ErasureLedger) -> None:
    assert ledger.get(SubjectKind.PERSONA, _PERSONA) is None


def test_unfinished_lists_what_a_sweep_has_to_resume(ledger: ErasureLedger) -> None:
    def claim(subject: str) -> str:
        return ledger.claim(
            owner_id=_OWNER, subject_kind=SubjectKind.PERSONA, subject_id=subject, captured={}
        ).id

    claim("pending")
    ledger.record_step(claim("running"), "memory", {"episodic": 1})
    ledger.fail(claim("failed"), "files_unavailable")
    ledger.complete(claim("complete"))

    assert {row.subject_id for row in ledger.unfinished()} == {"pending", "running", "failed"}
