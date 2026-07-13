"""The voice title-refresh writer + its bidirectional parity guard (R9-028).

Mirrors ``test_synthesis_enqueue.py`` byte-for-byte in structure:

1. **The writer builds the canonical core payload** — the shared idempotency
   key — and INSERTs it with the twin statement (proven against a fake
   connection, no DB).
2. **Bidirectional column-parity with the api ``jobs`` Table** — the voice
   INSERT's columns are ⊆ the real table (a renamed/dropped column fails CI)
   **and** ⊇ the table's NOT-NULL-without-server-default columns. Tests may
   cross layers, so this imports the api Table directly.
"""

from __future__ import annotations

import json

from persona.jobs import TitleRefreshJobPayload, title_refresh_idempotency_key
from persona_voice.session.title_enqueue import INSERTED_COLUMNS, enqueue_voice_title_refresh


class _FakeResult:
    def __init__(self, row: tuple[str, ...] | None) -> None:
        self._row = row

    def first(self) -> tuple[str, ...] | None:
        return self._row


class _FakeConn:
    def __init__(self, returned_id: str | None) -> None:
        self.executed: list[tuple[object, dict[str, object]]] = []
        self._returned_id = returned_id

    def __enter__(self) -> _FakeConn:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, statement: object, params: dict[str, object]) -> _FakeResult:
        self.executed.append((statement, params))
        return _FakeResult((self._returned_id,) if self._returned_id is not None else None)


class _FakeEngine:
    def __init__(self, returned_id: str | None = "job-1") -> None:
        self.conn = _FakeConn(returned_id)

    def begin(self) -> _FakeConn:
        return self.conn


def test_writer_builds_the_canonical_title_payload_and_inserts_it() -> None:
    engine = _FakeEngine(returned_id="job-xyz")
    job_id = enqueue_voice_title_refresh(
        engine,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="call-1",
        message_count=6,
    )
    assert job_id == "job-xyz"
    _stmt, params = engine.conn.executed[0]
    assert params["type"] == "title_refresh"
    assert params["owner_id"] == "owner-1"
    assert params["idempotency_key"] == "title:call-1:6"
    payload = json.loads(params["payload"])
    assert payload["conversation_id"] == "call-1"
    assert payload["threshold"] == 6
    # The params it binds are exactly the columns it claims to INSERT.
    assert set(params) == INSERTED_COLUMNS


def test_fires_unconditionally_even_at_zero_messages() -> None:
    # R9-028: "regardless of message count" — a call that never exchanged a
    # word still gets a (handler-side no-op-safe) enqueue, never gated here.
    engine = _FakeEngine(returned_id="job-zero")
    job_id = enqueue_voice_title_refresh(
        engine,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="call-empty",
        message_count=0,
    )
    assert job_id == "job-zero"
    _stmt, params = engine.conn.executed[0]
    assert params["idempotency_key"] == "title:call-empty:0"


def test_duplicate_enqueue_returns_none_on_conflict_noop() -> None:
    engine = _FakeEngine(returned_id=None)  # ON CONFLICT DO NOTHING → no row
    job_id = enqueue_voice_title_refresh(
        engine,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="call-1",
        message_count=6,
    )
    assert job_id is None


def test_key_matches_the_shared_core_contract() -> None:
    # The writer's key must equal what the core contract produces for the same
    # call — the api writer builds the identical key, so a re-enqueue from
    # either dedups.
    payload = TitleRefreshJobPayload(conversation_id="call-1", threshold=6)
    assert title_refresh_idempotency_key(payload) == "title:call-1:6"


def test_inserted_columns_are_a_subset_of_the_api_jobs_table() -> None:
    # Direction 1: every column the voice writer INSERTs exists on the real table.
    # A renamed/dropped column fails here, statically.
    from persona_api.db.models import jobs

    table_columns = set(jobs.columns.keys())
    assert table_columns >= INSERTED_COLUMNS, INSERTED_COLUMNS - table_columns


def test_inserted_columns_cover_every_required_api_jobs_column() -> None:
    # Direction 2: every NOT-NULL column without a server default MUST be supplied
    # by the voice writer — otherwise an api migration adding a required column
    # would only fail at runtime. Server-defaulted columns (id/state/priority/
    # …/created_at) are deliberately omitted (the DB fills them, exactly as api's
    # pg_insert relies on).
    from persona_api.db.models import jobs

    required = {
        c.name
        for c in jobs.columns
        if not c.nullable and c.server_default is None and c.default is None
    }
    missing = required - INSERTED_COLUMNS
    assert not missing, f"voice enqueue omits required jobs column(s): {missing}"
