"""Spec K8 T6 — the sleep-time engine (K8-D-8/9; acceptance 3/6/7 unit halves).

Pins the T6 bars at the unit level (the real-merge idempotency leg lives in the
Postgres integration suite):

2. Clustering is pure + deterministic: temporal windows, the open trailing
   window deferred, topic splits via the injected embedder, size caps.
3. Idempotency by construction: a re-run over the same store converges to
   ZERO new gists and ZERO new candidates (existing gist ids skip windows).
4. Gists are built ONLY through the T5 assembly seam — the summarizer spy
   receives byte-identical ``assemble_summarizer_input`` output.
5. Fail-soft: a failing summarizer/merge isolates per window with an honest
   skip reason; raw chunks stay untouched; the run never raises.
6. One engine, two outputs, distinct layers: gist-kind upserts + merge-port
   candidates, ZERO episodic-kind writes (layer separation holds).
"""

# ruff: noqa: ARG002 — spy doubles ignore protocol args by design.
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from persona.audit import MemoryAuditLogger
from persona.errors import SummarizerError
from persona.graph.models import NodeKind
from persona.graph.protocol import UpdateIntent
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.stores.engine import EpisodicConsolidationEngine
from persona.stores.lifecycle import EpisodicSettings
from persona.stores.pyramid import EpisodicPyramid
from persona.stores.summarizer import StubSummarizer, assemble_summarizer_input

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)
_SETTINGS = EpisodicSettings(
    min_chunks_per_run=2,
    cluster_gap_minutes=45.0,
    cluster_split_threshold=0.70,
    max_cluster_chunks=40,
    gist_target_tokens=64,
    engine_max_batch=500,
)


class _SpyBackend:
    """In-memory Backend recording every (method, store_kind) touch."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, PersonaChunk]] = {}
        self.touches: list[tuple[str, str]] = []

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        self.touches.append(("upsert", store_kind))
        bucket = self.rows.setdefault((persona_id, store_kind), {})
        for c in chunks:
            bucket[c.id] = c

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        self.touches.append(("get_all", store_kind))
        return list(self.rows.get((persona_id, store_kind), {}).values())

    def recent(self, *, persona_id: str, store_kind: str, limit: int) -> list[PersonaChunk]:
        self.touches.append(("recent", store_kind))
        current = list(self.rows.get((persona_id, store_kind), {}).values())
        current.sort(key=lambda c: (c.created_at, c.id), reverse=True)
        return current[:limit]

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        wanted = set(logical_ids)
        return [
            c
            for c in self.rows.get((persona_id, store_kind), {}).values()
            if c.provenance is not None and c.provenance.logical_id in wanted
        ]

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        self.touches.append(("delete_documents", store_kind))
        bucket = self.rows.get((persona_id, store_kind), {})
        for cid in ids:
            bucket.pop(cid, None)

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.touches.append(("delete_persona", store_kind))
        self.rows.pop((persona_id, store_kind), None)

    def query(self, **_: object) -> list[PersonaChunk]:
        return []

    def count(self, **_: object) -> int:
        return 0

    def reinforce(self, **_: object) -> None:
        return

    def set_bands(self, *, persona_id: str, store_kind: str, bands: dict[str, int]) -> None:
        self.touches.append(("set_bands", store_kind))
        bucket = self.rows.get((persona_id, store_kind), {})
        for cid, band in bands.items():
            if cid in bucket:
                bucket[cid] = bucket[cid].model_copy(update={"band": band})

    def band_histogram(self, *, persona_id: str, store_kind: str) -> dict[int, int]:
        histogram: dict[int, int] = {}
        for c in self.rows.get((persona_id, store_kind), {}).values():
            histogram[c.band] = histogram.get(c.band, 0) + 1
        return histogram

    def seed_raw(self, persona_id: str, chunk: PersonaChunk) -> None:
        self.rows.setdefault((persona_id, "episodic"), {})[chunk.id] = chunk


class _RecordingMerge:
    def __init__(self, *, explode: bool = False) -> None:
        self.candidates: list[tuple[str, Any]] = []
        self._explode = explode

    def merge(self, owner_id: str, candidate: Any) -> Any:  # noqa: ANN401
        if self._explode:
            msg = "graph down"
            raise RuntimeError(msg)
        self.candidates.append((owner_id, candidate))
        return object()


class _RecordingSummarizer:
    """Wraps the stub, recording exactly what content it was fed (bar 4)."""

    def __init__(self, *, fail_on: str | None = None) -> None:
        self._stub = StubSummarizer()
        self.contents: list[str] = []
        self._fail_on = fail_on

    async def summarize(self, content: str, *, target_tokens: int) -> str:
        if self._fail_on is not None and self._fail_on in content:
            raise SummarizerError("injected failure", context={})
        self.contents.append(content)
        return await self._stub.summarize(content, target_tokens=target_tokens)


def _chunk(*, hours_ago: float, text: str | None = None) -> PersonaChunk:
    cid = mint_chunk_id("p1", "episodic")
    created = _NOW - timedelta(hours=hours_ago)
    return PersonaChunk(
        id=cid,
        text=text or f"USER: at {hours_ago}h\nASSISTANT: noted",
        created_at=created,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM,
            logical_id=cid,
            version=1,
            written_at=created,
            written_by="test",
        ),
    )


def _engine(
    backend: _SpyBackend,
    *,
    summarizer: Any = None,  # noqa: ANN401
    merge: _RecordingMerge | None = None,
    embedder: Any = None,  # noqa: ANN401
) -> tuple[EpisodicConsolidationEngine, EpisodicPyramid, _RecordingMerge]:
    pyramid = EpisodicPyramid(backend=backend, audit_logger=MemoryAuditLogger())
    graph = merge or _RecordingMerge()
    engine = EpisodicConsolidationEngine(
        backend=backend,
        pyramid=pyramid,
        summarizer=summarizer or StubSummarizer(),
        graph=graph,
        settings=_SETTINGS,
        embedder=embedder,
    )
    return engine, pyramid, graph


def _seed_two_sessions(backend: _SpyBackend) -> tuple[list[PersonaChunk], list[PersonaChunk]]:
    """Two closed sessions (3h and 2h ago) separated by > the 45m gap."""
    older = [_chunk(hours_ago=3.0), _chunk(hours_ago=2.9), _chunk(hours_ago=2.8)]
    newer = [_chunk(hours_ago=2.0), _chunk(hours_ago=1.9)]
    for c in [*older, *newer]:
        backend.seed_raw("p1", c)
    return older, newer


# --- bar 2: deterministic clustering -------------------------------------------


def test_temporal_windows_split_on_the_gap_and_defer_the_open_tail() -> None:
    backend = _SpyBackend()
    older, newer = _seed_two_sessions(backend)
    live = _chunk(hours_ago=0.2)  # within 45m of now — the session may continue
    backend.seed_raw("p1", live)
    engine, pyramid, graph = _engine(backend)

    report = asyncio.run(engine.run("u1", "p1", now=_NOW))
    assert report.windows_formed == 2  # noqa: PLR2004 — the two CLOSED sessions
    assert report.windows_deferred_open == 1  # the live tail is never summarised
    assert report.gists_written == 2  # noqa: PLR2004
    gist_members = {g.member_ids for g in pyramid.gists("p1")}
    assert tuple(c.id for c in older) in gist_members
    assert tuple(c.id for c in newer) in gist_members
    assert not any(live.id in m for m in gist_members)


def test_below_min_batch_the_run_is_a_reported_noop() -> None:
    backend = _SpyBackend()
    backend.seed_raw("p1", _chunk(hours_ago=3.0))
    engine, pyramid, _ = _engine(backend)
    report = asyncio.run(engine.run("u1", "p1", now=_NOW))
    assert report.chunks_considered == 1
    assert report.gists_written == 0
    assert pyramid.gists("p1") == []


def test_topic_split_fires_on_an_embedding_dip() -> None:
    class _TwoTopicEmbedder:
        dimension = 2

        def encode(self, texts: list[str]) -> list[list[float]]:
            # First topic → x-axis, second topic → y-axis (orthogonal ⇒ sim 0).
            return [[0.0, 1.0] if "TOPIC-B" in t else [1.0, 0.0] for t in texts]

    backend = _SpyBackend()
    chunks = [
        _chunk(hours_ago=3.0, text="TOPIC-A one"),
        _chunk(hours_ago=2.95, text="TOPIC-A two"),
        _chunk(hours_ago=2.9, text="TOPIC-B one"),
        _chunk(hours_ago=2.85, text="TOPIC-B two"),
    ]
    for c in chunks:
        backend.seed_raw("p1", c)
    engine, pyramid, _ = _engine(backend, embedder=_TwoTopicEmbedder())

    report = asyncio.run(engine.run("u1", "p1", now=_NOW))
    assert report.windows_formed == 2  # noqa: PLR2004 — one temporal window, topic-split in two
    members = sorted(len(g.member_ids) for g in pyramid.gists("p1"))
    assert members == [2, 2]


# --- bar 3 (unit half): idempotent re-run --------------------------------------


def test_second_run_converges_to_zero_new_writes() -> None:
    backend = _SpyBackend()
    _seed_two_sessions(backend)
    engine, pyramid, graph = _engine(backend)

    first = asyncio.run(engine.run("u1", "p1", now=_NOW))
    gists_after_first = {g.id for g in pyramid.gists("p1")}
    second = asyncio.run(engine.run("u1", "p1", now=_NOW))

    assert first.gists_written == 2  # noqa: PLR2004
    assert second.gists_written == 0
    assert second.candidates_emitted == 0  # no duplicate candidates emitted at all
    assert {g.id for g in pyramid.gists("p1")} == gists_after_first
    assert len(graph.candidates) == 2  # noqa: PLR2004 — first run only


# --- bar 4: the T5 assembly seam is the ONLY input path -------------------------


def test_summarizer_receives_exactly_the_assembly_output() -> None:
    backend = _SpyBackend()
    older, newer = _seed_two_sessions(backend)
    spy = _RecordingSummarizer()
    engine, _, _ = _engine(backend, summarizer=spy)

    asyncio.run(engine.run("u1", "p1", now=_NOW))
    expected = {assemble_summarizer_input(older), assemble_summarizer_input(newer)}
    assert set(spy.contents) == expected  # byte-identical — the mechanism, not a claim


# --- bar 5: fail-soft, per-window isolation, honest report -----------------------


def test_a_failing_window_is_skipped_with_reason_and_the_run_continues() -> None:
    backend = _SpyBackend()
    older, newer = _seed_two_sessions(backend)
    spy = _RecordingSummarizer(fail_on=older[0].text)  # first session's window fails
    engine, pyramid, graph = _engine(backend, summarizer=spy)

    report = asyncio.run(engine.run("u1", "p1", now=_NOW))
    assert report.gists_written == 1  # the healthy window landed
    assert len(report.skipped) == 1
    assert "summarize" in report.skipped[0].reason  # honest, never silent
    raw_rows = backend.rows[("p1", "episodic")]
    assert len(raw_rows) == 5  # noqa: PLR2004 — raw chunks untouched (the §0 floor)


def test_a_merge_failure_keeps_the_gist_and_reports_the_candidate_skip() -> None:
    backend = _SpyBackend()
    _seed_two_sessions(backend)
    engine, pyramid, _ = _engine(backend, merge=_RecordingMerge(explode=True))

    report = asyncio.run(engine.run("u1", "p1", now=_NOW))
    assert report.gists_written == 2  # noqa: PLR2004 — the episodic layer landed
    assert report.candidates_emitted == 0
    assert len(report.skipped) == 2  # noqa: PLR2004
    assert all("merge" in s.reason for s in report.skipped)


# --- bar 6: one engine, two outputs, distinct layers ------------------------------


def test_layer_separation_gists_and_candidates_never_double_write() -> None:
    backend = _SpyBackend()
    _seed_two_sessions(backend)
    engine, pyramid, graph = _engine(backend)

    asyncio.run(engine.run("u1", "p1", now=_NOW))
    upserts = [kind for method, kind in backend.touches if method == "upsert"]
    assert all(kind == "episodic_gist" for kind in upserts)  # NEVER the raw kind
    assert len(upserts) == 2  # noqa: PLR2004 — one gist per window, no extras
    assert len(graph.candidates) == 2  # noqa: PLR2004 — one candidate per window


def test_candidate_shape_is_the_minimal_k8_d9_contract() -> None:
    backend = _SpyBackend()
    older, _ = _seed_two_sessions(backend)
    engine, _, graph = _engine(backend)
    asyncio.run(engine.run("u1", "p1", now=_NOW))

    owner, candidate = graph.candidates[0]
    assert owner == "u1"
    assert candidate.node_kind == NodeKind.CONCEPT
    assert candidate.update_intent == UpdateIntent.NONE
    assert candidate.target_node_id is None
    assert candidate.close_link_ids == ()
    assert candidate.valid_at is not None  # the window end (world event time)
    assert candidate.provenance.source == WriteSource.SYSTEM
    assert candidate.provenance.persona_id == "p1"
    assert candidate.concept_name.startswith("episode ")
