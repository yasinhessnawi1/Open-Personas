"""``persona repair --orphaned``: what a deleted persona left behind, found and removed (R9-284).

Deleting a persona used to delete only its row. Everything that had no foreign key to it stayed:
every memory row, the document chunks of its conversations, its initiative notices, and the
daily scan schedule that went on firing for nobody. Migration 062 stops new ones, and a
database restored from a backup still has the old ones.

Dry run by default. The sweep reports ids and counts and never content, because its output lands
in deploy logs, and each removal is audited in the same transaction as the delete.

These use ``pg_engine`` (tables only, no 062 trigger), so an orphan can be written directly, the
way history left them.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.cli.main import app
from persona.stores.orphans import OrphanSweep
from sqlalchemy import text
from typer.testing import CliRunner

if TYPE_CHECKING:
    from sqlalchemy import Engine

pytestmark = pytest.mark.integration

_OWNER = "u_sweep"
_LIVE = "persona_11111111111111111111111111111111"
_LIVE_CONVERSATION = "conv_live"
_GONE = "persona_22222222222222222222222222222222"
_GONE_CONVERSATION = "conv_gone"
_SECRET = "my dog is called Balto"

_INSERT_CHUNK = text(
    "INSERT INTO memory_chunks (id, persona_id, kind, text, embedding, content_hash) "
    "VALUES (:id, :p, :k, :t, array_fill(0, ARRAY[384])::vector, 'h')"
)


def _chunk(conn: Any, chunk_id: str, owner: str, kind: str) -> None:  # noqa: ANN401
    conn.execute(_INSERT_CHUNK, {"id": chunk_id, "p": owner, "k": kind, "t": _SECRET})


def _notice(conn: Any, notice_id: str, persona_id: str) -> None:  # noqa: ANN401
    conn.execute(
        text(
            "INSERT INTO initiative_notices "
            "(id, owner_id, persona_id, opportunity_key, trigger, source, disposition, candidate) "
            "VALUES (:i, :o, :p, :k, 't', 'scan', 'held', CAST(:c AS jsonb))"
        ),
        {
            "i": notice_id,
            "o": _OWNER,
            "p": persona_id,
            "k": f"key:{notice_id}",
            "c": json.dumps({"observation": _SECRET}),
        },
    )


def _scan_schedule(conn: Any, persona_id: str) -> None:  # noqa: ANN401
    conn.execute(
        text(
            "INSERT INTO schedules (id, owner_id, timezone, recurrence, target_job_type) "
            "VALUES (:i, :o, 'UTC', 'FREQ=DAILY', 'initiative_scan')"
        ),
        {"i": f"initsched:{persona_id}", "o": _OWNER},
    )


@pytest.fixture
def seeded(pg_engine: Engine) -> Engine:
    with pg_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'sweep@x.test')"), {"o": _OWNER}
        )
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
            {"p": _LIVE, "o": _OWNER},
        )
        conn.execute(
            text("INSERT INTO conversations (id, owner_id, persona_id) VALUES (:c, :o, :p)"),
            {"c": _LIVE_CONVERSATION, "o": _OWNER, "p": _LIVE},
        )
        # What a live persona owns, none of which may be touched.
        _chunk(conn, "live-episodic", _LIVE, "episodic")
        _chunk(conn, "live-core", _LIVE, "core_memory")
        _chunk(conn, "live-doc", _LIVE_CONVERSATION, "document")
        _notice(conn, "live-notice", _LIVE)
        _scan_schedule(conn, _LIVE)
        # An ordinary schedule: its id is not a persona's, and that must not make it an orphan.
        conn.execute(
            text(
                "INSERT INTO schedules (id, owner_id, timezone, recurrence, target_job_type) "
                "VALUES ('a_user_reminder', :o, 'UTC', 'FREQ=DAILY', 'task_scheduled_fire')"
            ),
            {"o": _OWNER},
        )
        # What a deleted persona left.
        _chunk(conn, "gone-core", _GONE, "core_memory")
        _chunk(conn, "gone-gist", _GONE, "episodic_gist")
        _chunk(conn, "gone-episodic-1", _GONE, "episodic")
        _chunk(conn, "gone-episodic-2", _GONE, "episodic")
        _chunk(conn, "gone-identity", _GONE, "identity")
        _chunk(conn, "gone-doc", _GONE_CONVERSATION, "document")
        _notice(conn, "gone-notice", _GONE)
        _scan_schedule(conn, _GONE)
    return pg_engine


def _cli(*args: str) -> Any:  # noqa: ANN401 - typer's Result
    return CliRunner().invoke(
        app,
        [
            "repair",
            "--all-personas",
            "--database-url",
            os.environ["DATABASE_URL"],
            "--orphaned",
            *args,
        ],
    )


def _ids(engine: Engine, sql: str) -> set[str]:
    with engine.connect() as conn:
        return set(conn.execute(text(sql)).scalars())


def _everything(engine: Engine) -> set[str]:
    return (
        _ids(engine, "SELECT id FROM memory_chunks")
        | _ids(engine, "SELECT id FROM initiative_notices")
        | _ids(engine, "SELECT id FROM schedules")
    )


_LIVE_ROWS = {
    "live-episodic",
    "live-core",
    "live-doc",
    "live-notice",
    f"initsched:{_LIVE}",
    "a_user_reminder",
}
_ORPHANS = {
    "gone-core",
    "gone-gist",
    "gone-episodic-1",
    "gone-episodic-2",
    "gone-identity",
    "gone-doc",
    "gone-notice",
    f"initsched:{_GONE}",
}


def test_it_finds_what_has_no_owner_and_nothing_else(seeded: Engine) -> None:
    groups = OrphanSweep(seeded).find()

    assert {(g.table, g.owner_id, g.kind, g.rows) for g in groups} == {
        ("memory_chunks", _GONE, "episodic", 2),
        ("memory_chunks", _GONE, "identity", 1),
        ("memory_chunks", _GONE, "episodic_gist", 1),
        ("memory_chunks", _GONE, "core_memory", 1),
        ("memory_chunks", _GONE_CONVERSATION, "document", 1),
        ("initiative_notices", _GONE, "initiative_notice", 1),
        ("schedules", _GONE, "initiative_schedule", 1),
    }


def test_evidence_is_listed_before_what_was_written_from_it(seeded: Engine) -> None:
    """K8-D-14's order: if a sweep stops part way, nothing derived is left without its source."""
    kinds = [g.kind for g in OrphanSweep(seeded).find()]

    assert kinds.index("episodic") < kinds.index("episodic_gist") < kinds.index("core_memory")
    assert kinds.index("identity") < kinds.index("core_memory")


def test_a_dry_run_reports_and_changes_nothing(seeded: Engine) -> None:
    before = _everything(seeded)

    result = _cli()

    assert result.exit_code == 1, result.output
    assert _GONE in result.output
    assert _SECRET not in result.output
    assert "--apply" in result.output
    assert _everything(seeded) == before
    assert _ids(seeded, "SELECT id FROM audit_log") == set()


def test_apply_removes_the_orphans_and_only_the_orphans(seeded: Engine) -> None:
    result = _cli("--apply")

    assert result.exit_code == 0, result.output
    assert _everything(seeded) == _LIVE_ROWS
    assert _SECRET not in result.output


@pytest.mark.usefixtures("seeded")
def test_a_second_sweep_finds_nothing() -> None:
    assert _cli("--apply").exit_code == 0

    result = _cli()

    assert result.exit_code == 0, result.output
    assert _GONE not in result.output


def test_each_removal_is_audited_with_ids_and_counts_and_no_content(seeded: Engine) -> None:
    assert _cli("--apply").exit_code == 0

    with seeded.connect() as conn:
        rows = conn.execute(
            text("SELECT user_id, action, target, metadata FROM audit_log ORDER BY target")
        ).all()
    assert len(rows) == 7
    assert {r.user_id for r in rows} == {"cli.repair"}
    assert {r.action for r in rows} == {"repair.orphaned"}
    assert {r.target for r in rows} == {_GONE, _GONE_CONVERSATION}
    assert _SECRET not in json.dumps([r.metadata for r in rows])
    episodic = next(r for r in rows if r.metadata["kind"] == "episodic")
    assert episodic.metadata == {"table": "memory_chunks", "kind": "episodic", "rows": "2"}


def test_an_owner_that_appeared_after_the_scan_keeps_its_rows(seeded: Engine) -> None:
    """The delete asks again, in one statement, so a stale scan cannot remove a live row."""
    sweep = OrphanSweep(seeded)
    group = next(g for g in sweep.find() if g.kind == "identity")
    with seeded.begin() as conn:
        conn.execute(
            text("INSERT INTO personas (id, owner_id, yaml) VALUES (:p, :o, 'x: 1')"),
            {"p": _GONE, "o": _OWNER},
        )

    assert sweep.remove(group, written_by="test") == 0

    assert "gone-identity" in _ids(seeded, "SELECT id FROM memory_chunks")
    assert _ids(seeded, "SELECT id FROM audit_log") == set()


def test_a_removal_only_takes_its_own_owner_and_kind(seeded: Engine) -> None:
    sweep = OrphanSweep(seeded)
    group = next(g for g in sweep.find() if g.kind == "identity")

    assert sweep.remove(group, written_by="test") == 1

    left = _ids(seeded, "SELECT id FROM memory_chunks")
    assert "gone-identity" not in left
    assert {"gone-core", "gone-gist", "gone-episodic-1", "gone-doc"} <= left


def test_memory_with_no_personas_at_all_stops_the_sweep(pg_engine: Engine) -> None:
    """A partial restore or the wrong database looks like this, and --apply cannot be undone."""
    with pg_engine.begin() as conn:
        _chunk(conn, "kept", _GONE, "episodic")

    result = _cli("--apply")

    assert result.exit_code == 2, result.output
    assert "no personas" in result.output
    assert _ids(pg_engine, "SELECT id FROM memory_chunks") == {"kept"}


def test_it_refuses_a_role_that_cannot_see_every_tenant(seeded: Engine) -> None:
    """``persona_app`` reads zero rows, so a sweep would report a healthy database."""
    _ = seeded
    result = CliRunner().invoke(
        app,
        [
            "repair",
            "--all-personas",
            "--database-url",
            os.environ["APP_DATABASE_URL"],
            "--orphaned",
        ],
    )
    assert result.exit_code == 2, result.output
    assert "row level security" in result.output


def test_the_restore_check_counts_the_same_owners_the_sweep_finds(seeded: Engine) -> None:
    """``verify.sql`` is a second copy of the rule, and a restore reads it before any sweep."""
    verify = Path(__file__).resolve().parents[4] / "deploy" / "server" / "sql" / "verify.sql"
    found = re.search(
        r"SELECT 'memory_orphan_owners'.*?;", verify.read_text(encoding="utf-8"), re.DOTALL
    )
    assert found is not None
    with seeded.connect() as conn:
        row = conn.execute(text(found.group(0))).one()

    memory = [g for g in OrphanSweep(seeded).find() if g.table == "memory_chunks"]
    assert row[1] == str(len(memory))
