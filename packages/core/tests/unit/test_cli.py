"""Tests for ``persona.cli``.

Uses typer's ``CliRunner`` to invoke commands without spawning a subprocess.
The ``persona chat`` REPL is exercised in the integration suite where it can
talk to a real ChromaBackend (this file covers validate/init/audit/run).
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from persona.cli.main import app
from typer.testing import CliRunner

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "personas"
VALID_FIXTURES = sorted((FIXTURES / "valid").glob("*.yaml"))
INVALID_FIXTURES = sorted((FIXTURES / "invalid").glob("*.yaml"))


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


# -- validate ---------------------------------------------------------------


class TestValidateCommand:
    @pytest.mark.parametrize("fixture", VALID_FIXTURES, ids=lambda p: p.name)
    def test_exits_zero_on_valid(self, runner: CliRunner, fixture: Path) -> None:
        result = runner.invoke(app, ["validate", str(fixture)])
        assert result.exit_code == 0, result.stderr
        assert "valid" in result.stdout

    @pytest.mark.parametrize("fixture", INVALID_FIXTURES, ids=lambda p: p.name)
    def test_exits_one_on_invalid(self, runner: CliRunner, fixture: Path) -> None:
        result = runner.invoke(app, ["validate", str(fixture)])
        assert result.exit_code == 1, result.stdout


# -- init -------------------------------------------------------------------


class TestInitCommand:
    def test_writes_a_valid_yaml(self, runner: CliRunner, tmp_path: Path) -> None:
        output = tmp_path / "new_persona.yaml"
        # typer.prompt reads from stdin one line at a time; emulate the
        # ordered-prompts flow: persona_id, name, role, background,
        # language_default, constraints empty, tools empty, skills empty.
        responses = (
            "\n".join(
                [
                    "",  # persona_id (accept default)
                    "Astrid",  # name
                    "tester role",  # role
                    "Background text",  # background
                    "",  # language_default (default en)
                    "",  # constraints[+] (empty → exit list)
                    "",  # tools (empty → skip)
                    "",  # skills (empty → skip)
                ]
            )
            + "\n"
        )
        result = runner.invoke(app, ["init", "--output", str(output)], input=responses)
        assert result.exit_code == 0, result.stderr
        assert output.exists()
        doc = yaml.safe_load(output.read_text(encoding="utf-8"))
        assert doc["identity"]["name"] == "Astrid"
        assert doc["schema_version"] == "1.0"

        # And the file we wrote is validatable.
        validate_result = runner.invoke(app, ["validate", str(output)])
        assert validate_result.exit_code == 0

    def test_init_with_from_is_stub(self, runner: CliRunner, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "init",
                "--output",
                str(tmp_path / "x.yaml"),
                "--from",
                "a Norwegian legal assistant",
            ],
        )
        assert result.exit_code == 2
        assert "LLM-assisted" in result.stderr

    def test_init_refuses_to_overwrite_without_confirmation(
        self, runner: CliRunner, tmp_path: Path
    ) -> None:
        existing = tmp_path / "p.yaml"
        existing.write_text("existing: true\n", encoding="utf-8")
        # Default for confirm is "no"; sending an empty line should abort.
        result = runner.invoke(app, ["init", "--output", str(existing)], input="\n")
        assert result.exit_code == 0
        assert "aborted" in result.stdout


# -- run --------------------------------------------------------------------


class TestRunCommand:
    def test_run_is_stub_with_exit_code_two(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["run", "any", "task"])
        assert result.exit_code == 2
        assert "persona-runtime" in result.stderr


# -- audit ------------------------------------------------------------------


class TestAuditCommand:
    def test_audit_on_missing_log_prints_no_events(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Redirect both chroma + audit roots into tmp_path so the audit log
        # we read from doesn't exist (it will simply have no events).
        monkeypatch.setenv("PERSONA_CHROMA_PATH", str(tmp_path / "chroma"))
        monkeypatch.setenv("PERSONA_AUDIT_PATH", str(tmp_path / "audit_dir"))
        result = runner.invoke(app, ["audit", str(VALID_FIXTURES[0])])
        assert result.exit_code == 0, result.stderr
        assert "no audit events" in result.stdout

    def test_audit_rejects_unknown_store_filter(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PERSONA_CHROMA_PATH", str(tmp_path / "chroma"))
        monkeypatch.setenv("PERSONA_AUDIT_PATH", str(tmp_path / "audit_dir"))
        result = runner.invoke(
            app,
            ["audit", str(VALID_FIXTURES[0]), "--store", "bogus"],
        )
        assert result.exit_code != 0

    @pytest.mark.parametrize(
        "store",
        [
            "identity",
            "self_facts",
            "worldview",
            "episodic",
            "episodic_gist",
            "core_memory",
            "knowledge_graph",
            "skill",
        ],
    )
    def test_audit_accepts_every_current_store_kind(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        store: str,
    ) -> None:
        """R9-031's "same class of gap": ``--store`` used to hardcode the
        original four typed stores, silently rejecting every kind added since
        (episodic_gist, knowledge_graph, skill — and now core_memory). It is
        now derived from ``persona.audit.StoreKind`` itself, so it can never
        drift again — this pins every CURRENT member accepted, not a
        hand-copied subset."""
        monkeypatch.setenv("PERSONA_CHROMA_PATH", str(tmp_path / "chroma"))
        monkeypatch.setenv("PERSONA_AUDIT_PATH", str(tmp_path / "audit_dir"))
        result = runner.invoke(
            app,
            ["audit", str(VALID_FIXTURES[0]), "--store", store],
        )
        assert result.exit_code == 0, result.stderr
        assert "no audit events" in result.stdout

    def test_audit_invalid_since_value_is_rejected(
        self,
        runner: CliRunner,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("PERSONA_CHROMA_PATH", str(tmp_path / "chroma"))
        monkeypatch.setenv("PERSONA_AUDIT_PATH", str(tmp_path / "audit_dir"))
        result = runner.invoke(
            app,
            ["audit", str(VALID_FIXTURES[0]), "--since", "tomorrow"],
        )
        assert result.exit_code != 0


# -- help -------------------------------------------------------------------


class TestHelp:
    def test_top_level_help(self, runner: CliRunner) -> None:
        result = runner.invoke(app, ["--help"])
        assert result.exit_code == 0
        for cmd in ("init", "validate", "chat", "audit", "run"):
            assert cmd in result.stdout
