"""Milestone-granularity episodic distillation (Spec A2, T10; D-A2-4).

Proves the anti-spam discipline: the loop's per-leg episodic write lands in the throwaway
sink (NOT the persona's episodic), and only milestones are promoted — a CONTINUE leg with no
new conclusion records nothing.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from persona.audit import MemoryAuditLogger
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.engine import EpisodicConsolidationEngine
from persona.stores.episodic import EpisodicStore
from persona.stores.lifecycle import EpisodicSettings
from persona.stores.pyramid import EpisodicPyramid
from persona.stores.summarizer import StubSummarizer
from persona.tasks import TaskCheckpoint
from persona_runtime.legs import (
    MILESTONE_TEXT_CAP,
    LegDisposition,
    MilestoneRecorder,
    TaskEpisodicSink,
    TaskMilestone,
    milestone_for,
    render_milestone_summary,
)

_NOW = datetime(2026, 6, 25, 9, 0, tzinfo=UTC)


def _checkpoint(*conclusions: str) -> TaskCheckpoint:
    return TaskCheckpoint(
        task_id="t1",
        leg_id="leg",
        checkpoint_seq=0,
        progress_conclusions=conclusions,
        next_step="x",
        updated_at=_NOW,
    )


class _FakeEpisodic:
    """A minimal episodic store recording writes."""

    def __init__(self) -> None:
        self.chunks: list[PersonaChunk] = []

    def get_all(self, persona_id: str, *, include_superseded: bool = False) -> list[PersonaChunk]:  # noqa: ARG002
        return list(self.chunks)

    def write(self, persona_id: str, chunks: list[PersonaChunk], **_: object) -> None:  # noqa: ARG002
        self.chunks.extend(chunks)


# --- the sink absorbs the loop's per-leg write (no persona-episodic spam) -----


def test_episodic_sink_buffers_and_does_not_promote() -> None:
    sink = TaskEpisodicSink()
    chunk = PersonaChunk(
        id="c0",
        text="leg output",
        created_at=_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.SYSTEM, logical_id="c0", version=1, written_at=_NOW, written_by="x"
        ),
    )
    sink.write("persona_a", [chunk], source=WriteSource.SYSTEM, written_by="agentic.run")
    # buffered (task-scoped), available to the loop's index computation, but never the persona's.
    assert len(sink.get_all("persona_a", include_superseded=True)) == 1
    assert sink.query("persona_a", "anything") == []


# --- the milestone gate is restrained ----------------------------------------


def test_first_leg_is_task_started() -> None:
    m = milestone_for(
        is_first_leg=True,
        prior_checkpoint=None,
        new_checkpoint=_checkpoint("c1"),
        disposition=LegDisposition.CONTINUE,
    )
    assert m == TaskMilestone.TASK_STARTED


def test_new_conclusion_is_major_progress() -> None:
    m = milestone_for(
        is_first_leg=False,
        prior_checkpoint=_checkpoint("c1"),
        new_checkpoint=_checkpoint("c1", "c2"),
        disposition=LegDisposition.CONTINUE,
    )
    assert m == TaskMilestone.MAJOR_PROGRESS


def test_continue_leg_with_no_new_conclusion_is_not_a_milestone() -> None:
    # The anti-spam case: a leg that advanced but established nothing new → no episodic entry.
    m = milestone_for(
        is_first_leg=False,
        prior_checkpoint=_checkpoint("c1"),
        new_checkpoint=_checkpoint("c1"),
        disposition=LegDisposition.CONTINUE,
    )
    assert m is None


def test_completed_and_failed_are_milestones() -> None:
    assert (
        milestone_for(
            is_first_leg=False,
            prior_checkpoint=_checkpoint("c1"),
            new_checkpoint=_checkpoint("c1"),
            disposition=LegDisposition.COMPLETED,
        )
        == TaskMilestone.COMPLETED
    )
    assert (
        milestone_for(
            is_first_leg=False,
            prior_checkpoint=_checkpoint("c1"),
            new_checkpoint=_checkpoint("c1"),
            disposition=LegDisposition.FAILED,
        )
        == TaskMilestone.FAILED
    )


# --- the recorder writes ONE chunk per milestone -----------------------------


def test_recorder_writes_one_tagged_chunk() -> None:
    store = _FakeEpisodic()
    MilestoneRecorder(store).record(
        "persona_a", TaskMilestone.MAJOR_PROGRESS, "found 1620kr", task_id="t1"
    )
    assert len(store.chunks) == 1
    chunk = store.chunks[0]
    assert chunk.text == "found 1620kr"
    assert chunk.metadata["source"] == "task_milestone"
    assert chunk.metadata["milestone"] == "major_progress"
    assert chunk.metadata["task_id"] == "t1"


# --- the note the persona will later read -------------------------------------


def test_every_milestone_renders_a_note_naming_the_task() -> None:
    """Whichever milestone it is, the persona can answer "did you look into X?" from the text.

    The goal is the only identifier the sentence carries: recalled months later there is no
    task row beside it, so a note that did not name the task would be a memory of nothing.
    """
    goal = "find a two-bedroom flat in Kristiansand"
    for milestone in TaskMilestone:
        note = render_milestone_summary(milestone, goal=goal)
        assert goal in note, milestone


def test_each_milestone_says_which_one_it_was() -> None:
    """Started, progressed, waiting, completed and failed read as five different events."""
    goal = "book the summer trip"
    notes = {m: render_milestone_summary(m, goal=goal) for m in TaskMilestone}
    assert "started" in notes[TaskMilestone.TASK_STARTED]
    assert "progress" in notes[TaskMilestone.MAJOR_PROGRESS]
    assert "waiting on you" in notes[TaskMilestone.WAITING]
    assert "completed" in notes[TaskMilestone.COMPLETED]
    assert "failed" in notes[TaskMilestone.FAILED]
    assert len(set(notes.values())) == len(TaskMilestone)  # no two read the same


def test_progress_carries_the_new_conclusion_and_the_others_do_not() -> None:
    """A progress note is worth writing BECAUSE something was learned, so it carries it."""
    progress = render_milestone_summary(
        TaskMilestone.MAJOR_PROGRESS, goal="find a flat", detail="three listings under 12000kr"
    )
    assert "three listings under 12000kr" in progress
    # The other four are news about the task, not about a finding.
    assert "three listings" not in render_milestone_summary(
        TaskMilestone.COMPLETED, goal="find a flat", detail="three listings under 12000kr"
    )


def test_a_long_goal_is_capped_rather_than_pasted_whole() -> None:
    """A milestone is a memory of what happened, not a second copy of the contract."""
    note = render_milestone_summary(TaskMilestone.COMPLETED, goal="x" * 500)
    assert len(note) < MILESTONE_TEXT_CAP + 40
    assert note.endswith("….")


# --- producer + consumer: a milestone chunk is transient to consolidation ------


class _InMemoryBackend:
    """The slice of ``Backend`` the episodic store, the pyramid and the engine touch."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], dict[str, PersonaChunk]] = {}

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        bucket = self.rows.setdefault((persona_id, store_kind), {})
        for c in chunks:
            bucket[c.id] = c

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return list(self.rows.get((persona_id, store_kind), {}).values())

    def recent(self, *, persona_id: str, store_kind: str, limit: int) -> list[PersonaChunk]:
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
        bucket = self.rows.get((persona_id, store_kind), {})
        for cid in ids:
            bucket.pop(cid, None)

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.rows.pop((persona_id, store_kind), None)

    def set_bands(self, *, persona_id: str, store_kind: str, bands: dict[str, int]) -> None:
        bucket = self.rows.get((persona_id, store_kind), {})
        for cid, band in bands.items():
            if cid in bucket:
                bucket[cid] = bucket[cid].model_copy(update={"band": band})

    def band_histogram(self, *, persona_id: str, store_kind: str) -> dict[int, int]:
        histogram: dict[int, int] = {}
        for c in self.rows.get((persona_id, store_kind), {}).values():
            histogram[c.band] = histogram.get(c.band, 0) + 1
        return histogram

    def query(self, **_: object) -> list[PersonaChunk]:
        return []

    def count(self, **_: object) -> int:
        return 0

    def reinforce(self, **_: object) -> None:
        return


class _RecordingMerge:
    def __init__(self) -> None:
        self.candidates: list[tuple[str, Any]] = []

    def merge(self, owner_id: str, candidate: Any) -> Any:  # noqa: ANN401 — the merge port
        self.candidates.append((owner_id, candidate))
        return object()


def test_a_recorded_milestone_is_excluded_from_graph_consolidation() -> None:
    """The producer and its shipped consumer, in one test (ENGINEERING_STANDARDS §6b).

    ``persona.stores.engine`` has always counted ``task_milestone`` as a transient source, so
    a task's own notices never graduate into the knowledge graph: a persona should remember
    that it ran an errand without deciding the errand is a fact about the world. Until the
    recorder was wired that consumer had no producer at all. This drives the REAL recorder
    into the REAL episodic store and the REAL engine over the result.
    """
    backend = _InMemoryBackend()
    audit = MemoryAuditLogger()
    store = EpisodicStore(backend=backend, audit_logger=audit)
    recorder = MilestoneRecorder(store)

    recorder.record(
        "p1",
        TaskMilestone.TASK_STARTED,
        render_milestone_summary(TaskMilestone.TASK_STARTED, goal="find a flat"),
        task_id="t1",
    )
    recorder.record(
        "p1",
        TaskMilestone.COMPLETED,
        render_milestone_summary(TaskMilestone.COMPLETED, goal="find a flat"),
        task_id="t1",
    )

    graph = _RecordingMerge()
    engine = EpisodicConsolidationEngine(
        backend=backend,
        pyramid=EpisodicPyramid(backend=backend, audit_logger=audit),
        summarizer=StubSummarizer(),
        graph=graph,
        settings=EpisodicSettings(min_chunks_per_run=2, cluster_gap_minutes=45.0),
        embedder=None,
    )
    # The recorder stamps ``created_at`` from the wall clock, so the engine is run from a
    # point at which that window has closed (an open trailing window is deferred by design).
    report = asyncio.run(engine.run("u1", "p1", now=datetime.now(UTC) + timedelta(hours=2)))

    assert report.candidates_excluded_transient == 1
    assert report.candidates_emitted == 0
    assert graph.candidates == []
