"""The sleep-time episodic consolidation engine (Spec K8 T6, K8-D-8/9).

One engine, two outputs, one pass (design §1): per-persona idle-window
clustering over recent raw chunks → (1) gist rows through the T5 summarizer
seam and the T4 pyramid surface, (2) one :class:`KnowledgeCandidate` per
cluster through K7's ``GraphStore.merge`` (K8-D-9: minimal, contract-
conformant — the engine emits, K7 owns what merge does; ConsolidationPass
cadence STAYS with the synthesis tail, gate redirect 5).

Determinism ⇒ idempotency (the K7 star/center discipline): window boundaries
are pure functions of the chunk timestamps (+ optional embedding refinement
over freshly-encoded texts), gist ids are uuid5 over the member set, and a
window whose gist already exists is skipped — so a re-run over the same store
converges to zero new writes. The candidate side rides K7's own idempotency
(content short-circuit; same candidate twice ⇒ one durable mutation).

Watermark is DERIVED, not stored: "new since the last run" = raw chunks not
yet covered by any gist (no marker table, no schema). The newest window is
deferred while still open (its tail within ``cluster_gap_minutes`` of now —
LightMem's boundary discipline: never summarise a conversation mid-flight).

Failure posture (acceptance 7): per-window isolation — a failed summarize or
merge lands in ``skipped`` with its reason (the honest report, never a silent
skip) and the run continues; a failed run leaves recall over raw chunks
untouched (the §0 floor). The engine never runs on the turn path — the api
worker owns scheduling (watermark-bucket coalesced, idle-deferred).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from persona.errors import SummarizerError
from persona.graph.models import NodeKind, NodeProvenance
from persona.graph.protocol import KnowledgeCandidate
from persona.logging import get_logger
from persona.schema.chunks import WriteSource
from persona.stores.lifecycle import classify_band
from persona.stores.pyramid import make_gist_id
from persona.stores.summarizer import assemble_summarizer_input

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.graph.protocol import MergeOutcome
    from persona.schema.chunks import PersonaChunk
    from persona.stores.backend import Backend
    from persona.stores.embedder import Embedder
    from persona.stores.lifecycle import EpisodicSettings
    from persona.stores.pyramid import EpisodicPyramid
    from persona.stores.summarizer import Summarizer

__all__ = [
    "CandidateMergePort",
    "EpisodicConsolidationEngine",
    "EpisodicConsolidationReport",
    "SkippedWindow",
]

_log = get_logger("stores.episodic_engine")

_ENGINE_WRITTEN_BY = "episodic.engine"


@runtime_checkable
class CandidateMergePort(Protocol):
    """The K7 write entrypoint the engine emits through (contract §1)."""

    def merge(self, owner_id: str, candidate: KnowledgeCandidate) -> MergeOutcome: ...


class SkippedWindow(BaseModel):
    """One window the pass could not complete — reported, never silent."""

    model_config = ConfigDict(frozen=True)

    window_start: datetime
    member_count: int
    reason: str


class EpisodicConsolidationReport(BaseModel):
    """What one engine run did (the P8 measurement surface + the honest report)."""

    model_config = ConfigDict(frozen=True)

    persona_id: str
    chunks_considered: int
    windows_formed: int
    windows_deferred_open: int
    gists_written: int
    candidates_emitted: int
    skipped: tuple[SkippedWindow, ...] = ()
    # --- T7 tiering (K8-D-3/7): the band-materialization half of the pass.
    bands_demoted: int = 0
    bands_promoted: int = 0
    full_band_size: int = 0


class EpisodicConsolidationEngine:
    """Cluster → summarize-from-originals → gist + graph candidate (K8-D-8/9).

    Pure DI: the transport, pyramid, summarizer seam, merge port, and settings
    are injected; the engine holds no global state and never touches an event
    loop primitive beyond awaiting its summarizer and off-loading the sync
    merge (``asyncio.to_thread`` — the K7 handler discipline).

    Args:
        backend: The episodic transport (reads recent raw chunks).
        pyramid: The T4 gist surface (writes gists; enforces §0 structurally).
        summarizer: The T5 seam (stub / tier adapter / P7 later).
        graph: K7's merge port — the ONLY graph write path (redirect 5:
            no ConsolidationPass steering).
        settings: The lifecycle knobs (cluster gaps, batch sizes, targets).
        embedder: Optional embedding refinement for topic splits inside a
            temporal window (freshly encodes member texts — no transport
            widening); ``None`` ⇒ temporal-only clustering.
    """

    def __init__(
        self,
        *,
        backend: Backend,
        pyramid: EpisodicPyramid,
        summarizer: Summarizer,
        graph: CandidateMergePort,
        settings: EpisodicSettings,
        embedder: Embedder | None = None,
    ) -> None:
        self._backend = backend
        self._pyramid = pyramid
        self._summarizer = summarizer
        self._graph = graph
        self._settings = settings
        self._embedder = embedder

    async def run(
        self,
        owner_id: str,
        persona_id: str,
        *,
        now: datetime | None = None,
    ) -> EpisodicConsolidationReport:
        """One consolidation pass for ``persona_id`` (owner-scoped writes).

        ``now`` is injectable for tests; the production caller (the A0
        handler) passes nothing and gets wall-clock — window closure is
        genuine idle detection, the one place wall time is the point.
        """
        moment = now or datetime.now(UTC)
        uncovered = self._uncovered_recent(persona_id)
        if len(uncovered) < self._settings.min_chunks_per_run:
            # Too few NEW chunks to gist — but a quiet store still ages, so the
            # tiering half of the pass (T7) runs regardless: demotions/promotions
            # depend on time and reinforcement, not on new material.
            demoted, promoted = self._materialize_bands(persona_id, now=moment)
            histogram = self._backend.band_histogram(persona_id=persona_id, store_kind="episodic")
            return EpisodicConsolidationReport(
                persona_id=persona_id,
                chunks_considered=len(uncovered),
                windows_formed=0,
                windows_deferred_open=0,
                gists_written=0,
                candidates_emitted=0,
                bands_demoted=demoted,
                bands_promoted=promoted,
                full_band_size=histogram.get(0, 0),
            )

        windows, deferred = self._form_windows(uncovered, now=moment)
        gists_written = 0
        candidates_emitted = 0
        skipped: list[SkippedWindow] = []

        existing_gist_ids = {g.id for g in self._pyramid.gists(persona_id)}
        for window in windows:
            member_ids = [c.id for c in window]
            if make_gist_id(persona_id, member_ids) in existing_gist_ids:
                continue  # idempotent re-run: this window already converged
            window_end = max(c.created_at for c in window)
            try:
                # Bar 4 (acceptance 3): the summarizer input is built by the T5
                # assembly — the ONLY path, so from-originals is mechanical.
                content = assemble_summarizer_input(window)
                text = await self._summarizer.summarize(
                    content, target_tokens=self._settings.gist_target_tokens
                )
                self._pyramid.write_gist(
                    persona_id,
                    text=text,
                    member_ids=member_ids,
                    created_at=window_end,
                    written_by=_ENGINE_WRITTEN_BY,
                )
                gists_written += 1
            except SummarizerError as exc:
                skipped.append(
                    SkippedWindow(
                        window_start=window[0].created_at,
                        member_count=len(window),
                        reason=f"summarize: {exc}",
                    )
                )
                continue  # per-window isolation — the run continues

            try:
                candidate = self._candidate_for(persona_id, text, window_end)
                # merge is sync + LLM-free (contract §1); off the loop so a slow
                # DB never stalls the worker's other jobs (the K7 discipline).
                await asyncio.to_thread(self._graph.merge, owner_id, candidate)
                candidates_emitted += 1
            except Exception as exc:  # noqa: BLE001 — isolate; gist already landed
                skipped.append(
                    SkippedWindow(
                        window_start=window[0].created_at,
                        member_count=len(window),
                        reason=f"merge: {type(exc).__name__}",
                    )
                )

        demoted, promoted = self._materialize_bands(persona_id, now=moment)
        histogram = self._backend.band_histogram(persona_id=persona_id, store_kind="episodic")
        report = EpisodicConsolidationReport(
            persona_id=persona_id,
            chunks_considered=len(uncovered),
            windows_formed=len(windows),
            windows_deferred_open=deferred,
            gists_written=gists_written,
            candidates_emitted=candidates_emitted,
            skipped=tuple(skipped),
            bands_demoted=demoted,
            bands_promoted=promoted,
            full_band_size=histogram.get(0, 0),
        )
        _log.info(
            "episodic consolidation ran persona={p} windows={w} gists={g} "
            "candidates={c} skipped={s}",
            p=persona_id,
            w=report.windows_formed,
            g=report.gists_written,
            c=report.candidates_emitted,
            s=len(report.skipped),
        )
        return report

    def _materialize_bands(self, persona_id: str, *, now: datetime) -> tuple[int, int]:
        """The T7 tiering/demote pass — materialize what classify_band computes.

        Over the recent batch (newest first, so the index IS the tail rank):
        a chunk demotes to GIST display only when a covering gist EXISTS
        (K8-D-3: never display nothing — uncovered old chunks stay FULL until
        the engine covers them); a reinforced/pinned demoted chunk promotes
        back to FULL. Band flips are lifecycle-only (one batched ``set_bands``;
        never text/embedding — tier-and-demote, NEVER delete, K8-D-7). Returns
        ``(demoted, promoted)`` for the honest report.
        """
        batch = self._backend.recent(
            persona_id=persona_id,
            store_kind="episodic",
            limit=self._settings.engine_max_batch,
        )
        covered: set[str] = set()
        for gist in self._pyramid.gists(persona_id):
            covered.update(gist.member_ids)

        updates: dict[str, int] = {}
        demoted = promoted = 0
        for tail_rank, chunk in enumerate(batch):
            if chunk.member_ids:
                continue  # a gist row rode in somehow — bands are for raw chunks
            target = classify_band(chunk, now=now, settings=self._settings, tail_rank=tail_rank)
            if target == 1 and chunk.id not in covered:
                target = 0  # no covering gist yet — stays FULL (fail-soft)
            if target == chunk.band:
                continue
            updates[chunk.id] = target
            if target == 1:
                demoted += 1
            else:
                promoted += 1
        if updates:
            self._backend.set_bands(persona_id=persona_id, store_kind="episodic", bands=updates)
        return demoted, promoted

    # ----- internals ----------------------------------------------------------

    def _uncovered_recent(self, persona_id: str) -> list[PersonaChunk]:
        """Recent raw chunks not yet covered by any gist (the derived watermark)."""
        covered: set[str] = set()
        for gist in self._pyramid.gists(persona_id):
            covered.update(gist.member_ids)
        batch = self._backend.recent(
            persona_id=persona_id,
            store_kind="episodic",
            limit=self._settings.engine_max_batch,
        )
        fresh = [c for c in batch if c.id not in covered and not c.member_ids]
        fresh.sort(key=lambda c: (c.created_at, c.id))
        return fresh

    def _form_windows(
        self, chunks: Sequence[PersonaChunk], *, now: datetime
    ) -> tuple[list[list[PersonaChunk]], int]:
        """Deterministic windows: temporal gaps → optional topic split → size cap.

        Returns ``(closed_windows, deferred_open_count)`` — the trailing window
        is deferred while its tail is within ``cluster_gap_minutes`` of ``now``
        (still open; summarising a live session would violate the boundary
        discipline).
        """
        gap = timedelta(minutes=self._settings.cluster_gap_minutes)
        windows: list[list[PersonaChunk]] = []
        current: list[PersonaChunk] = []
        for chunk in chunks:
            if current and (chunk.created_at - current[-1].created_at) > gap:
                windows.append(current)
                current = []
            current.append(chunk)
        if current:
            if (now - current[-1].created_at) > gap:
                windows.append(current)
                deferred = 0
            else:
                deferred = 1
        else:
            deferred = 0

        refined: list[list[PersonaChunk]] = []
        for window in windows:
            refined.extend(self._topic_split(window))
        capped: list[list[PersonaChunk]] = []
        for window in refined:
            for i in range(0, len(window), self._settings.max_cluster_chunks):
                capped.append(window[i : i + self._settings.max_cluster_chunks])
        return capped, deferred

    def _topic_split(self, window: list[PersonaChunk]) -> list[list[PersonaChunk]]:
        """Embedding refinement (K8-D-8): split where adjacent similarity dips.

        Freshly encodes member texts through the injected embedder (no
        transport widening; deterministic for a deterministic embedder).
        ``None`` embedder or a tiny window ⇒ no split.
        """
        min_splittable = 4  # below this a topic split over-fragments
        if self._embedder is None or len(window) < min_splittable:
            return [window]
        vectors = self._embedder.encode([c.text for c in window])
        parts: list[list[PersonaChunk]] = []
        current = [window[0]]
        for i in range(1, len(window)):
            if _cosine(vectors[i - 1], vectors[i]) < self._settings.cluster_split_threshold:
                parts.append(current)
                current = []
            current.append(window[i])
        parts.append(current)
        return parts

    def _candidate_for(
        self, persona_id: str, gist_text: str, window_end: datetime
    ) -> KnowledgeCandidate:
        """One minimal, contract-conformant candidate per cluster (K8-D-9)."""
        return KnowledgeCandidate(
            concept_name=f"episode {window_end:%Y-%m-%d %H:%M}",
            content=gist_text,
            node_kind=NodeKind.CONCEPT,
            provenance=NodeProvenance(
                source=WriteSource.SYSTEM,
                persona_id=persona_id,
                written_at=window_end,
                grounding="episodic cluster gist (from originals)",
                reason="sleep-time episodic consolidation",
            ),
            valid_at=window_end,
        )


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity for L2-normalised vectors (a plain dot product)."""
    return sum(x * y for x, y in zip(a, b, strict=True))
