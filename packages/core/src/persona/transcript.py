"""Core-owned minimal view of the ``messages`` table (Spec V9, V9-D-1/D-2).

The per-call **transcript** is built from real ``messages`` rows — the voice
runtime persists each committed spoken turn (user STT + persona TTS-heard text)
to the conversation-scoped ``messages`` table so a finished call is a durable,
re-readable trace that renders under the SAME thread/recap UI as a text chat
(D-V7-7's ``/chat/{id}``). Before V9 a voice turn reached only ``memory_chunks``
(persona-scoped, no ``conversation_id`` — a transcript can't be grouped from it,
V9-D-1) and the in-memory history (lost at teardown); it never reached
``messages``.

The voice runtime is **API-free** (``voice → runtime → core``, never
persona-api), so — exactly like ``memory_chunks`` and ``calls`` — this module
defines core's OWN minimal :class:`~sqlalchemy.Table` view of ``messages``: the
voice ``VoiceTranscriptWriter`` writes through it on the session RLS engine, with
no persona-api import. Column names MIRROR ``persona_api.db.models.messages``
(api owns the DDL); a contract test guards the drift, and the voice write
populates exactly the byte-for-byte subset a finalized chat row carries (V9-D-2).
"""

from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, MetaData, Table, Text, func, text
from sqlalchemy.dialects.postgresql import JSONB

__all__ = ["messages"]

# A private MetaData so importing this module never collides with the api schema
# registry (mirrors ``persona.stores.postgres._md`` / ``persona.calls._md``).
_md = MetaData()

#: Core's minimal view of the api ``messages`` table. Column NAMES mirror
#: ``persona_api.db.models.messages`` (the contract test guards drift). The voice
#: writer is cloud-only (Postgres), so JSONB is safe; the view is never
#: ``create_all``-ed against SQLite.
messages = Table(
    "messages",
    _md,
    Column("id", Text, primary_key=True),
    Column("conversation_id", Text, nullable=False),
    Column("role", Text, nullable=False),
    Column("content", Text, nullable=False),
    Column("tool_calls", JSONB),
    Column("channel", JSONB),
    Column("images", JSONB),
    Column("tier_used", Text),
    # NOT NULL DEFAULT false on the api side; a solicited voice turn reads false.
    Column("originated", Boolean, nullable=False, server_default=text("false")),
    Column("streaming_status", Text),
    Column("stream_events", JSONB),
    # R9-025 leg C / migration 047: the regenerate/edit supersede marker. The
    # voice writer never sets this (a fresh voice turn is always "in force" —
    # NULL); mirrored here purely so the contract test stays green (the voice
    # transcript has no regenerate/edit surface of its own).
    Column("superseded_at", DateTime(timezone=True)),
    # The DB fills this server-side (mirrors the api ``func.now()``); the writer
    # sets it explicitly per turn for the deterministic user-before-assistant order.
    Column("created_at", DateTime(timezone=True), nullable=False, server_default=func.now()),
)
