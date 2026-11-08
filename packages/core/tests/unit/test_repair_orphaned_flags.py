"""``persona repair --orphaned`` is a whole-database sweep, and says so when asked for less.

Orphans are rows whose persona or conversation is gone, so finding them takes the table those
owners live in. One persona's YAML cannot name them, and a local Chroma folder has no such table.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - used at runtime in fixtures

import pytest
from persona.cli import repair_cmd
from persona.cli.main import app
from persona.cli.repair_cmd import run_orphan_sweep
from persona.stores.orphans import OrphanGroup
from typer.testing import CliRunner

_DSN = "postgresql+psycopg://nobody@127.0.0.1:1/never"


@pytest.fixture(autouse=True)
def _no_connection_is_attempted(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every refusal here comes before the database is touched, so reaching it is a failure."""

    def _reached(*_args: object) -> None:
        msg = "the flags should have been refused before connecting"
        raise AssertionError(msg)

    monkeypatch.setattr(repair_cmd, "_build_target", _reached)


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        (["--all-personas", "--database-url", _DSN, "--resolve-raced"], "--orphaned"),
        (["--all-personas", "--database-url", _DSN, "--forgotten-evidence"], "--orphaned"),
        (["--all-personas", "--database-url", _DSN, "--store", "episodic"], "--orphaned"),
    ],
)
def test_it_runs_on_its_own(extra: list[str], needle: str) -> None:
    result = CliRunner().invoke(app, ["repair", "--orphaned", *extra])
    assert result.exit_code == 2, result.output
    assert needle in result.output


def test_it_refuses_a_single_persona(tmp_path: Path) -> None:
    yaml_path = tmp_path / "p.yaml"
    yaml_path.write_text("x: 1", encoding="utf-8")
    result = CliRunner().invoke(
        app, ["repair", str(yaml_path), "--orphaned", "--database-url", _DSN]
    )
    assert result.exit_code == 2, result.output
    assert "--all-personas" in result.output


def test_it_needs_a_database() -> None:
    result = CliRunner().invoke(app, ["repair", "--all-personas", "--orphaned"], env={})
    assert result.exit_code == 2, result.output
    assert "--database-url" in result.output


class _FakeSweep:
    """Orphans that a removal may or may not clear."""

    def __init__(self, *, clears: bool) -> None:
        self._groups = [OrphanGroup("memory_chunks", "persona_x", "episodic", 2)]
        self._clears = clears
        self.removed_by: list[str] = []

    def find(self) -> list[OrphanGroup]:
        return list(self._groups)

    def remove(self, group: OrphanGroup, *, written_by: str) -> int:
        self.removed_by.append(written_by)
        if self._clears:
            self._groups = []
        return group.rows


def test_a_dry_run_removes_nothing_and_says_there_is_work() -> None:
    sweep = _FakeSweep(clears=True)
    assert run_orphan_sweep(sweep) == 1  # type: ignore[arg-type]
    assert sweep.removed_by == []


def test_apply_reports_success_only_when_nothing_is_left() -> None:
    sweep = _FakeSweep(clears=True)
    assert run_orphan_sweep(sweep, apply=True, written_by="who") == 0  # type: ignore[arg-type]
    assert sweep.removed_by == ["who"]


def test_rows_still_there_after_the_sweep_are_not_reported_as_clean() -> None:
    sweep = _FakeSweep(clears=False)
    assert run_orphan_sweep(sweep, apply=True) == 1  # type: ignore[arg-type]
