"""The voice delegated-turn enqueue writer + its bidirectional parity guard (Spec A9, T4; A9-D-5).

Pinned here (the ``enqueue_voice_synthesis`` discipline, applied to A9's crossing):

1. **The writer builds the canonical core payload** — the verbatim ask, provenance ``voice``, the
   shared idempotency key — and INSERTs it with the twin statement (proven on a fake connection).
2. **Idempotency = draft-hash-primary over the verbatim ask + conversation** — a double-confirm /
   redelivery converges (same key); a different ask ⇒ a different key; the ``dedup_token`` escape
   hatch overrides the ask hash (the deliberate same-phrase repeat).
3. **Bidirectional column-parity with the api ``jobs`` Table** — the voice INSERT's columns are ⊆
   the real table (a renamed/dropped column fails CI) **and** ⊇ its NOT-NULL-without-default cols (a
   newly-added required api column fails CI too, instead of only at runtime).
"""

from __future__ import annotations

import json

from persona.jobs import (
    DELEGATED_TURN_JOB_TYPE,
    PROVENANCE_VOICE,
    delegated_turn_idempotency_key,
    make_delegated_turn_payload,
)
from persona_voice.session.delegation_enqueue import INSERTED_COLUMNS, enqueue_delegated_turn


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


def test_writer_builds_the_canonical_delegation_payload_and_inserts_it() -> None:
    engine = _FakeEngine(returned_id="job-xyz")
    job_id = enqueue_delegated_turn(
        engine,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="call-1",
        verbatim_ask="brief me every morning",
        persona_id="p1",
    )
    assert job_id == "job-xyz"
    _stmt, params = engine.conn.executed[0]
    assert params["type"] == DELEGATED_TURN_JOB_TYPE
    assert params["owner_id"] == "owner-1"
    payload = json.loads(params["payload"])  # type: ignore[arg-type]
    # The VERBATIM ask crosses — never a parsed draft (A9-D-5).
    assert payload["verbatim_ask"] == "brief me every morning"
    assert payload["conversation_id"] == "call-1"
    assert payload["persona_id"] == "p1"
    assert payload["provenance"] == PROVENANCE_VOICE
    # The params it binds are exactly the columns it claims to INSERT.
    assert set(params) == INSERTED_COLUMNS


def test_duplicate_enqueue_returns_none_on_conflict_noop() -> None:
    engine = _FakeEngine(returned_id=None)  # ON CONFLICT DO NOTHING → no row
    job_id = enqueue_delegated_turn(
        engine,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id="call-1",
        verbatim_ask="brief me every morning",
        persona_id="p1",
    )
    assert job_id is None


def test_key_matches_the_shared_core_contract() -> None:
    # The writer's key must equal what the core contract produces for the same ask — the api-side
    # re-enqueue builds the identical key, so a re-enqueue from either dedups.
    payload = make_delegated_turn_payload(
        conversation_id="call-1", verbatim_ask="brief me every morning", persona_id="p1"
    )
    _stmt, params = _captured_params(verbatim_ask="brief me every morning")
    assert params["idempotency_key"] == delegated_turn_idempotency_key(payload)


def test_same_ask_same_conversation_dedups_different_ask_does_not() -> None:
    key_a = _captured_params(verbatim_ask="brief me every morning")[1]["idempotency_key"]
    key_a2 = _captured_params(verbatim_ask="brief me every morning")[1]["idempotency_key"]
    key_b = _captured_params(verbatim_ask="brief me every evening")[1]["idempotency_key"]
    assert key_a == key_a2  # a double-confirm / redelivery converges (over-dedup bias)
    assert key_a != key_b  # a genuinely-distinct ask is a distinct delegation


def test_same_ask_different_conversation_is_distinct() -> None:
    key_1 = _captured_params(verbatim_ask="remind me daily", conversation_id="call-1")[1]
    key_2 = _captured_params(verbatim_ask="remind me daily", conversation_id="call-2")[1]
    assert key_1["idempotency_key"] != key_2["idempotency_key"]


def test_dedup_token_escape_hatch_overrides_the_ask_hash() -> None:
    # The gate-minted token lets a deliberate same-phrase repeat be a DISTINCT delegation (A9-D-5).
    payload_a = make_delegated_turn_payload(
        conversation_id="c", verbatim_ask="remind me daily", persona_id="p", dedup_token="tok-1"
    )
    payload_b = make_delegated_turn_payload(
        conversation_id="c", verbatim_ask="remind me daily", persona_id="p", dedup_token="tok-2"
    )
    assert delegated_turn_idempotency_key(payload_a) != delegated_turn_idempotency_key(payload_b)


def test_inserted_columns_are_a_subset_of_the_api_jobs_table() -> None:
    # Direction 1: every column the voice writer INSERTs exists on the real table.
    from persona_api.db.models import jobs

    table_columns = set(jobs.columns.keys())
    assert table_columns >= INSERTED_COLUMNS, INSERTED_COLUMNS - table_columns


def test_inserted_columns_cover_every_required_api_jobs_column() -> None:
    # Direction 2: every NOT-NULL column without a server default MUST be supplied by the voice
    # writer — otherwise an api migration adding a required column would only fail at runtime.
    from persona_api.db.models import jobs

    required = {
        c.name
        for c in jobs.columns
        if not c.nullable and c.server_default is None and c.default is None
    }
    missing = required - INSERTED_COLUMNS
    assert not missing, f"voice delegation enqueue omits required jobs column(s): {missing}"


def _captured_params(
    *, verbatim_ask: str, conversation_id: str = "call-1"
) -> tuple[object, dict[str, object]]:
    engine = _FakeEngine()
    enqueue_delegated_turn(
        engine,  # type: ignore[arg-type]
        owner_id="owner-1",
        conversation_id=conversation_id,
        verbatim_ask=verbatim_ask,
        persona_id="p1",
    )
    return engine.conn.executed[0]
