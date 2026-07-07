"""The delegated-turn job contract — the ONE definition both writers share (Spec A9, T4; A9-D-5)."""

from __future__ import annotations

import hashlib

import pytest
from persona.jobs import (
    DELEGATED_TURN_JOB_TYPE,
    PROVENANCE_VOICE,
    DelegatedTurnPayload,
    delegated_turn_idempotency_key,
    make_delegated_turn_payload,
)
from pydantic import ValidationError


def test_job_type_string_is_stable() -> None:
    assert DELEGATED_TURN_JOB_TYPE == "delegated_turn"


def test_payload_is_frozen_and_forbids_extra() -> None:
    payload = make_delegated_turn_payload(
        conversation_id="c1", verbatim_ask="brief me every morning", persona_id="p1"
    )
    with pytest.raises(ValidationError):
        DelegatedTurnPayload(  # extra field rejected (extra="forbid")
            conversation_id="c1",
            verbatim_ask="x",
            persona_id="p1",
            surprise="nope",  # type: ignore[call-arg]
        )
    with pytest.raises(ValidationError):
        payload.conversation_id = "c2"  # frozen  # type: ignore[misc]


def test_payload_defaults_provenance_voice_and_no_token() -> None:
    payload = make_delegated_turn_payload(conversation_id="c1", verbatim_ask="x", persona_id="p1")
    assert payload.provenance == PROVENANCE_VOICE
    assert payload.dedup_token is None


def test_key_is_draft_hash_primary_over_ask_and_conversation() -> None:
    ask = "brief me every morning"
    payload = make_delegated_turn_payload(
        conversation_id="call-1", verbatim_ask=ask, persona_id="p"
    )
    expected_anchor = hashlib.sha256(ask.encode("utf-8")).hexdigest()
    assert delegated_turn_idempotency_key(payload) == f"delegate:call-1:{expected_anchor}"


def test_dedup_token_overrides_the_ask_hash() -> None:
    payload = make_delegated_turn_payload(
        conversation_id="call-1", verbatim_ask="anything", persona_id="p", dedup_token="tok-9"
    )
    assert delegated_turn_idempotency_key(payload) == "delegate:call-1:tok-9"


def test_same_ask_same_conversation_produces_the_same_key() -> None:
    a = make_delegated_turn_payload(conversation_id="c", verbatim_ask="remind me", persona_id="p")
    b = make_delegated_turn_payload(conversation_id="c", verbatim_ask="remind me", persona_id="p2")
    # persona differs, but the key is over ask + conversation only → converges (one call, one turn).
    assert delegated_turn_idempotency_key(a) == delegated_turn_idempotency_key(b)


def test_different_conversation_is_a_distinct_key() -> None:
    a = make_delegated_turn_payload(conversation_id="c1", verbatim_ask="remind me", persona_id="p")
    b = make_delegated_turn_payload(conversation_id="c2", verbatim_ask="remind me", persona_id="p")
    assert delegated_turn_idempotency_key(a) != delegated_turn_idempotency_key(b)
