"""Erasing a persona on Postgres: ``memory_chunks``, the cascade and the owner guard (R9-284).

The unit tests (tests/unit/services/test_erase_persona.py) pin every step on the community
engine with Chroma. This is the same erasure on a migrated Postgres, where memory is one table
with no foreign key, documents are keyed by conversation, and migration 062's guard refuses a
memory row whose owner is gone. Runs in CI.
"""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

import pytest
from _erase_seed import (
    OWNER,
    PERSONA,
    SIBLING,
    STRANGER,
    Seeded,
    chunk,
    conversation_of,
    ids_naming,
    seed_into,
    uncovered,
)
from persona.errors import MemoryOwnerMissingError
from persona.stores.postgres import PostgresBackend
from persona_api.services import erasure_service
from persona_api.services.erasure_ledger import ErasureLedger, ErasureState, SubjectKind
from persona_api.services.erasure_service import (
    AUDIT_CONTENT_KEYS,
    PersonaEraseDeps,
    capture_persona,
    erase_persona,
)
from sqlalchemy import create_engine, event, text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from persona_api.services.erasure_ledger import Erasure
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

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
    # R9-253: seeded on a live conversation and on one already gone; both go with the persona.
    "wellbeing_disclosures",
)


@pytest.fixture
def seeded(migrated_engine: Engine, embedder: object, tmp_path: Path) -> Seeded:
    memory = PostgresBackend(engine=migrated_engine, embedder=embedder)  # type: ignore[arg-type]
    return seed_into(migrated_engine, memory, tmp_path)


@pytest.fixture
def platform(migrated_engine: Engine, database_url: str) -> Iterator[Engine]:
    """An engine that is ``persona_platform`` (BYPASSRLS, not a superuser), as in production.

    The role is made NOLOGIN here and taken with SET ROLE on each connection, so the test needs
    no password; its grants are release.py's (review L5: the erasure runs as this role).
    """
    with migrated_engine.begin() as conn:
        exists = conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = 'persona_platform'")
        ).first()
        if exists is None:
            conn.execute(text("CREATE ROLE persona_platform NOLOGIN BYPASSRLS"))
        conn.execute(text("GRANT persona_platform TO CURRENT_USER"))
        for grant in _PLATFORM_GRANTS:
            conn.execute(text(grant))
    engine = create_engine(database_url)

    @event.listens_for(engine, "connect")
    def _become_platform(dbapi_connection: Any, _record: Any) -> None:  # noqa: ANN401
        with dbapi_connection.cursor() as cursor:
            cursor.execute("SET ROLE persona_platform")

    with engine.connect() as conn:
        assert conn.execute(text("SELECT current_user")).scalar() == "persona_platform"
        assert (
            conn.execute(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            ).scalar()
            is False
        )
    yield engine
    engine.dispose()


_PLATFORM_GRANTS = (
    "GRANT USAGE ON SCHEMA public TO persona_platform",
    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO persona_platform",
    "GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO persona_platform",
)


def test_every_place_the_schema_can_name_a_persona_has_a_decision(
    migrated_engine: Engine,
) -> None:
    """Review L5, from information_schema: the migrated schema, not the models."""
    with migrated_engine.connect() as conn:
        persona_tables = [
            r[0]
            for r in conn.execute(
                text(
                    "SELECT DISTINCT table_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' "
                    "AND column_name IN ('persona_id', 'active_persona_id')"
                )
            )
        ]
        json_columns = [
            f"{r[0]}.{r[1]}"
            for r in conn.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.columns "
                    "WHERE table_schema = 'public' AND data_type IN ('json', 'jsonb')"
                )
            )
        ]

    assert "memory_chunks" in persona_tables
    assert "graph_nodes.provenance" in json_columns
    assert uncovered(persona_tables, json_columns) == []


def _memory_of(engine: Engine, pid: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM memory_chunks WHERE persona_id IN (:p, :c)"),
                {"p": pid, "c": conversation_of(pid)},
            ).scalar_one()
        )


def _rows_of(engine: Engine, pid: str) -> dict[str, list[str]]:
    found = {t: ids_naming(engine, t, pid[-6:]) for t in _TABLES}
    return {t: ids for t, ids in found.items() if ids}


def _erase(seeded: Seeded, platform: Engine, embedder: Any) -> None:  # noqa: ANN401
    """Claim and erase as production does: everything on the persona_platform engine."""
    with platform.connect() as conn:
        captured = capture_persona(conn, owner_id=OWNER, persona_id=PERSONA)
    erasure = ErasureLedger(platform).claim(
        owner_id=OWNER, subject_kind=SubjectKind.PERSONA, subject_id=PERSONA, captured=captured
    )
    erase_persona(
        PersonaEraseDeps(
            engine=platform,
            memory_backend=PostgresBackend(engine=platform, embedder=embedder),
            audit_logger=seeded.audit,
            file_storage=seeded.storage,
            workspace_root=seeded.workspace_root,
            audit_root=seeded.audit_root,
        ),
        erasure,
    )


def test_nothing_of_the_persona_remains_on_postgres_and_its_neighbours_keep_all(
    seeded: Seeded,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    sibling = (_memory_of(seeded.engine, SIBLING), _rows_of(seeded.engine, SIBLING))
    stranger = (_memory_of(seeded.engine, STRANGER), _rows_of(seeded.engine, STRANGER))
    assert _memory_of(seeded.engine, PERSONA) == 7
    assert len(ids_naming(seeded.engine, "wellbeing_disclosures", PERSONA[-6:])) == 2

    _erase(seeded, platform, embedder)

    assert _memory_of(seeded.engine, PERSONA) == 0
    assert _rows_of(seeded.engine, PERSONA) == {}
    for kept in ("initiative_declines", "schedule_tombstones"):
        assert ids_naming(seeded.engine, kept, PERSONA) == [], f"{kept} must lose the persona"
        assert ids_naming(seeded.engine, kept, PERSONA[-6:]) != [], f"{kept} rows are kept"
    assert (_memory_of(seeded.engine, SIBLING), _rows_of(seeded.engine, SIBLING)) == sibling
    assert (_memory_of(seeded.engine, STRANGER), _rows_of(seeded.engine, STRANGER)) == stranger
    done = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert done is not None
    assert done.state is ErasureState.COMPLETE
    assert sum(done.counts["memory"].values()) == 7
    # R9-291: the kept log keeps ids and labels, and no content key is left in it.
    kept = (seeded.audit_root / f"{PERSONA}.jsonl").read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in kept]
    assert events, "the log is kept"
    assert all(not AUDIT_CONTENT_KEYS & (e.get("metadata") or {}).keys() for e in events)
    assert all(e.get("reason") is None for e in events)


def test_after_the_erasure_the_guard_refuses_new_memory_for_the_persona(
    seeded: Seeded,
    platform: Engine,
    embedder: Any,  # noqa: ANN401
) -> None:
    _erase(seeded, platform, embedder)

    with pytest.raises(MemoryOwnerMissingError):
        seeded.memory.upsert(persona_id=PERSONA, store_kind="episodic", chunks=[chunk("late")])
    assert _memory_of(seeded.engine, PERSONA) == 0


def test_two_runs_at_once_hold_one_advisory_lock_and_count_once(
    seeded: Seeded, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review M1 on Postgres: two sessions, one session advisory lock, one run does the work."""
    with seeded.engine.connect() as conn:
        captured = capture_persona(conn, owner_id=OWNER, persona_id=PERSONA)
    erasure = ErasureLedger(seeded.engine).claim(
        owner_id=OWNER, subject_kind=SubjectKind.PERSONA, subject_id=PERSONA, captured=captured
    )
    deps = PersonaEraseDeps(
        engine=seeded.engine,
        memory_backend=seeded.memory,
        audit_logger=seeded.audit,
        file_storage=seeded.storage,
        workspace_root=seeded.workspace_root,
        audit_root=seeded.audit_root,
    )
    first_inside, second_done = threading.Event(), threading.Event()
    steps = dict(erasure_service.STEPS)
    fence = steps["fence"]

    def held_open(d: PersonaEraseDeps, e: Erasure) -> dict[str, int]:
        if not first_inside.is_set():
            first_inside.set()
            second_done.wait(timeout=60)
        return fence(d, e)

    steps["fence"] = held_open
    monkeypatch.setattr(erasure_service, "STEPS", tuple(steps.items()))
    errors: list[BaseException] = []

    def first() -> None:
        try:
            erase_persona(deps, erasure)
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            errors.append(exc)

    thread = threading.Thread(target=first)
    thread.start()
    assert first_inside.wait(timeout=60)
    try:
        erase_persona(deps, erasure)
    finally:
        second_done.set()
        thread.join(timeout=120)

    assert errors == []
    done = ErasureLedger(seeded.engine).get(SubjectKind.PERSONA, PERSONA)
    assert done is not None
    assert done.state is ErasureState.COMPLETE
    assert sum(done.counts["memory"].values()) == 7
    with seeded.engine.connect() as conn:
        audited = conn.execute(
            text("SELECT count(*) FROM audit_log WHERE action = 'persona.erase' AND target = :p"),
            {"p": PERSONA},
        ).scalar_one()
    assert audited == 1
