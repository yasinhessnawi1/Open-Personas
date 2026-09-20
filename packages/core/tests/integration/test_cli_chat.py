"""Integration tests for ``persona chat`` — spec §8 #6.

Verifies the REPL flow end-to-end against a ``MockChatBackend`` (injected
via monkeypatch) + a real ChromaBackend on tmp_path. Confirms episodic
memory from session N is retrievable in session N+1 (same
``PERSONA_CHROMA_PATH``).

Spec 02 deleted the `EchoBackend` stub from src/; production code never
ships a fake backend (D-02-12). Tests inject ``MockChatBackend`` from
``tests/_mock_backend.py`` by patching ``persona.cli.chat_cmd.load_backend``.

To keep these fast the chat runs with ``PERSONA_EMBEDDER=hash``, which is the real
selection path a person uses on a cold cache (Spec K13, T6) rather than a monkeypatch
around the model. That covers the wiring end to end: if the knob ever stops being read,
these tests either load the real model or lose the notice, and both are failures here.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from persona.cli.main import app
from typer.testing import CliRunner

from tests._mock_backend import MockChatBackend

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "personas" / "valid"


@pytest.fixture(autouse=True)
def stub_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace ``load_backend`` with a factory returning MockChatBackend."""
    monkeypatch.setattr(
        "persona.cli.chat_cmd.load_backend",
        lambda _config: MockChatBackend(),
    )


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _env(tmp_path: Path) -> dict[str, str]:
    return {
        # Mock backend is injected via monkeypatch, but BackendConfig() still
        # constructs — set provider + a dummy key so it validates cleanly.
        "PERSONA_PROVIDER": "anthropic",
        "PERSONA_API_KEY": "mock-key",
        "PERSONA_CHROMA_PATH": str(tmp_path / "chroma"),
        "PERSONA_AUDIT_PATH": str(tmp_path / "audit"),
        # The real knob, not a stub of the symbol behind it (Spec K13, D-K13-18). These
        # tests never wanted a real model; they wanted a cheap deterministic embedder, and
        # there is one in the product now, chosen the way a person chooses it.
        "PERSONA_EMBEDDER": "hash",
    }


def test_chat_replies_via_mock_backend(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for k, v in _env(tmp_path).items():
        monkeypatch.setenv(k, v)
    result = runner.invoke(
        app,
        ["chat", str(FIXTURES / "01_minimal.yaml")],
        input="hello there\n\n",
    )
    assert result.exit_code == 0, result.stderr
    assert "I would say: hello there" in result.stdout
    # The knob was read and the person was told. This is also what keeps the test honest:
    # without it, a broken knob would quietly load the real model here instead of failing.
    assert "semantic search is OFF" in result.stderr


def test_episodic_memory_persists_across_sessions(
    runner: CliRunner, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for k, v in _env(tmp_path).items():
        monkeypatch.setenv(k, v)

    persona_path = FIXTURES / "01_minimal.yaml"

    # Session N: write one turn.
    result1 = runner.invoke(app, ["chat", str(persona_path)], input="remember tenancy law\n\n")
    assert result1.exit_code == 0, result1.stderr

    # Session N+1: re-invoke. The same chroma path means session N's
    # episodic chunks should still be there.
    result2 = runner.invoke(app, ["chat", str(persona_path)], input="hi again\n\n")
    assert result2.exit_code == 0, result2.stderr

    # Read episodic store directly to verify both turns landed and survived.
    from persona.audit import MemoryAuditLogger
    from persona.stores import ChromaBackend, EpisodicStore, HashEmbedder

    # The SAME embedder the chat wrote with, so the collection's vectors and this reader
    # agree on their dimension. ``get_all`` does not embed, so the mismatch would not have
    # failed here, but it would be waiting for the first person to add a query below.
    backend = ChromaBackend(persist_path=tmp_path / "chroma", embedder=HashEmbedder())
    store = EpisodicStore(backend=backend, audit_logger=MemoryAuditLogger())
    chunks = store.get_all("01_minimal", include_superseded=True)
    assert len(chunks) >= 2, (
        f"expected >=2 episodic chunks across sessions, got {len(chunks)}: "
        f"{[c.text for c in chunks]}"
    )
    texts = " | ".join(c.text for c in chunks)
    assert "remember tenancy law" in texts
    assert "hi again" in texts
