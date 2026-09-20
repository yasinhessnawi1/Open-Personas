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
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.chroma import ChromaBackend
from persona.stores.self_facts import SelfFactsStore
from typer.testing import CliRunner

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
