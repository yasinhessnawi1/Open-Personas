"""Typed memory with versioning, history, and rollback — no model key required.

The four stores live behind one ``TypedStore`` surface with per-store write
policies (``user`` writes to self-facts are always accepted; ``persona_self``
writes need ``force`` + high confidence + a reason). Versioned stores keep the
whole chain: updating a fact supersedes it, ``history()`` shows every version,
and ``rollback()`` restores an old one — as a NEW version, never by rewriting
the past.

Uses a throwaway Chroma directory; the embedding model (bge-small-en-v1.5)
downloads once on first run.

Run from ``packages/core/examples/``:

    uv run python 02_typed_memory.py
"""

from __future__ import annotations

import tempfile
from datetime import UTC, datetime
from pathlib import Path

from persona.audit import JSONLAuditLogger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.stores import ChromaBackend, SelfFactsStore, SentenceTransformerEmbedder

PERSONA_ID = "example-memory"

workdir = Path(tempfile.mkdtemp(prefix="persona-example-"))
backend = ChromaBackend(
    persist_path=workdir / "chroma",
    embedder=SentenceTransformerEmbedder(model_name="BAAI/bge-small-en-v1.5"),
)
store = SelfFactsStore(backend=backend, audit_logger=JSONLAuditLogger(workdir / "audit"))


def fact_chunk(text: str, *, logical_id: str | None = None, version: int = 1) -> PersonaChunk:
    now = datetime.now(UTC)
    chunk_id = mint_chunk_id(PERSONA_ID, "self_facts")
    return PersonaChunk(
        id=chunk_id,
        text=text,
        metadata={"confidence": "0.9"},
        created_at=now,
        provenance=ChunkProvenance(
            source=WriteSource.USER,
            logical_id=logical_id or chunk_id,
            version=version,
            written_at=now,
            written_by="examples.02",
        ),
    )


# 1. write a fact as the USER (the owner can always edit self-facts)
first = fact_chunk("Prefers espresso; drinks tea only when it rains.")
store.write(PERSONA_ID, [first], source=WriteSource.USER, written_by="examples.02")
logical_id = first.provenance.logical_id if first.provenance else first.id
print(f"v1 written: {first.text!r}")

# 2. semantic query — retrieval is by meaning, not substring
hits = store.query(PERSONA_ID, "what hot drinks does it like?", top_k=1)
print(f"query hit:  {hits[0].text!r}")

# 3. update the same logical fact — a NEW version that supersedes v1
second = fact_chunk(
    "Switched to oat-milk cortados; espresso demoted to deadline fuel.",
    logical_id=logical_id,
    version=2,
)
store.write(PERSONA_ID, [second], source=WriteSource.USER, written_by="examples.02")
print(f"v2 written: {second.text!r}")

# 4. the full chain is kept — summaries and updates never replace evidence
chain = store.history(PERSONA_ID, logical_id)
print(f"\nhistory ({len(chain)} versions):")
for chunk in chain:
    v = chunk.provenance.version if chunk.provenance else "?"
    superseded = bool(chunk.provenance and chunk.provenance.superseded_by)
    print(f"  v{v}{' (superseded)' if superseded else ' (current)':16} {chunk.text!r}")

# 5. one-call rollback — restores v1 as a new head version
store.rollback(
    PERSONA_ID,
    logical_id,
    to_version=1,
    source=WriteSource.USER,
    written_by="examples.02",
    reason="the cortado phase did not last",
)
# superseded versions are filtered AFTER top_k, so query wide enough to
# clear the old versions of the same fact
current = store.query(PERSONA_ID, "hot drinks", top_k=5)
print(f"\nafter rollback, current: {current[0].text!r}")
print(f"audit log: {workdir / 'audit'} (one JSONL event per mutation)")
