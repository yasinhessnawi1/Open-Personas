"""The pending-proposal rail's persistence half (R4 operator find — the rail escape).

The live escape: turn 1's contract echo tags its assistant message with
``metadata["contract_proposal"]`` — but only on the IN-MEMORY ``Conversation``. The
``messages`` table has no metadata column, ``MessagesTurnSink.finalize`` never writes it,
and ``chat_service._to_message`` rebuilds reloaded messages without it. Every HTTP turn
reloads the conversation from the DB (``start_chat_turn`` → ``_load_conversation``), so on
the NEXT real turn ``_pending_contract_draft`` finds nothing, the rail silently vanishes,
and the reply ("once at 9 30") runs as a free model turn — the R4-C1-20 .ics confabulation.

These tests pin the round-trip contract at the production seam: the metadata the loop
leaves on the final assistant message must survive finalize → reload. They drive the REAL
sink + the REAL loader over the community engine (no manual SQL on the write side).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING

import pytest
from persona.schema.conversation import Conversation, ConversationMessage
from persona_api.db.community import (
    create_community_schema,
    ensure_owner,
    make_community_engine,
)
from persona_api.db.models import conversations as conversations_t
from persona_api.db.models import personas as personas_t
from persona_api.middleware.rls_context import current_user_id
from persona_api.services.chat_service import _load_conversation
from persona_api.services.chat_turn_sink import MessagesTurnSink
from sqlalchemy import insert

if TYPE_CHECKING:
    from collections.abc import Iterator

    from sqlalchemy import Engine

_OWNER = "user_alice"
_PERSONA = "astrid"
_CONV = "conv_rail"

# The draft shape the loop persists (ContractDraft.model_dump_json() — opaque here; the
# rail contract is that the STRING round-trips, not that the api layer parses it).
_DRAFT_JSON = '{"goal":"check email inbox","scope":"","grants":[],"schedule":null}'


@pytest.fixture
def engine(tmp_path: object) -> Iterator[Engine]:
    eng = make_community_engine(tmp_path / "t.db")  # type: ignore[operator]
    create_community_schema(eng)
    ensure_owner(eng, owner_id=_OWNER, email="a@example.com")
    with eng.begin() as conn:
        conn.execute(insert(personas_t).values(id=_PERSONA, owner_id=_OWNER, yaml="name: Astrid"))
        conn.execute(insert(conversations_t).values(id=_CONV, owner_id=_OWNER, persona_id=_PERSONA))
    token = current_user_id.set(_OWNER)
    try:
        yield eng
    finally:
        current_user_id.reset(token)


def _finalize_turn_with_metadata(engine: Engine, metadata: dict[str, str]) -> None:
    """Run one turn the way production does: open_turn, then finalize with the conversation
    the loop mutated in place (its last assistant message carrying ``metadata``)."""
    sink = MessagesTurnSink(engine)
    echo = "Goal: check email inbox\nWhen: once…\nShall I go ahead?"
    assistant_id = sink.open_turn(
        conversation_id=_CONV,
        user_message="schedule a task to check my email inbox",
        channel=None,
        images=None,
    )
    now = datetime.now(UTC)
    conversation = Conversation(
        conversation_id=_CONV,
        persona_id=_PERSONA,
        messages=[
            ConversationMessage(
                role="user",
                content="schedule a task to check my email inbox",
                created_at=now,
            ),
            ConversationMessage(role="assistant", content=echo, created_at=now, metadata=metadata),
        ],
    )
    sink.finalize(
        conversation_id=_CONV,
        assistant_message_id=assistant_id,
        conversation=conversation,
        status="complete",
        content=echo,
        events=[],
        tier="small",
    )


def _reload(engine: Engine) -> Conversation:
    with engine.begin() as conn:
        return _load_conversation(conn, _CONV)


def test_contract_proposal_metadata_survives_finalize_then_reload(engine: Engine) -> None:
    """THE rail escape (R4): the pending contract must still be pending on the next turn."""
    _finalize_turn_with_metadata(engine, {"contract_proposal": _DRAFT_JSON})
    reloaded = _reload(engine)
    last_assistant = next(m for m in reversed(reloaded.messages) if m.role == "assistant")
    assert last_assistant.metadata.get("contract_proposal") == _DRAFT_JSON, (
        "the pending contract_proposal was lost across the turn boundary — the next user "
        "reply ('once at 9 30') will run as a free model turn instead of the amendment path"
    )


def test_cancel_and_reschedule_rail_metadata_survive_reload(engine: Engine) -> None:
    """The same seam carries the A5 cancel-confirm and A8 reschedule-confirm rails."""
    _finalize_turn_with_metadata(
        engine,
        {"cancel_proposal": "task-42", "reschedule_proposal": '{"schedule_id": "sched-1"}'},
    )
    reloaded = _reload(engine)
    last_assistant = next(m for m in reversed(reloaded.messages) if m.role == "assistant")
    assert last_assistant.metadata.get("cancel_proposal") == "task-42"
    assert last_assistant.metadata.get("reschedule_proposal") == '{"schedule_id": "sched-1"}'


def test_metadata_free_turn_round_trips_unchanged(engine: Engine) -> None:
    """The common path stays byte-identical: no metadata in, empty metadata out."""
    _finalize_turn_with_metadata(engine, {})
    reloaded = _reload(engine)
    last_assistant = next(m for m in reversed(reloaded.messages) if m.role == "assistant")
    assert last_assistant.metadata == {}
