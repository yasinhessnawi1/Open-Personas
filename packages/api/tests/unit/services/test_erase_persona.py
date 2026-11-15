"""Erasing a persona leaves nothing of it behind, and nothing of anyone else is touched (R9-284).

The anchor test of D-R9-284-erase: a persona with something in every place an erasure has to
reach (_erase_seed.py) is erased, and every table, store, folder and file is then read back for
it. Its sibling and another owner's persona have the same things and must keep all of them.
Then each step is made to fail once, and the erasure resumed must reach the same end state with
the same counts.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING

import pytest
from _erase_seed import (
    OTHER_OWNER,
    OWNER,
    PERSONA,
    PERSONA_KINDS,
    SECRET,
    SIBLING,
    STRANGER,
    Seeded,
    chunk,
    conversation_of,
    ids_naming,
    seed,
)
from persona_api.db.models import audit_log, metadata
from persona_api.errors import ErasureRecordError
from persona_api.services import erasure_service
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_service import (
    PersonaEraseDeps,
    capture_persona,
    erase_persona,
    fence_persona,
)
from sqlalchemy import select

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from persona_api.services.erasure_ledger import Erasure

# Tables a persona's rows live in, read back by any column naming it or its conversation.
_TABLES = (
    "personas",
    "conversations",
    "messages",
    "tasks",
    "runs",
    "schedules",
    "jobs",
    "jobs_archive",
    "notifications",
    "initiative_notices",
    "tool_audit_events",
)


@pytest.fixture
def seeded(tmp_path: Path, embedder: object) -> Seeded:
    return seed(tmp_path, embedder)  # type: ignore[arg-type]


def _deps(seeded: Seeded) -> PersonaEraseDeps:
    return PersonaEraseDeps(
        engine=seeded.engine,
        memory_backend=seeded.memory,
        audit_logger=seeded.audit,
        file_storage=seeded.storage,
        workspace_root=seeded.workspace_root,
        audit_root=seeded.audit_root,
    )


def _claim(seeded: Seeded, owner: str = OWNER, persona: str = PERSONA) -> Erasure:
    with seeded.engine.connect() as conn:
        captured = capture_persona(conn, owner_id=owner, persona_id=persona)
    return ErasureLedger(seeded.engine).claim(
        owner_id=owner, subject_kind=SubjectKind.PERSONA, subject_id=persona, captured=captured
    )


def _memory_left(seeded: Seeded, pid: str) -> dict[str, int]:
    left = {k: len(seeded.memory.get_all(persona_id=pid, store_kind=k)) for k in PERSONA_KINDS}
    left["document"] = len(
        seeded.memory.get_all(persona_id=conversation_of(pid), store_kind="document")
    )
    return {kind: n for kind, n in left.items() if n}


def _everything_of(seeded: Seeded, owner: str, pid: str) -> dict[str, object]:
    """Every trace of one persona, by place. Empty means it is gone."""
    traces: dict[str, object] = {}
    for table in _TABLES:
        # Every seeded id of a persona ends with the same six characters as the persona id.
        found = ids_naming(seeded.engine, table, pid[-6:])
        if found:
            traces[table] = found
    memory = _memory_left(seeded, pid)
    if memory:
        traces["memory"] = memory
    files = sorted(f.key for f in seeded.storage.list(f"{owner}/{pid}"))
    if files or (seeded.workspace_root / owner / pid).exists():
        traces["files"] = files or ["the local folder"]
    if (seeded.audit_root / f"{pid}.tools.jsonl").exists():
        traces["tool_log"] = True
    with seeded.engine.connect() as conn:
        scrubbed = conn.execute(select(audit_log.c.metadata).where(audit_log.c.user_id == owner))
        if any(SECRET in json.dumps(m) and pid in json.dumps(m) for (m,) in scrubbed):
            traces["audit_log_content"] = True
    return traces


def test_nothing_of_an_erased_persona_remains_and_nothing_else_is_touched(seeded: Seeded) -> None:
    before_sibling = _everything_of(seeded, OWNER, SIBLING)
    before_stranger = _everything_of(seeded, OTHER_OWNER, STRANGER)
    assert _everything_of(seeded, OWNER, PERSONA), "the seed must put something everywhere"

    erase_persona(_deps(seeded), _claim(seeded))

    assert _everything_of(seeded, OWNER, PERSONA) == {}
    assert _everything_of(seeded, OWNER, SIBLING) == before_sibling
    assert _everything_of(seeded, OTHER_OWNER, STRANGER) == before_stranger


def test_what_the_ruling_keeps_is_kept(seeded: Seeded) -> None:
    """Store audit stays (ids and labels), declines and tombstones stay as the owner's intent."""
    erase_persona(_deps(seeded), _claim(seeded))

    assert (seeded.audit_root / f"{PERSONA}.jsonl").exists()
    assert ids_naming(seeded.engine, "store_audit_events", PERSONA) != []
    assert ids_naming(seeded.engine, "initiative_declines", PERSONA) == []
    assert len(ids_naming(seeded.engine, "initiative_declines", "decl_aaaaaa")) == 1
    assert len(ids_naming(seeded.engine, "schedule_tombstones", "tomb_aaaaaa")) == 1
    with seeded.engine.connect() as conn:
        kept = conn.execute(
            select(audit_log.c.metadata).where(audit_log.c.target == conversation_of(PERSONA))
        ).scalar_one()
    assert kept == {"persona_id": PERSONA, "outcome": "done"}


def test_the_erasure_completes_with_counts_and_is_audited_without_content(seeded: Seeded) -> None:
    erasure = _claim(seeded)

    erase_persona(_deps(seeded), erasure)

    done = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert done is not None
    assert done.state is ErasureState.COMPLETE
    assert done.counts["row"] == {"personas": 1}
    assert done.counts["memory"] == dict.fromkeys((*PERSONA_KINDS, "document"), 1)
    assert done.counts["late_writes"] == dict.fromkeys((*PERSONA_KINDS, "document"), 0) | {
        "schedules": 0,
        "jobs": 0,
    }
    assert done.counts["files"]["objects"] >= 1
    assert done.counts["files"]["tool_log"] == 1
    with seeded.engine.connect() as conn:
        audited = conn.execute(
            select(audit_log.c.metadata).where(
                audit_log.c.action == "persona.erase", audit_log.c.target == PERSONA
            )
        ).scalar_one()
    assert audited["memory_episodic"] == "1"
    assert SECRET not in json.dumps(audited)


def test_capture_reads_what_the_later_steps_need_while_it_still_exists(seeded: Seeded) -> None:
    with seeded.engine.connect() as conn:
        captured = capture_persona(conn, owner_id=OWNER, persona_id=PERSONA)

    assert captured == {
        "conversations": [conversation_of(PERSONA)],
        "tasks": ["task_aaaaaa"],
        "runs": ["run_aaaaaa"],
        "schedules": [f"initsched:{PERSONA}", "sched_aaaaaa"],
    }


def test_capture_refuses_a_persona_that_is_not_the_owners(seeded: Seeded) -> None:
    with seeded.engine.connect() as conn, pytest.raises(ErasureRecordError):
        capture_persona(conn, owner_id=OWNER, persona_id=STRANGER)


def test_an_erasure_whose_owner_does_not_own_the_persona_touches_nothing(seeded: Seeded) -> None:
    """A ledger row naming another owner's persona must not reach that persona's memory."""
    before = _everything_of(seeded, OTHER_OWNER, STRANGER)
    forged = ErasureLedger(seeded.engine).claim(
        owner_id=OWNER, subject_kind=SubjectKind.PERSONA, subject_id=STRANGER, captured={}
    )

    with pytest.raises(ErasureRecordError):
        erase_persona(_deps(seeded), forged)

    assert _everything_of(seeded, OTHER_OWNER, STRANGER) == before


def test_the_fence_stops_what_would_run_for_the_persona(seeded: Seeded) -> None:
    counts = fence_persona(seeded.engine, _claim(seeded))

    assert counts == {"schedules": 2, "jobs": 1}
    assert ids_naming(seeded.engine, "schedules", PERSONA) == []
    assert ids_naming(seeded.engine, "jobs", conversation_of(PERSONA)) == []
    assert len(ids_naming(seeded.engine, "schedules", SIBLING)) == 1


def test_the_erase_job_itself_is_never_fenced(seeded: Seeded) -> None:
    erasure = _claim(seeded)
    from _erase_seed import row  # noqa: PLC0415 - one helper, one test

    with seeded.engine.begin() as conn:
        row(
            conn,
            "jobs",
            id="the_erase_job",
            type=erasure_service.PERSONA_ERASE_JOB_TYPE,
            owner_id=OWNER,
            payload={"erasure_id": erasure.id, "persona_id": PERSONA},
            idempotency_key="erase",
            state="running",
        )

    erase_persona(_deps(seeded), erasure)

    assert ids_naming(seeded.engine, "jobs", "the_erase_job") == ["the_erase_job"]


def test_a_write_that_lands_after_the_memory_step_is_caught(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A turn already past its write check when the row went still writes; the last pass sees it."""

    def late_turn(step: Callable[..., dict[str, int]]) -> Callable[..., dict[str, int]]:
        def wrapped(deps: PersonaEraseDeps, erasure: Erasure) -> dict[str, int]:
            counts = step(deps, erasure)
            seeded.memory.upsert(persona_id=PERSONA, store_kind="episodic", chunks=[chunk("late")])
            return counts

        return wrapped

    steps = dict(erasure_service.STEPS)
    steps["files"] = late_turn(steps["files"])
    monkeypatch.setattr(erasure_service, "STEPS", tuple(steps.items()))

    erase_persona(_deps(seeded), _claim(seeded))

    done = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert done is not None
    assert done.counts["late_writes"]["episodic"] == 1
    assert _memory_left(seeded, PERSONA) == {}


@pytest.mark.parametrize("failing", [name for name, _ in erasure_service.STEPS])
def test_a_failure_at_any_step_resumes_to_the_same_end_state(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch, failing: str
) -> None:
    before_sibling = _everything_of(seeded, OWNER, SIBLING)
    original = tuple(erasure_service.STEPS)

    def broken(_deps: PersonaEraseDeps, _erasure: Erasure) -> dict[str, int]:
        raise OSError("a path that must not reach the ledger: /srv/secret")

    monkeypatch.setattr(
        erasure_service,
        "STEPS",
        tuple((name, broken if name == failing else step) for name, step in original),
    )
    with pytest.raises(OSError, match="a path"):
        erase_persona(_deps(seeded), _claim(seeded))
    failed = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert failed is not None
    assert failed.state is ErasureState.FAILED
    assert failed.error_code == f"{failing}_failed"

    monkeypatch.setattr(erasure_service, "STEPS", original)
    erase_persona(_deps(seeded), failed)

    done = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert done is not None
    assert done.state is ErasureState.COMPLETE
    assert done.counts["memory"] == dict.fromkeys((*PERSONA_KINDS, "document"), 1)
    assert done.counts["row"] == {"personas": 1}
    assert _everything_of(seeded, OWNER, PERSONA) == {}
    assert _everything_of(seeded, OWNER, SIBLING) == before_sibling


def test_a_finished_erasure_is_not_run_again(seeded: Seeded) -> None:
    erasure = _claim(seeded)
    erase_persona(_deps(seeded), erasure)
    finished = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)

    erase_persona(_deps(seeded), erasure)

    assert ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA) == finished


def test_the_seed_reaches_every_table_that_names_a_persona() -> None:
    """A new table with a persona column must be added to the erasure, or this fails."""
    erased_or_kept = {
        *_TABLES,
        "initiative_declines",  # nulled: the owner's intent, kept
        "schedule_tombstones",  # nulled: the owner's intent, kept
        "store_audit_events",  # kept by the ruling: ids and labels
        "erasures",  # the ledger itself
        "memory_chunks",  # through the store doors, read back above as "memory"
    }
    naming = {
        t.name
        for t in metadata.sorted_tables
        if "persona_id" in t.columns or "active_persona_id" in t.columns
    }
    # Gone by the personas foreign key cascade, which the anchor test reads back for the
    # tables that matter; the rest are listed so a new one is a decision, not an accident.
    cascaded = {
        "approval_proposals",
        "approval_decisions",
        "cadence_counters",
        "calls",
        "connector_channels",
        "connector_conversations",
        "deferred_digest",
        "event_triggers",
        "persona_mcp_assignments",
        "skill_consents",
        "suspended_personas",
        "task_checkpoints",
        "turn_logs",
    }
    assert naming - erased_or_kept - cascaded == set()


def _race(seeded: Seeded, monkeypatch: pytest.MonkeyPatch) -> list[BaseException]:
    """Run the same erasure twice at once: the second starts while the first is in its fence."""
    erasure = _claim(seeded)
    first_inside, second_done = threading.Event(), threading.Event()
    steps = dict(erasure_service.STEPS)
    fence = steps["fence"]

    def held_open(deps: PersonaEraseDeps, e: Erasure) -> dict[str, int]:
        if not first_inside.is_set():
            first_inside.set()
            second_done.wait(timeout=60)
        return fence(deps, e)

    steps["fence"] = held_open
    monkeypatch.setattr(erasure_service, "STEPS", tuple(steps.items()))
    errors: list[BaseException] = []

    def first() -> None:
        try:
            erase_persona(_deps(seeded), erasure)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    thread = threading.Thread(target=first)
    thread.start()
    assert first_inside.wait(timeout=60)
    try:
        erase_persona(_deps(seeded), erasure)
    except BaseException as exc:  # noqa: BLE001 - reported to the test
        errors.append(exc)
    finally:
        second_done.set()
        thread.join(timeout=120)
    return errors


def test_two_runs_of_one_erasure_count_it_once_and_audit_it_once(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M1: the second run finds the first holding the lock and does nothing."""
    errors = _race(seeded, monkeypatch)

    assert errors == []
    done = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert done is not None
    assert done.state is ErasureState.COMPLETE
    assert done.counts["memory"] == dict.fromkeys((*PERSONA_KINDS, "document"), 1)
    assert done.counts["row"] == {"personas": 1}
    with seeded.engine.connect() as conn:
        audited = conn.execute(
            select(audit_log.c.id).where(
                audit_log.c.action == "persona.erase", audit_log.c.target == PERSONA
            )
        ).all()
    assert len(audited) == 1
