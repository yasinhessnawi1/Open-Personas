"""Unit tests for the voice transcript writer (V9-D-2 + R9-001).

The happy-path byte-for-byte write is proven against a real Postgres + the api
read surface in the api integration suite (``test_conversations`` —
``test_voice_turn_persists_to_messages_and_renders``). Here we lock:

- the best-effort discipline in isolation: a DB error during the write MUST NOT
  raise (``record_turn`` runs in V4's turn-commit ``finally`` — a
  transcript-write failure degrades the saved transcript, never the live call);
- the R9-001 synthetic-turn contract at the row level: ``user_text=None``
  persists the ASSISTANT row only (the internal greeting nudge / narration
  prompt never becomes a user bubble), while a normal turn still persists
  user-before-assistant deterministically.

The row-level tests run against a hand-created SQLite ``messages`` table — the
core Table view's JSONB columns bind-serialize fine on SQLite for DML (only
``create_all`` is Postgres-bound), so the writer's real insert path is exercised.
"""

from __future__ import annotations

from datetime import UTC, datetime

from persona_voice.model.transcript import VoiceTranscriptWriter
from sqlalchemy import Engine, create_engine, text

_NOW = datetime(2026, 6, 25, 12, 0, 0, tzinfo=UTC)


def _engine_with_messages_table() -> Engine:
    """An in-memory SQLite engine carrying a ``messages`` table the writer can hit."""
    engine = create_engine("sqlite://")
    with engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE messages (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tool_calls TEXT,
                    channel TEXT,
                    images TEXT,
                    tier_used TEXT,
                    originated BOOLEAN NOT NULL DEFAULT 0,
                    streaming_status TEXT,
                    stream_events TEXT,
                    created_at TIMESTAMP NOT NULL
                )
                """
            )
        )
    return engine


def _rows(engine: Engine) -> list[tuple[str, str]]:
    with engine.begin() as conn:
        result = conn.execute(text("SELECT role, content FROM messages ORDER BY created_at, role"))
        return [(row.role, row.content) for row in result]


def test_record_turn_does_not_raise_on_db_error() -> None:
    # A SQLite engine with NO ``messages`` table → the insert raises "no such
    # table", which the writer must swallow (best-effort, like the episodic write).
    engine = create_engine("sqlite://")
    writer = VoiceTranscriptWriter(engine=engine, conversation_id="c1")
    writer.record_turn(
        user_text="what are my rights?",
        heard_text="you have strong rights.",
        truncated=False,
        now=_NOW,
    )  # must not raise


def test_normal_turn_persists_user_then_assistant() -> None:
    """A real spoken turn persists BOTH rows, user before assistant (V9-D-2)."""
    engine = _engine_with_messages_table()
    writer = VoiceTranscriptWriter(engine=engine, conversation_id="c1")

    writer.record_turn(
        user_text="what are my rights?",
        heard_text="you have strong rights.",
        truncated=False,
        now=_NOW,
    )

    assert _rows(engine) == [
        ("user", "what are my rights?"),
        ("assistant", "you have strong rights."),
    ]


def test_synthetic_turn_persists_exactly_one_assistant_row() -> None:  # R9-001
    """``user_text=None`` (a synthetic turn — the greeting nudge) persists the
    persona's reply ONLY: exactly one row, role assistant, no user bubble."""
    engine = _engine_with_messages_table()
    writer = VoiceTranscriptWriter(engine=engine, conversation_id="c1")

    writer.record_turn(
        user_text=None,
        heard_text="Hei! So glad you called.",
        truncated=False,
        now=_NOW,
    )

    assert _rows(engine) == [("assistant", "Hei! So glad you called.")]
