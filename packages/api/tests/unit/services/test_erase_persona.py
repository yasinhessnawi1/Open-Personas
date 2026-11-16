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
from collections.abc import Iterator  # noqa: TC003 - a fixture annotation
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
    legacy_edition,
    row,
    uncovered,
)
from persona_api.db.models import audit_log, store_audit_events
from persona_api.errors import ErasureRecordError
from persona_api.services import erasure_service
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_service import (
    PersonaEraseDeps,
    capture_persona,
    erase_persona,
    fence_persona,
)
from sqlalchemy import JSON, inspect, select

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
def seeded(tmp_path: Path, embedder: object) -> Iterator[Seeded]:
    with legacy_edition(tmp_path, embedder) as legacy:  # type: ignore[arg-type]
        yield legacy


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
    assert ids_naming(seeded.engine, "schedule_tombstones", PERSONA) == []
    assert len(ids_naming(seeded.engine, "schedule_tombstones", "tomb_aaaaaa")) == 1
    with seeded.engine.connect() as conn:
        kept = conn.execute(
            select(audit_log.c.metadata).where(audit_log.c.id == "audit_aaaaaa")
        ).scalar_one()
    assert kept == {"persona_id": PERSONA, "outcome": "done"}


def test_file_names_leave_the_audit_log_and_its_rows_stay(seeded: Seeded) -> None:
    """Review L3: a workspace path and a document's target name the user's files."""
    erase_persona(_deps(seeded), _claim(seeded))

    with seeded.engine.connect() as conn:
        rows = {
            r.id: (r.target, r.metadata)
            for r in conn.execute(
                select(audit_log.c.id, audit_log.c.target, audit_log.c.metadata).where(
                    audit_log.c.id.in_(["upload_aaaaaa", "docup_aaaaaa", "upload_bbbbbb"])
                )
            )
        }
    assert rows["upload_aaaaaa"] == (PERSONA, {"media_type": "image/png"})
    assert rows["docup_aaaaaa"] == (conversation_of(PERSONA), {"format": "pdf"})
    assert SECRET in json.dumps(rows["upload_bbbbbb"]), "the sibling's rows are untouched"


def test_the_personas_event_trigger_notifications_go(seeded: Seeded) -> None:
    """Review L1: the trigger goes by cascade, so its id is captured for its notification."""
    erase_persona(_deps(seeded), _claim(seeded))

    for kind in ("event_trigger_dropped", "run_terminal", "schedule_fired"):
        assert ids_naming(seeded.engine, "notifications", f"{kind}_aaaaaa") == []
        assert ids_naming(seeded.engine, "notifications", f"{kind}_bbbbbb") != []


def test_a_conversation_started_after_the_claim_loses_its_documents_too(seeded: Seeded) -> None:
    """Review L2: the row step re-reads the persona's conversations under the row lock."""
    erasure = _claim(seeded)
    late = "convlateaaaaaa"
    with seeded.engine.begin() as conn:
        row(conn, "conversations", id=late, owner_id=OWNER, persona_id=PERSONA, origin="chat")
    seeded.memory.upsert(persona_id=late, store_kind="document", chunks=[chunk("late-doc")])

    erase_persona(_deps(seeded), erasure)

    assert seeded.memory.get_all(persona_id=late, store_kind="document") == []
    done = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert done is not None
    assert late in done.captured["conversations"]
    assert done.counts["memory"]["document"] == 2


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
        "event_triggers": ["trig_aaaaaa"],
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


def test_every_place_the_schema_can_name_a_persona_has_a_decision(seeded: Seeded) -> None:
    """Review L5: derived from the live schema, so a new table or JSON column fails here."""
    inspector = inspect(seeded.engine)
    persona_tables, json_columns = [], []
    for table in inspector.get_table_names():
        columns = inspector.get_columns(table)
        if any(c["name"] in {"persona_id", "active_persona_id"} for c in columns):
            persona_tables.append(table)
        json_columns += [f"{table}.{c['name']}" for c in columns if isinstance(c["type"], JSON)]

    assert len(persona_tables) >= 10
    assert len(json_columns) >= 20
    assert uncovered(persona_tables, json_columns) == []


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


def test_the_store_audit_keeps_its_ids_and_loses_its_free_text(seeded: Seeded) -> None:
    """The ruling keeps store audit as ids and labels; a reason is free text from the model."""
    erase_persona(_deps(seeded), _claim(seeded))

    with seeded.engine.connect() as conn:
        rows = {
            r.id: (r.reason, r.chunk_ids, r.metadata)
            for r in conn.execute(
                select(
                    store_audit_events.c.id,
                    store_audit_events.c.reason,
                    store_audit_events.c.chunk_ids,
                    store_audit_events.c.metadata,
                )
            )
        }
    assert rows["store_aaaaaa"] == (None, ["c"], {"to_version": "2"})
    assert rows["graphaudit_aaaaaa"] == (None, ["node"], {"action": "created"})
    assert rows["store_bbbbbb"][0] == SECRET, "the sibling's audit is untouched"
    assert rows["graphaudit_bbbbbb"][0] == SECRET

    own = (seeded.audit_root / f"{PERSONA}.jsonl").read_text(encoding="utf-8")
    graph = (seeded.audit_root / f"{OWNER}.jsonl").read_text(encoding="utf-8")
    assert SECRET not in own
    assert json.loads(own)["chunk_ids"] == ["c"]
    assert json.loads(own)["metadata"] == {"to_version": "2"}, "R9-291: content keys go"
    lines = [json.loads(line) for line in graph.splitlines()]
    assert {e["written_by"]: e["reason"] for e in lines} == {PERSONA: None, SIBLING: SECRET}
    # Review L2: the persona's events in the owner's log lose their content keys too; the
    # sibling's keep theirs.
    meta = {e["written_by"]: e["metadata"] for e in lines}
    assert meta[PERSONA] == {"action": "created"}
    assert meta[SIBLING] == {"action": "created", "workspace_path": SECRET}


def test_another_owners_audit_rows_are_never_scrubbed(seeded: Seeded) -> None:
    """The owner predicate: another owner's row naming this persona's conversation stays."""
    erase_persona(_deps(seeded), _claim(seeded))

    with seeded.engine.connect() as conn:
        foreign = conn.execute(
            select(audit_log.c.target, audit_log.c.metadata).where(
                audit_log.c.id == "foreign_aaaaaa"
            )
        ).one()
    assert foreign.target == f"{conversation_of(PERSONA)}/doc_x"
    assert foreign.metadata == {"workspace_path": f"x/{SECRET}.pdf", "verbatim_ask": SECRET}
