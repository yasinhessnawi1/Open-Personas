"""Conversation-delete forget — stamp + content-match cascade (Spec K11, T3, D-K11-9).

A conversation delete has always torn down its ``messages``/``turn_logs`` rows, but the
persona's EPISODIC memory of that conversation — the layer that actually drives recall
— survived untouched: deleting a chat about "my dog Balto" left the persona still able
to recall Balto in a future turn. This is the cascade fix, opt-in via
``?forget_memory=true`` (default ``False`` — a plain delete is unchanged behaviour).

Two independent matches feed the cascade (unioned):

- **Exact** — every live writer (``ConversationLoop._write_episodic``, the voice
  recorder, the origination recorder) now stamps ``metadata["conversation_id"]`` at
  write time (T3), so a stamped chunk is found by a plain metadata filter.
- **Content-match** — a LEGACY chunk written before the stamp existed carries no
  ``conversation_id``; the fallback is the SAME semantic matcher T1's
  ``forget_preview`` uses (``memory_service.match_episodic_by_text``, factored out for
  reuse), run over the deleted conversation's own message texts.

Each mechanism is tested in isolation (a message text that only the exact-match/stamp
would catch; a legacy chunk that only content-match would catch), plus the opt-out
default and the gist-cascade + real-recall proof (K8-D-14's cascade, driven end-to-end,
not hand-forced — the A4 lesson).
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

import pytest
from fastapi.testclient import TestClient
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.stores.episodic import EpisodicStore
from persona.stores.pyramid import EpisodicPyramid
from persona_api.app import create_app
from persona_api.auth import AuthenticatedUser
from persona_api.config import APIConfig
from persona_api.middleware.rls_context import current_user_id
from sqlalchemy import text

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from sqlalchemy import Engine
    from tests.conftest import HashEmbedder384

pytestmark = pytest.mark.integration

_OWNER = "u_conv_forget"
_PERSONA = "p_conv_forget"
_NOW = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)

# Byte-identical text pairs are load-bearing under the deterministic HashEmbedder384
# (SHA-256 whole-text hash, zero semantic structure — two distinct texts cosine ≈ 0,
# mirrors test_forget_crosslayer.py's rationale): the content-match assertions below
# hinge on exact-string self-similarity, not paraphrase quality.
_BALTO_TEXT = "USER: I have a dog named Balto\nASSISTANT: nice, tell me more"
_UNRELATED_TEXT = "what is the weather like today"
_LEGACY_TEXT = "USER: legacy Balto memory\nASSISTANT: ok noted"
# Distinct from BOTH texts above — used where a chunk must be untouched by content-match
# against either of them (proves the cascade is scoped, not a persona-wide sweep).
_HIKING_TEXT = "USER: I love hiking in the mountains\nASSISTANT: sounds fun"


def _auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {_OWNER}"}


@pytest.fixture
def app_client(
    migrated_engine: Engine, embedder: HashEmbedder384, tmp_path: Path
) -> Iterator[TestClient]:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set; skipping K11 conversation-forget test")
    with migrated_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO users (id, email) VALUES (:o, 'conv-forget@example.com')"),
            {"o": _OWNER},
        )
        conn.execute(
            text(
                "INSERT INTO personas (id, owner_id, yaml) "
                "VALUES (:p, :o, 'schema_version: \"1.0\"')"
            ),
            {"p": _PERSONA, "o": _OWNER},
        )

    cfg = APIConfig(app_database_url=app_url, audit_root=str(tmp_path / "audit"))
    app = create_app(cfg)

    async def _fake_verify(token: str) -> AuthenticatedUser:
        return AuthenticatedUser(id=token, email=None)  # token == user_id, the test convention

    with TestClient(app) as client:
        app.state.verify_token = _fake_verify
        assert app.state.memory_backend is not None, "memory_backend must compose on Postgres"
        # A deterministic, fast embedder (mirrors the sibling K11 test files' seam-swap).
        app.state.memory_backend._embedder = embedder  # noqa: SLF001 — test seam
        yield client
    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM users WHERE id = :o"), {"o": _OWNER})


def _create_conversation(client: TestClient) -> str:
    resp = client.post(
        f"/v1/personas/{_PERSONA}/conversations", json={"title": "t"}, headers=_auth()
    )
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


def _insert_message(engine: Engine, *, conversation_id: str, role: str, content: str) -> None:
    """A real message row on the real conversation (not synthesised at delete time) —
    the exact shape :func:`chat_service._conversation_message_texts` reads."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO messages (conversation_id, role, content) "
                "VALUES (:cid, :role, :content)"
            ),
            {"cid": conversation_id, "role": role, "content": content},
        )


def _chunk(*, hours_ago: float, body: str, conversation_id: str | None) -> PersonaChunk:
    cid = mint_chunk_id(_PERSONA, "episodic")
    created = _NOW - timedelta(hours=hours_ago)
    metadata = {"importance": "0.5"}
    if conversation_id is not None:
        metadata["conversation_id"] = conversation_id
    return PersonaChunk(
        id=cid,
        text=body,
        metadata=metadata,
        created_at=created,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM,
            logical_id=cid,
            version=1,
            written_at=created,
            written_by=_OWNER,
        ),
    )


def _write_chunk(
    client: TestClient, *, hours_ago: float, body: str, conversation_id: str | None
) -> str:
    """A real episodic write on the real RLS-scoped backend (not an admin bypass) —
    ``conversation_id=None`` simulates a LEGACY (pre-T3-stamp) chunk."""
    token = current_user_id.set(_OWNER)
    try:
        chunk = _chunk(hours_ago=hours_ago, body=body, conversation_id=conversation_id)
        client.app.state.memory_backend.upsert(
            persona_id=_PERSONA, store_kind="episodic", chunks=[chunk]
        )
    finally:
        current_user_id.reset(token)
    return chunk.id


def _raw_chunk_count(client: TestClient) -> int:
    token = current_user_id.set(_OWNER)
    try:
        chunks = client.app.state.memory_backend.get_all(persona_id=_PERSONA, store_kind="episodic")
    finally:
        current_user_id.reset(token)
    return len(chunks)


def _recall_returns(client: TestClient, query: str, *, contains: str) -> bool:
    """REAL recall — the exact ``EpisodicStore.query`` the chat/voice loop calls."""
    store = EpisodicStore(
        backend=client.app.state.memory_backend, audit_logger=client.app.state.audit_logger
    )
    token = current_user_id.set(_OWNER)
    try:
        hits = store.query(_PERSONA, query, 5)
    finally:
        current_user_id.reset(token)
    return any(contains in h.text for h in hits)


def test_delete_conversation_default_leaves_episodic_intact(
    app_client: TestClient, migrated_engine: Engine
) -> None:
    """Opt-out (D-K11-9): the default ``forget_memory=false`` is byte-for-byte the
    prior behaviour — the conversation is gone, its episodic memory is not."""
    conv_id = _create_conversation(app_client)
    _insert_message(migrated_engine, conversation_id=conv_id, role="user", content=_BALTO_TEXT)
    _write_chunk(app_client, hours_ago=1.0, body=_BALTO_TEXT, conversation_id=conv_id)

    resp = app_client.delete(f"/v1/conversations/{conv_id}", headers=_auth())
    assert resp.status_code == 204

    # The conversation itself is gone (the pre-existing behaviour)...
    get_after = app_client.get(f"/v1/conversations/{conv_id}", headers=_auth())
    assert get_after.status_code == 404
    # ...but its episodic memory is untouched.
    assert _raw_chunk_count(app_client) == 1
    assert _recall_returns(app_client, "dog", contains="Balto")


def test_delete_conversation_forget_memory_removes_the_exact_stamped_chunk(
    app_client: TestClient, migrated_engine: Engine
) -> None:
    """Isolates the EXACT metadata match (T3): the conversation's own message text
    (``_UNRELATED_TEXT``) shares no content with the stamped chunk, so only the
    ``metadata["conversation_id"]`` stamp — not content-match — can find it."""
    conv_id = _create_conversation(app_client)
    _insert_message(migrated_engine, conversation_id=conv_id, role="user", content=_UNRELATED_TEXT)
    _write_chunk(app_client, hours_ago=1.0, body=_BALTO_TEXT, conversation_id=conv_id)
    # A second, unrelated conversation's stamped chunk must survive — the cascade is
    # scoped to the DELETED conversation's id, not a persona-wide wipe. Its text is
    # distinct from BOTH _BALTO_TEXT and _UNRELATED_TEXT so content-match (run over
    # conv_id's own message text) cannot accidentally sweep it too.
    other_conv_id = _create_conversation(app_client)
    _write_chunk(app_client, hours_ago=1.0, body=_HIKING_TEXT, conversation_id=other_conv_id)

    resp = app_client.delete(
        f"/v1/conversations/{conv_id}", params={"forget_memory": "true"}, headers=_auth()
    )
    assert resp.status_code == 204

    assert not _recall_returns(app_client, "dog", contains="Balto")
    assert _raw_chunk_count(app_client) == 1  # only the other conversation's chunk remains
    assert _recall_returns(app_client, "hiking", contains="hiking")


def test_delete_conversation_forget_memory_content_match_spares_other_conversations_stamp(
    app_client: TestClient, migrated_engine: Engine
) -> None:
    """K11 whole-branch review fix: content-match must NOT sweep in a chunk that is
    STAMPED with a *different* conversation's id, even when its text is a near-exact
    match to the deleted conversation's messages. Only the deleted conversation's own
    stamped chunks (exact-match) and genuinely LEGACY (unstamped) chunks may be caught
    by content-match — a stamped chunk for conversation B is B's, full stop."""
    conv_id = _create_conversation(app_client)
    _insert_message(migrated_engine, conversation_id=conv_id, role="user", content=_BALTO_TEXT)
    _write_chunk(app_client, hours_ago=1.0, body=_BALTO_TEXT, conversation_id=conv_id)

    other_conv_id = _create_conversation(app_client)
    # B's chunk is STAMPED with B's own id, but its text is byte-identical to A's
    # message — under the old (unfiltered) content-match, this would be swept into
    # A's delete despite carrying a live stamp for a different conversation.
    _write_chunk(app_client, hours_ago=1.0, body=_BALTO_TEXT, conversation_id=other_conv_id)
    # A genuinely legacy (unstamped) chunk with the SAME similar text must still be
    # caught by content-match — proving the fix narrows scope without disabling the
    # legacy fallback entirely.
    _write_chunk(app_client, hours_ago=2.0, body=_BALTO_TEXT, conversation_id=None)

    resp = app_client.delete(
        f"/v1/conversations/{conv_id}", params={"forget_memory": "true"}, headers=_auth()
    )
    assert resp.status_code == 204

    # A's own stamped chunk + the legacy chunk are gone; only B's stamped chunk remains.
    assert _raw_chunk_count(app_client) == 1
    token = current_user_id.set(_OWNER)
    try:
        remaining = app_client.app.state.memory_backend.get_all(
            persona_id=_PERSONA, store_kind="episodic"
        )
    finally:
        current_user_id.reset(token)
    assert remaining[0].metadata.get("conversation_id") == other_conv_id


def test_delete_conversation_forget_memory_removes_a_legacy_chunk_by_content_match(
    app_client: TestClient, migrated_engine: Engine
) -> None:
    """Isolates the CONTENT-MATCH fallback (T3): the chunk carries no
    ``conversation_id`` stamp (a pre-T3 write) — only ``match_episodic_by_text``
    (T1's matcher, reused) run over the conversation's own message text finds it."""
    conv_id = _create_conversation(app_client)
    _insert_message(migrated_engine, conversation_id=conv_id, role="user", content=_LEGACY_TEXT)
    _write_chunk(app_client, hours_ago=1.0, body=_LEGACY_TEXT, conversation_id=None)
    # A negative control: an unrelated legacy chunk (different content, different —
    # nonexistent — conversation) must survive; the floor discriminates.
    _write_chunk(app_client, hours_ago=5.0, body=_UNRELATED_TEXT, conversation_id=None)

    resp = app_client.delete(
        f"/v1/conversations/{conv_id}", params={"forget_memory": "true"}, headers=_auth()
    )
    assert resp.status_code == 204

    assert not _recall_returns(app_client, "legacy", contains="Balto")
    assert _raw_chunk_count(app_client) == 1  # the unrelated legacy chunk survives
    assert _recall_returns(app_client, "weather", contains="weather")


def test_delete_conversation_forget_memory_cascades_gists_and_recall(
    app_client: TestClient, migrated_engine: Engine
) -> None:
    """(d) the raw-chunk cascade reaches its covering gist too (K8-D-14) — driven
    through the REAL delete route, not a hand-forced gist removal (the A4 lesson)."""
    conv_id = _create_conversation(app_client)
    _insert_message(migrated_engine, conversation_id=conv_id, role="user", content=_BALTO_TEXT)
    chunk_id = _write_chunk(app_client, hours_ago=1.0, body=_BALTO_TEXT, conversation_id=conv_id)
    token = current_user_id.set(_OWNER)
    try:
        pyramid = EpisodicPyramid(
            backend=app_client.app.state.memory_backend,
            audit_logger=app_client.app.state.audit_logger,
        )
        gist = pyramid.write_gist(
            _PERSONA, text=_BALTO_TEXT, member_ids=[chunk_id], created_at=_NOW
        )
    finally:
        current_user_id.reset(token)

    # Sanity: both layers are live, and recall surfaces the fact, before the delete.
    assert _recall_returns(app_client, "dog", contains="Balto")
    win_before = app_client.get(
        "/v1/memory/episodic", params={"persona_id": _PERSONA}, headers=_auth()
    ).json()
    assert any(g["id"] == gist.id for g in win_before["gists"])

    resp = app_client.delete(
        f"/v1/conversations/{conv_id}", params={"forget_memory": "true"}, headers=_auth()
    )
    assert resp.status_code == 204

    assert _raw_chunk_count(app_client) == 0
    win_after = app_client.get(
        "/v1/memory/episodic", params={"persona_id": _PERSONA}, headers=_auth()
    ).json()
    assert win_after["gists"] == []  # the covering gist was cascaded away too
    assert not _recall_returns(app_client, "dog", contains="Balto")


def test_delete_conversation_forget_memory_is_a_noop_with_nothing_to_forget(
    app_client: TestClient,
) -> None:
    """No messages, no episodic evidence — ``forget_memory=true`` still just deletes
    the (now-empty) conversation cleanly, no error."""
    conv_id = _create_conversation(app_client)
    resp = app_client.delete(
        f"/v1/conversations/{conv_id}", params={"forget_memory": "true"}, headers=_auth()
    )
    assert resp.status_code == 204
    assert _raw_chunk_count(app_client) == 0
