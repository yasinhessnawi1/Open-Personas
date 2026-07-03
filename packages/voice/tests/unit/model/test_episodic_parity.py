"""V13-T7 — episodic parity: voice contributes + consumes episodic like chat (Spec V13).

Phase 1 established that voice already writes episodic chunks and reads them back via
the SHARED ``retrieve_context`` (D-V5-6 — never reimplemented). This verifies the
parity the spec's criterion 6 requires, guarding against drift by driving the REAL
writers/reader:

1. **Contribution parity** — the voice ``_write_episodic`` and the chat
   ``ConversationLoop._write_episodic`` produce the SAME chunk shape (text format,
   importance, source, chunk-id scheme), differing ONLY by voice's ``modality: voice``
   marker (the episodic-layer channel provenance, parallel to the graph-layer
   ``NodeProvenance.channel`` V13 adds).
2. **Consumption parity** — the shared ``retrieve_context`` surfaces a voice-written
   episodic chunk. The voice-profile 0.72 floor (D-3) is GRAPH-only; episodic recall
   has no voice delta (same ``top_k``, same function).
3. **Cross-channel both ways** — a voice-era episode surfaces in a chat-style recall
   and a chat-era episode in a voice-style recall (unified persona-scoped store).
4. **Layer separation** — the episodic write and the synthesis batch write DISTINCT
   layers: one voice turn writes exactly one episodic chunk and no graph/job row; the
   synthesis enqueue writes a job and no episodic chunk. No double-write.
"""

# ruff: noqa: ARG002 — the store double ignores protocol args (persona_id/query) by design.
from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, make_chunk_id
from persona_runtime.loop import ConversationLoop
from persona_runtime.retrieval import retrieve_context
from persona_voice.model.memory import VoiceTurnRecorder

_NOW = datetime(2026, 7, 3, 12, 0, tzinfo=UTC)
_PERSONA = "p1"


class _WriteStore:
    """Records writes + serves query/recent/get_all from what was written."""

    def __init__(self, seed: list[PersonaChunk] | None = None) -> None:
        self.chunks: list[PersonaChunk] = list(seed or [])

    # write side
    def write(self, persona_id: str, chunks: list[PersonaChunk], **_: object) -> None:
        self.chunks.extend(chunks)

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:
        return list(self.chunks)

    # read side (retrieve_context)
    def query(self, persona_id: str, user_message: str, top_k: int) -> list[PersonaChunk]:
        return self.chunks[:top_k]

    def recent(self, persona_id: str, limit: int) -> list[PersonaChunk]:
        return self.chunks[-limit:]


def _write_chat_episodic(store: _WriteStore, user: str, assistant: str) -> None:
    # The chat writer only touches ``self._stores["episodic"]`` — drive it unbound.
    ConversationLoop._write_episodic(
        SimpleNamespace(_stores={"episodic": store}), _PERSONA, user, assistant
    )  # type: ignore[arg-type] # noqa: E501, SLF001


def _write_voice_episodic(store: _WriteStore, user: str, heard: str) -> None:
    ctx = SimpleNamespace(persona_id=_PERSONA, stores={"episodic": store})
    recorder = SimpleNamespace(_ctx=ctx, _clock=lambda: _NOW)
    VoiceTurnRecorder._write_episodic(recorder, user, heard)  # type: ignore[arg-type]


def _episodic_chunk(text: str, *, idx: int, modality: str | None = None) -> PersonaChunk:
    meta = {"importance": "0.5"}
    if modality is not None:
        meta["modality"] = modality
    cid = make_chunk_id(_PERSONA, "episodic", idx)
    return PersonaChunk(
        id=cid,
        text=text,
        metadata=meta,
        created_at=_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM, logical_id=cid, version=1, written_at=_NOW, written_by="t"
        ),
    )


def _empty_stores(episodic: _WriteStore) -> dict[str, _WriteStore]:
    return {
        "identity": _WriteStore(),
        "self_facts": _WriteStore(),
        "worldview": _WriteStore(),
        "episodic": episodic,
    }


def test_contribution_parity_voice_matches_chat_modulo_the_modality_marker() -> None:
    chat_store, voice_store = _WriteStore(), _WriteStore()
    _write_chat_episodic(chat_store, "I moved to Oslo", "Congrats on the move.")
    _write_voice_episodic(voice_store, "I moved to Oslo", "Congrats on the move.")
    chat_chunk, voice_chunk = chat_store.chunks[0], voice_store.chunks[0]

    # Same combined-turn text format ...
    assert (
        chat_chunk.text
        == voice_chunk.text
        == "USER: I moved to Oslo\nASSISTANT: Congrats on the move."
    )
    # ... same importance, same write source, same chunk-id scheme ...
    assert chat_chunk.metadata["importance"] == voice_chunk.metadata["importance"] == "0.5"
    assert chat_chunk.provenance.source == voice_chunk.provenance.source == WriteSource.SYSTEM
    assert chat_chunk.id == voice_chunk.id  # make_chunk_id(persona, "episodic", 0)
    # ... differing ONLY by voice's episodic-layer channel marker.
    assert voice_chunk.metadata.get("modality") == "voice"
    assert "modality" not in chat_chunk.metadata


def test_consumption_parity_shared_retrieve_context_surfaces_a_voice_chunk() -> None:
    voice_chunk = _episodic_chunk("USER: I love hiking\nASSISTANT: Nice.", idx=0, modality="voice")
    ctx = retrieve_context(_empty_stores(_WriteStore([voice_chunk])), _PERSONA, "hiking")
    assert voice_chunk in ctx.episodic  # the same function chat uses, no voice fork


def test_cross_channel_episodic_recall_surfaces_both_directions() -> None:
    voice_chunk = _episodic_chunk("USER: said on a call\nASSISTANT: ok", idx=0, modality="voice")
    chat_chunk = _episodic_chunk("USER: typed in chat\nASSISTANT: ok", idx=1)
    episodic = _WriteStore([voice_chunk, chat_chunk])
    # One shared store, one shared reader: a voice-era episode is recalled alongside a
    # chat-era one regardless of which channel reads (retrieve_context is channel-blind).
    recalled = retrieve_context(_empty_stores(episodic), _PERSONA, "anything", top_k=5).episodic
    assert voice_chunk in recalled  # voice-era episode readable in chat-style recall
    assert chat_chunk in recalled  # chat-era episode readable in voice-style recall


def test_layer_separation_one_turn_one_episodic_chunk_no_graph_write() -> None:
    # The episodic path writes exactly one combined chunk per turn to the episodic
    # store — and touches no other store (the recorder holds no graph store; synthesis
    # is the separate A0 batch that writes graph_nodes, proven in the T5/T6 integration
    # tests). So the episodic layer and the synthesis layer never double-write.
    store = _WriteStore()
    _write_voice_episodic(store, "turn one user", "turn one heard")
    _write_voice_episodic(store, "turn two user", "turn two heard")
    assert len(store.chunks) == 2  # exactly one per turn, none duplicated
    assert all(c.metadata.get("modality") == "voice" for c in store.chunks)
    assert all(c.provenance.source == WriteSource.SYSTEM for c in store.chunks)
