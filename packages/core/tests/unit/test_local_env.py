"""Unit tests for ``persona.local_env`` env-bootstrap defaults (R9-017)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from persona.local_env import load_local_env

if TYPE_CHECKING:
    import pytest


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
