"""Unit tests for ``persona.local_env`` env-bootstrap defaults (R9-017)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

import pytest
from persona.local_env import load_local_env

if TYPE_CHECKING:
    from pathlib import Path


def _isolate(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise every side effect of ``load_local_env`` except the var under test.

    ``_repo_root`` → ``None`` skips the ``.env`` load + Clerk pem; a stubbed
    ``SSL_CERT_FILE`` skips the certifi mutation; a cleared ``DATABASE_URL``
    skips the DB-url derivation. What remains observable is the
    ``TOKENIZERS_PARALLELISM`` setdefault.
    """
    monkeypatch.setattr("persona.local_env._repo_root", lambda: None)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", "stub")  # skip certifi.where() mutation


class TestTokenizersParallelismDefault:
    def test_defaults_to_false_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _isolate(monkeypatch)
        monkeypatch.delenv("TOKENIZERS_PARALLELISM", raising=False)
        load_local_env()
        assert os.environ["TOKENIZERS_PARALLELISM"] == "false"

    def test_preserves_explicit_override(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # setdefault must not clobber an operator's explicit choice.
        _isolate(monkeypatch)
        monkeypatch.setenv("TOKENIZERS_PARALLELISM", "true")
        load_local_env()
        assert os.environ["TOKENIZERS_PARALLELISM"] == "true"


@pytest.fixture
def fake_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A throwaway checkout root carrying one ``.env`` with a single marker var.

    Points ``_repo_root`` at ``tmp_path`` so the real repo ``.env`` is never read
    and the developer's own secrets never enter the assertion.
    """
    (tmp_path / ".env").write_text("PERSONA_DOTENV_MARKER=loaded\n")
    monkeypatch.setattr("persona.local_env._repo_root", lambda: tmp_path)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SSL_CERT_FILE", "stub")
    # setenv-then-delenv so monkeypatch records these names and removes them at
    # teardown even on the branch where the load puts them into os.environ.
    for leaked in ("PERSONA_DOTENV_MARKER", "APP_DATABASE_URL"):
        monkeypatch.setenv(leaked, "")
        monkeypatch.delenv(leaked)
    return tmp_path


@pytest.mark.usefixtures("fake_checkout")
class TestDotenvLoadGate:
    """``PERSONA_DOTENV_LOAD`` is the documented opt-out, so it has to be real.

    ``.env.example`` has advertised this gate since spec 02 (D-02-4) while the
    loader read ``.env`` unconditionally. The branch below is what makes the
    documented line true.
    """

    def test_loads_by_default_when_unset(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PERSONA_DOTENV_LOAD", raising=False)
        load_local_env()
        assert os.environ.get("PERSONA_DOTENV_MARKER") == "loaded"

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "on", "anything-else"])
    def test_loads_for_any_non_falsy_value(
        self, monkeypatch: pytest.MonkeyPatch, truthy: str
    ) -> None:
        monkeypatch.setenv("PERSONA_DOTENV_LOAD", truthy)
        load_local_env()
        assert os.environ.get("PERSONA_DOTENV_MARKER") == "loaded"

    @pytest.mark.parametrize("falsy", ["0", "false", "FALSE", "no", "off", " false "])
    def test_skips_the_file_for_a_falsy_value(
        self, monkeypatch: pytest.MonkeyPatch, falsy: str
    ) -> None:
        monkeypatch.setenv("PERSONA_DOTENV_LOAD", falsy)
        load_local_env()
        assert "PERSONA_DOTENV_MARKER" not in os.environ

    def test_the_rest_of_the_bootstrap_still_runs_when_the_file_is_skipped(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The gate is about the FILE, not about the dev-derived vars.

        An operator who exports everything themselves still wants the
        ``APP_DATABASE_URL`` derivation and the tokenizers default.
        """
        monkeypatch.setenv("PERSONA_DOTENV_LOAD", "0")
        monkeypatch.delenv("TOKENIZERS_PARALLELISM", raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://persona:pw@127.0.0.1:5436/db")
        load_local_env()
        assert os.environ["TOKENIZERS_PARALLELISM"] == "false"
        assert "persona_app" in os.environ["APP_DATABASE_URL"]
