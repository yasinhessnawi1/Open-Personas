"""``persona repair`` is a command a person can actually run (Spec K13, T3).

The repair exists as a CLI command rather than a library function for one reason: the person
who needs it is an operator or a self hoster looking at a store that has stopped behaving, and
a function they cannot call is no use to them. So the proof that it works has to go through
the command, the way they would run it, against a real transport. Calling the store method
underneath would prove the mending and leave the door untested (§6b: a public symbol needs a
production call site, and for this one the CLI IS the call site).

The other claim these make good on is the quiet one: the command loads no embedding model.
A repair moves version pointers, so there is nothing to embed, and an operator should not wait
a hundred seconds for torch to start in order to fix a pointer. The last test spawns a real
process and looks at what it imported.
"""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path  # noqa: TC003 - pytest tmp_path at runtime

import pytest
import yaml
from persona.audit import MemoryAuditLogger
from persona.cli.main import app
from persona.recall.core_memory import CoreBlock, core_block_to_chunk
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.chroma import ChromaBackend
from persona.stores.core_memory import CoreMemoryStore
from persona.stores.self_facts import SelfFactsStore
from typer.testing import CliRunner

from tests._cli_output import plain
from tests._embedder import HashEmbedder

pytestmark = pytest.mark.integration

_PERSONA_ID = "repair-demo"
_LOGICAL = "repair-demo::self_facts::0000"


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def persona_file(tmp_path: Path) -> Path:
    """The smallest persona the loader accepts, on disk where the command expects it."""
    path = tmp_path / f"{_PERSONA_ID}.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": "1.0",
                "persona_id": _PERSONA_ID,
                "identity": {
                    "name": "Repair Demo",
                    "role": "a persona whose memory needs checking",
                    "background": "Exists so the repair command has something to check.",
                },
            }
        ),
        encoding="utf-8",
    )
    return path


@pytest.fixture
def chroma_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the CLI's config at a throwaway store."""
    home = tmp_path / "chroma"
    monkeypatch.setenv("PERSONA_CHROMA_PATH", str(home))
    return home


def _backend(home: Path) -> ChromaBackend:
    return ChromaBackend(persist_path=home, embedder=HashEmbedder())


def _chunk(version: int, *, superseded_by: str | None, chunk_id: str) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=f"version {version}",
        created_at=datetime.now(UTC),
        provenance=ChunkProvenance(
            source=WriteSource.USER,
            logical_id=_LOGICAL,
            version=version,
            superseded_by=superseded_by,
            written_at=datetime.now(UTC),
        ),
    )


def _seed_broken_chain(home: Path) -> None:
    """A chain left behind by an interrupted save: the link landed, the new version did not."""
    backend = _backend(home)
    backend.upsert(
        persona_id=_PERSONA_ID,
        store_kind="self_facts",
        chunks=[_chunk(1, superseded_by="never-written", chunk_id=_LOGICAL)],
    )


def test_a_healthy_store_gets_a_calm_answer(
    runner: CliRunner, persona_file: Path, chroma_home: Path
) -> None:
    """The commonest run by far. It must read as reassurance, not as an alarm."""
    backend = _backend(chroma_home)
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    store.write(
        _PERSONA_ID,
        [_chunk(1, superseded_by=None, chunk_id=_LOGICAL)],
        source=WriteSource.USER,
    )

    result = runner.invoke(app, ["repair", str(persona_file)])

    assert result.exit_code == 0, result.stdout
    assert "intact" in result.stdout
    assert "Nothing to repair" in result.stdout
    for alarming in ("ERROR", "WARNING", "corrupt", "damage", "broken"):
        assert alarming not in result.stdout, (
            f"a clean scan said {alarming!r}; a command that cries wolf on a healthy store "
            f"gets ignored the one time it matters: {result.stdout}"
        )


def test_a_broken_chain_is_reported_and_nothing_is_changed_without_apply(
    runner: CliRunner, persona_file: Path, chroma_home: Path
) -> None:
    _seed_broken_chain(chroma_home)

    result = runner.invoke(app, ["repair", str(persona_file)])

    assert result.exit_code == 1, result.stdout
    assert _LOGICAL in result.stdout
    assert "--apply" in result.stdout, "the report does not say how to act on it"

    rows = _backend(chroma_home).get_all(persona_id=_PERSONA_ID, store_kind="self_facts")
    assert rows[0].provenance is not None
    assert rows[0].provenance.superseded_by == "never-written", "a dry run changed the store"


def test_apply_repairs_the_chain_and_says_so(
    runner: CliRunner, persona_file: Path, chroma_home: Path
) -> None:
    _seed_broken_chain(chroma_home)

    result = runner.invoke(app, ["repair", str(persona_file), "--apply"])

    assert result.exit_code == 0, result.stdout
    assert "Repaired 1" in result.stdout
    assert "now intact" in result.stdout

    backend = _backend(chroma_home)
    rows = backend.get_all(persona_id=_PERSONA_ID, store_kind="self_facts")
    assert rows[0].provenance is not None
    assert rows[0].provenance.superseded_by is None
    # And the history reads again, which is what the operator actually wanted.
    store = SelfFactsStore(backend=backend, audit_logger=MemoryAuditLogger())
    assert [c.id for c in store.history(_PERSONA_ID, _LOGICAL)] == [_LOGICAL]


def test_a_chain_it_cannot_justify_is_reported_and_left_alone(
    runner: CliRunner, persona_file: Path, chroma_home: Path
) -> None:
    """--apply does not mean "do something to everything"."""
    backend = _backend(chroma_home)
    backend.upsert(
        persona_id=_PERSONA_ID,
        store_kind="self_facts",
        chunks=[
            _chunk(1, superseded_by=f"{_LOGICAL}::v0002", chunk_id=_LOGICAL),
            _chunk(3, superseded_by=None, chunk_id=f"{_LOGICAL}::v0003"),
        ],
    )

    result = runner.invoke(app, ["repair", str(persona_file), "--apply"])

    assert result.exit_code == 1, result.stdout
    assert "guessing" in result.stdout
    rows = _backend(chroma_home).get_all(persona_id=_PERSONA_ID, store_kind="self_facts")
    links = {c.id: (c.provenance.superseded_by if c.provenance else None) for c in rows}
    assert links == {_LOGICAL: f"{_LOGICAL}::v0002", f"{_LOGICAL}::v0003": None}, (
        "the repair rewrote a chain whose version order it cannot trust"
    )


def test_an_unknown_store_name_is_refused_before_anything_is_touched(
    runner: CliRunner, persona_file: Path, chroma_home: Path
) -> None:
    result = runner.invoke(app, ["repair", str(persona_file), "--store", "identity"])
    # Typer reports a bad option as a usage error (exit 2) on stderr, before the command body
    # runs, which is the point: an unknown store name never reaches a transport.
    assert result.exit_code == 2
    assert not chroma_home.exists(), "the command opened a store before validating its input"


def _seed_raced_chain(home: Path) -> None:
    """Two writers read version 1 and both wrote version 2. The first link names ``b``."""
    backend = _backend(home)
    backend.upsert(
        persona_id=_PERSONA_ID,
        store_kind="self_facts",
        chunks=[
            _chunk(1, superseded_by="b", chunk_id=_LOGICAL),
            _chunk(2, superseded_by=None, chunk_id="a"),
            _chunk(2, superseded_by=None, chunk_id="b"),
        ],
    )


def _versions(home: Path) -> dict[str, int]:
    rows = _backend(home).get_all(persona_id=_PERSONA_ID, store_kind="self_facts")
    return {c.id: c.provenance.version for c in rows if c.provenance is not None}


def test_a_raced_chain_is_reported_and_a_plain_apply_leaves_it(
    runner: CliRunner, persona_file: Path, chroma_home: Path
) -> None:
    _seed_raced_chain(chroma_home)

    result = runner.invoke(app, ["repair", str(persona_file), "--apply"])

    assert result.exit_code == 1
    assert "--resolve-raced" in plain(result), "the output has to say what the way out is"
    assert _versions(chroma_home) == {_LOGICAL: 1, "a": 2, "b": 2}


def test_the_flag_without_apply_changes_nothing(
    runner: CliRunner, persona_file: Path, chroma_home: Path
) -> None:
    """Dry run stays the default, and the flag only widens what ``--apply`` may do."""
    _seed_raced_chain(chroma_home)

    result = runner.invoke(app, ["repair", str(persona_file), "--resolve-raced"])

    assert result.exit_code == 1
    assert _versions(chroma_home) == {_LOGICAL: 1, "a": 2, "b": 2}


def test_the_flag_with_apply_settles_the_chain_on_the_real_transport(
    runner: CliRunner, persona_file: Path, chroma_home: Path
) -> None:
    _seed_raced_chain(chroma_home)

    result = runner.invoke(app, ["repair", str(persona_file), "--apply", "--resolve-raced"])

    assert result.exit_code == 0, result.output
    assert "now intact" in plain(result)
    assert _versions(chroma_home) == {_LOGICAL: 1, "a": 2, "b": 3}
    store = SelfFactsStore(backend=_backend(chroma_home), audit_logger=MemoryAuditLogger())
    assert [c.id for c in store.history(_PERSONA_ID, _LOGICAL)] == [_LOGICAL, "a", "b"]


def test_the_command_needs_to_know_what_to_check(runner: CliRunner, chroma_home: Path) -> None:
    result = runner.invoke(app, ["repair"])
    assert result.exit_code != 0
    assert not chroma_home.exists(), "refused before anything was opened"


def test_a_persona_and_all_personas_are_refused_together(
    runner: CliRunner, persona_file: Path, chroma_home: Path
) -> None:
    result = runner.invoke(
        app, ["repair", str(persona_file), "--all-personas", "--database-url", "postgresql://x"]
    )
    assert result.exit_code == 2, "a usage error, not a failed connection to that url"
    assert not chroma_home.exists()


def test_all_personas_needs_a_database(runner: CliRunner, chroma_home: Path) -> None:
    """A Chroma folder has no list of personas to sweep, and it must say so rather than guess."""
    result = runner.invoke(app, ["repair", "--all-personas"])
    assert result.exit_code != 0
    assert "--database-url" in plain(result)
    assert not chroma_home.exists()


def _seed_block_citing_nothing(home: Path) -> None:
    """A summary restored from before a forget reached it: its evidence is not on disk."""
    store = CoreMemoryStore(backend=_backend(home), audit_logger=MemoryAuditLogger())
    block = CoreBlock(text="a summary of something forgotten", source_ids=("gone",))
    store.write(
        _PERSONA_ID,
        [core_block_to_chunk(block, _PERSONA_ID, now=datetime.now(UTC))],
        source=WriteSource.SYSTEM,
    )


def _block_rows(home: Path) -> int:
    return len(_backend(home).get_all(persona_id=_PERSONA_ID, store_kind="core_memory"))


def test_the_sweep_is_a_dry_run_by_default(
    runner: CliRunner, persona_file: Path, chroma_home: Path
) -> None:
    _seed_block_citing_nothing(chroma_home)

    result = runner.invoke(app, ["repair", str(persona_file), "--forgotten-evidence"])

    assert result.exit_code == 1
    assert "--apply" in plain(result)
    assert "forgotten" not in plain(result).split("Nothing has been changed")[1].split("--apply")[0]
    assert "a summary of" not in plain(result), "ids and counts, never a word of the text"
    assert _block_rows(chroma_home) == 1


def test_the_sweep_with_apply_deletes_it_and_then_finds_nothing(
    runner: CliRunner, persona_file: Path, chroma_home: Path
) -> None:
    _seed_block_citing_nothing(chroma_home)

    result = runner.invoke(app, ["repair", str(persona_file), "--forgotten-evidence", "--apply"])

    assert result.exit_code == 0, result.output
    assert _block_rows(chroma_home) == 0
    again = runner.invoke(app, ["repair", str(persona_file), "--forgotten-evidence"])
    assert again.exit_code == 0
    assert "cites anything forgotten" in plain(again)


def test_the_sweep_runs_on_its_own(
    runner: CliRunner, persona_file: Path, chroma_home: Path
) -> None:
    result = runner.invoke(
        app, ["repair", str(persona_file), "--forgotten-evidence", "--resolve-raced"]
    )
    assert result.exit_code == 2
    assert not chroma_home.exists()


def test_the_command_loads_no_embedding_model(persona_file: Path, chroma_home: Path) -> None:
    """The claim that a repair costs nothing to start, checked rather than asserted.

    A real process, the real command, and then a look at what it imported. Loading
    sentence-transformers here would cost an operator about a hundred seconds on a cold cache
    to move one pointer, and on a host set up with a different model it could rewrite the
    stored vectors as a side effect of a repair.
    """
    script = (
        "import sys\n"
        "from typer.testing import CliRunner\n"
        "from persona.cli.main import app\n"
        f"r = CliRunner().invoke(app, ['repair', {str(persona_file)!r}])\n"
        "print('EXIT', r.exit_code)\n"
        "print('TORCH', 'torch' in sys.modules)\n"
        "print('ST', 'sentence_transformers' in sys.modules)\n"
    )
    done = subprocess.run(  # noqa: S603 - fixed argv, this interpreter
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "PERSONA_CHROMA_PATH": str(chroma_home)},
        check=False,
    )

    assert "EXIT 0" in done.stdout, f"{done.stdout}\n{done.stderr[-2000:]}"
    assert "TORCH False" in done.stdout, f"persona repair imported torch: {done.stdout}"
    assert "ST False" in done.stdout, "persona repair imported sentence-transformers"
