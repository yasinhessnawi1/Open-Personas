"""A refused boot names a repair command, and that command really works (R9-205, D-K10-10).

The managed community database is invisible: the user never installed it and it is only up while
the app is, so ``persona repair --database-url`` is no use to them. When migration 059 refuses, the
boot stops with a message, and the message has to be something they can act on. So these run the
real bundled Postgres, take the command OUT OF THE MESSAGE, and run exactly that as a separate
process, with the app down, the way the person would.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from persona.audit import MemoryAuditLogger
from persona.recall.core_memory import CoreBlock, core_block_to_chunk
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.core_memory import CoreMemoryStore
from persona.stores.postgres import CHAIN_HEAD_INDEX, PostgresBackend
from persona.stores.self_facts import SelfFactsStore
from persona_api.db.community_managed import CommunityDbManager, run_migrations
from persona_api.db.community_repair import repair_command
from persona_api.errors import CommunityDbError
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy.engine import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

pytest.importorskip("pixeltable_pgserver")

_NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
_PRED = "058_jobs_live_task_leg_index"
_PERSONA = "p1"
_LOGICAL = "chain"


@pytest.fixture
def short_base_dir() -> Iterator[Path]:
    """Short, because the unix socket lives in the data folder (the AF_UNIX path cap)."""
    root = None if sys.platform == "win32" else "/tmp"  # noqa: S108
    base = Path(tempfile.mkdtemp(prefix="k13", dir=root))
    yield base
    shutil.rmtree(base, ignore_errors=True)


def _chunk(chunk_id: str, version: int, superseded_by: str | None) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=f"text of {chunk_id}",
        created_at=_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.USER,
            logical_id=_LOGICAL,
            version=version,
            superseded_by=superseded_by,
            written_at=_NOW,
        ),
    )


def _as_a_database_from_before_the_index(engine: Engine, embedder: HashEmbedder384) -> None:
    """A community install that ran 058 and raced, one version before this app update."""
    with engine.begin() as conn:
        conn.execute(text(f"DROP INDEX IF EXISTS {CHAIN_HEAD_INDEX}"))
        conn.execute(text("UPDATE alembic_version SET version_num = :v"), {"v": _PRED})
    PostgresBackend(engine=engine, embedder=embedder).upsert(
        persona_id=_PERSONA,
        store_kind="self_facts",
        chunks=[_chunk("v1", 1, "b"), _chunk("a", 2, None), _chunk("b", 2, None)],
    )


def _environment(base_dir: Path) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in {"DATABASE_URL", "APP_DATABASE_URL"}}
    env["PERSONA_COMMUNITY_DB_MODE"] = "embedded"
    env["PERSONA_API_COMMUNITY_MANAGED_DB_DIR"] = str(base_dir)
    env["PERSONA_EDITION"] = "community"
    env["HF_HUB_OFFLINE"] = "1"
    return env


def _run_as_the_user_would(command_text: str, base_dir: Path) -> subprocess.CompletedProcess[str]:
    """Run the command as the message spelled it, with this interpreter for ``uv run python``."""
    words = shlex.split(command_text)
    assert words[:3] == ["uv", "run", "python"], f"not the command a user can type: {command_text}"
    return subprocess.run(  # noqa: S603 - a fixed interpreter and the app's own module
        [sys.executable, *words[3:]],
        env=_environment(base_dir),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )


def _commands_in(message: str) -> list[str]:
    return re.findall(r"`([^`]+)`", message)


@pytest.mark.timeout(300)
def test_the_refusal_names_a_command_that_settles_the_managed_database(
    short_base_dir: Path, embedder: HashEmbedder384, monkeypatch: pytest.MonkeyPatch
) -> None:
    # run_migrations mutates os.environ["DATABASE_URL"] (D-K10-8); neutralise the leak.
    monkeypatch.delenv("DATABASE_URL", raising=False)
    manager = CommunityDbManager(external_url=None, base_dir=short_base_dir)
    url = manager.start()
    engine = create_engine(url)
    try:
        run_migrations(url)
        _as_a_database_from_before_the_index(engine, embedder)

        with pytest.raises(CommunityDbError) as caught:
            run_migrations(url)
        message = str(caught.value)
        assert caught.value.context["reason"] == "chain_heads_conflict"
        commands = _commands_in(message)
        assert len(commands) == 2, f"the message must name a look and a fix: {message}"
        look, fix = commands

        # The app is down, and so is its database, which is the state the person is in.
        engine.dispose()
        manager.stop()

        looked = _run_as_the_user_would(look, short_base_dir)
        assert looked.returncode == 1, looked.stdout + looked.stderr
        assert "--resolve-raced" in looked.stdout

        fixed = _run_as_the_user_would(fix, short_base_dir)
        assert fixed.returncode == 0, fixed.stdout + fixed.stderr
        assert "now intact" in fixed.stdout

        # And the app boots: the next start migrates as if nothing had happened.
        url = manager.start()
        run_migrations(url)
        engine = create_engine(url)
        with engine.connect() as conn:
            valid = conn.execute(
                text(
                    "SELECT indisvalid FROM pg_index "
                    f"WHERE indexrelid = '{CHAIN_HEAD_INDEX}'::regclass"
                )
            ).scalar()
            audited = conn.execute(
                text("SELECT count(*) FROM store_audit_events WHERE action = 'repair'")
            ).scalar()
        assert valid is True
        assert audited == 1
        store = SelfFactsStore(
            backend=PostgresBackend(engine=engine, embedder=embedder),
            audit_logger=MemoryAuditLogger(),
        )
        assert [c.id for c in store.history(_PERSONA, _LOGICAL)] == ["v1", "a", "b"]
    finally:
        engine.dispose()
        manager.stop()


@pytest.mark.timeout(300)
def test_the_command_leaves_a_database_it_found_running_as_it_found_it(
    short_base_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stopping the postmaster under a running app would be a far worse bug than a broken chain."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    manager = CommunityDbManager(external_url=None, base_dir=short_base_dir)
    url = manager.start()
    engine = create_engine(url)
    try:
        run_migrations(url)

        result = _run_as_the_user_would(repair_command(), short_base_dir)
        assert result.returncode == 0, result.stdout + result.stderr

        with engine.connect() as conn:
            assert conn.execute(text("SELECT 1")).scalar() == 1
    finally:
        engine.dispose()
        manager.stop()


@pytest.mark.timeout(300)
def test_the_sweep_for_forgotten_evidence_works_on_the_managed_database(
    short_base_dir: Path, embedder: HashEmbedder384, monkeypatch: pytest.MonkeyPatch
) -> None:
    """R9-209's sweep, on the install nobody can reach with a DSN."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    manager = CommunityDbManager(external_url=None, base_dir=short_base_dir)
    url = manager.start()
    engine = create_engine(url)
    try:
        run_migrations(url)
        store = CoreMemoryStore(
            backend=PostgresBackend(engine=engine, embedder=embedder),
            audit_logger=MemoryAuditLogger(),
        )
        block = CoreBlock(text="a summary", source_ids=("long-gone",))
        store.write(
            _PERSONA,
            [core_block_to_chunk(block, _PERSONA, now=_NOW)],
            source=WriteSource.SYSTEM,
        )

        look = _run_as_the_user_would(repair_command("--forgotten-evidence"), short_base_dir)
        assert look.returncode == 1, look.stdout + look.stderr
        assert store.current(_PERSONA) is not None, "a dry run changed something"

        fixed = _run_as_the_user_would(
            repair_command("--forgotten-evidence", "--apply"), short_base_dir
        )
        assert fixed.returncode == 0, fixed.stdout + fixed.stderr
        assert store.current(_PERSONA) is None
    finally:
        engine.dispose()
        manager.stop()


def test_a_community_install_on_sqlite_is_told_what_to_do_instead(tmp_path: Path) -> None:
    """legacy-sqlite has no managed database, so the command must not pretend it does."""
    env = _environment(tmp_path)
    env["PERSONA_COMMUNITY_DB_MODE"] = "legacy-sqlite"
    result = subprocess.run(  # noqa: S603 - a fixed interpreter and the app's own module
        [sys.executable, "-m", "persona_api.db.community_repair"],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 2
    assert "persona repair" in result.stdout + result.stderr
