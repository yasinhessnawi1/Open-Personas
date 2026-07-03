"""The voice synthesis-enqueue writer + its bidirectional parity guard (Spec V13, T5).

Two things pinned here (D-4-amended riders):

1. **The writer builds the canonical core payload** — channel ``voice``,
   interaction_kind ``conversation``, the shared idempotency key — and INSERTs it
   with the twin statement (proven against a fake connection, no DB).
2. **Bidirectional column-parity with the api ``jobs`` Table** — the voice INSERT's
   columns are ⊆ the real table (a renamed/dropped column fails CI) **and** ⊇ the
   table's NOT-NULL-without-server-default columns (a newly-added required column
   api adds fails CI too, instead of only at runtime). Tests may cross layers, so
   this imports the api Table directly.
"""

from __future__ import annotations

import json

from persona.jobs import CHANNEL_VOICE, synthesis_idempotency_key
from persona.jobs.synthesis import make_conversation_synthesis_payload
from persona_voice.session.synthesis_enqueue import INSERTED_COLUMNS, enqueue_voice_synthesis


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


def test_writer_builds_the_canonical_voice_payload_and_inserts_it() -> None:
    engine = _FakeEngine(returned_id="job-xyz")
    job_id = enqueue_voice_synthesis(
        engine,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="call-1",
        persona_id="p1",
        message_count=6,
    )
    assert job_id == "job-xyz"
    _stmt, params = engine.conn.executed[0]
    assert params["type"] == "synthesis"
    assert params["owner_id"] == "owner-1"
    assert params["idempotency_key"] == "synthesis:conversation:call-1:6"
    payload = json.loads(params["payload"])
    assert payload["channel"] == CHANNEL_VOICE
    assert payload["interaction_kind"] == "conversation"
    assert payload["interaction_id"] == "call-1"
    assert payload["high_water_mark"] == 6
    # The params it binds are exactly the columns it claims to INSERT.
    assert set(params) == INSERTED_COLUMNS


def test_duplicate_enqueue_returns_none_on_conflict_noop() -> None:
    engine = _FakeEngine(returned_id=None)  # ON CONFLICT DO NOTHING → no row
    job_id = enqueue_voice_synthesis(
        engine,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="call-1",
        persona_id="p1",
        message_count=6,
    )
    assert job_id is None


def test_key_matches_the_shared_core_contract() -> None:
    # The writer's key must equal what the core contract produces for the same call —
    # the api writer builds the identical key, so a re-enqueue from either dedups.
    payload = make_conversation_synthesis_payload(
        conversation_id="call-1", persona_id="p1", message_count=6, channel=CHANNEL_VOICE
    )
    assert synthesis_idempotency_key(payload) == "synthesis:conversation:call-1:6"


def test_inserted_columns_are_a_subset_of_the_api_jobs_table() -> None:
    # Direction 1: every column the voice writer INSERTs exists on the real table.
    # A renamed/dropped column fails here, statically.
    from persona_api.db.models import jobs

    table_columns = set(jobs.columns.keys())
    assert table_columns >= INSERTED_COLUMNS, INSERTED_COLUMNS - table_columns


def test_inserted_columns_cover_every_required_api_jobs_column() -> None:
    # Direction 2: every NOT-NULL column without a server default MUST be supplied by
    # the voice writer — otherwise an api migration adding a required column would only
    # fail at runtime. Server-defaulted columns (id/state/priority/…/created_at) are
    # deliberately omitted (the DB fills them, exactly as api's pg_insert relies on).
    from persona_api.db.models import jobs

    required = {
        c.name
        for c in jobs.columns
        if not c.nullable and c.server_default is None and c.default is None
    }
    missing = required - INSERTED_COLUMNS
    assert not missing, f"voice enqueue omits required jobs column(s): {missing}"
