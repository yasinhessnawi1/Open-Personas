"""The background document index (D-FV0-31, third security review).

Unit level: slices, cursors, idempotency, deletes while queued and while running, the
per-owner quota, failure marking, the A0 handler's continuation and its forced-kill re-run,
both schedulers, and the indexer thread (niceness, one core, the owner scope). The real
claim against Postgres (the serial type) is in
``tests/integration/test_document_index_jobs.py``.
"""

# ruff: noqa: SLF001, ARG002, ARG005, ANN401, PLC0415 - fakes and private seams are the point here.
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from _document_upload import upload
from persona.documents.chunker import ChunkCursor
from persona.errors import PermanentJobError
from persona.stores.document_store import DocumentStore
from persona_api.services import document_index, document_service
from persona_api.services.document_index import (
    DOCUMENT_INDEX_JOB_TYPE,
    EMBED_BATCH,
    MAX_PENDING_DOCUMENTS_PER_OWNER,
    OWNER_CHUNKS_PER_HOUR,
    QUOTA_RETRY_SECONDS,
    SLICE_CHUNKS,
    DocumentIndexBacklogFullError,
    DocumentIndexDeps,
    DocumentIndexer,
    DocumentIndexHandler,
    DocumentIndexJobPayload,
    DocumentIndexUnreadableError,
    InMemoryIndexQuota,
    JobDocumentIndexScheduler,
    LocalDocumentIndexQueue,
    run_slice,
)
from persona_api.storage import LocalFileStorage

if TYPE_CHECKING:
    from persona.schema.chunks import PersonaChunk

OWNER, PERSONA, CONV = "owner", "astrid", "conv1"


class _Backend:
    """An in-memory store backend that records each write's size."""

    def __init__(self) -> None:
        self.rows: dict[str, PersonaChunk] = {}
        self.writes: list[int] = []
        self.on_write: Any = None

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        self.writes.append(len(chunks))
        for chunk in chunks:
            self.rows[chunk.id] = chunk
        if self.on_write is not None:
            self.on_write(len(self.writes))

    def query(self, **_: Any) -> list[PersonaChunk]:
        return []

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return [c for c in self.rows.values() if c.id.startswith(f"{persona_id}::")]

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.rows = {k: v for k, v in self.rows.items() if not k.startswith(f"{persona_id}::")}

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        for chunk_id in ids:
            self.rows.pop(chunk_id, None)


@pytest.fixture
def backend() -> _Backend:
    return _Backend()


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    root = tmp_path / "workspace"
    root.mkdir()
    return LocalFileStorage(root)


def _deps(storage: LocalFileStorage, backend: _Backend, **kwargs: Any) -> DocumentIndexDeps:  # noqa: ANN401
    return DocumentIndexDeps(
        file_storage=storage,
        document_store=lambda: DocumentStore(backend=backend),  # type: ignore[arg-type]
        indexer=kwargs.pop("indexer", DocumentIndexer()),
        quota=kwargs.pop("quota", InMemoryIndexQuota()),
        **kwargs,
    )


def _long_text(paragraphs: int = 120) -> bytes:
    return "\n\n".join(
        f"Paragraph {p}. " + " ".join(f"w{p}x{k}" for k in range(90)) for p in range(paragraphs)
    ).encode()


def _retrieval_doc(
    storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch, name: str = "report.txt"
) -> document_service.DocumentRef:
    monkeypatch.setenv("PERSONA_DOC_INJECT_THRESHOLD", "100")
    ref = upload(
        file_storage=storage,
        owner_id=OWNER,
        persona_id=PERSONA,
        conversation_id=CONV,
        file_bytes=_long_text(),
        filename=name,
    )
    assert (ref.strategy.value, ref.index_state, ref.chunks_indexed) == ("retrieval", "pending", 0)
    return ref


def _slice(
    deps: DocumentIndexDeps, ref: Any, cursor: ChunkCursor, limit: int = SLICE_CHUNKS
) -> Any:  # noqa: ANN401
    return run_slice(
        deps=deps,
        owner_id=OWNER,
        persona_id=PERSONA,
        conversation_id=CONV,
        doc_ref=ref.doc_ref,
        cursor=cursor,
        limit=limit,
    )


def _listed(storage: LocalFileStorage) -> list[document_service.DocumentRef]:
    return document_service.list_for_conversation(
        file_storage=storage, owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV
    )


def _files(storage: LocalFileStorage) -> list[str]:
    root = storage._root / OWNER / PERSONA / "conversations" / CONV / "documents"
    return sorted(p.name for p in root.iterdir()) if root.exists() else []


# ----- the upload stores, the index embeds ------------------------------------------


class TestTheUploadEmbedsNothing:
    def test_a_retrieval_upload_is_pending_and_the_store_is_untouched(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        assert backend.rows == {}
        assert not any(name.endswith(".index.json") for name in _files(storage))
        assert f"{ref.doc_ref}.txt.text.json" in _files(storage)


# ----- one slice ------------------------------------------------------------------


class TestASlice:
    def test_it_writes_at_most_its_limit_in_batches_of_four(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        outcome = _slice(_deps(storage, backend), ref, ChunkCursor())
        assert outcome.state == "indexing"
        assert outcome.chunks_written == SLICE_CHUNKS
        assert backend.writes == [EMBED_BATCH] * (SLICE_CHUNKS // EMBED_BATCH)
        assert outcome.cursor.index == SLICE_CHUNKS
        (listed,) = _listed(storage)
        assert (listed.index_state, listed.chunks_indexed) == ("indexing", SLICE_CHUNKS)

    def test_slices_reach_ready_with_contiguous_ids_and_no_duplicates(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend)
        cursor, slices = ChunkCursor(), 0
        while True:
            outcome = _slice(deps, ref, cursor)
            slices += 1
            if outcome.state != "indexing":
                break
            cursor = outcome.cursor
        assert outcome.state == "ready"
        assert slices > 1
        ids = sorted(backend.rows)
        assert ids == [f"{CONV}::document::{ref.doc_ref}::{i:04d}" for i in range(len(ids))]
        assert sum(backend.writes) >= len(ids)
        (listed,) = _listed(storage)
        assert (listed.index_state, listed.chunks_indexed) == ("ready", len(ids))

    def test_a_slice_run_twice_writes_the_same_rows(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A worker killed after writing, then the job re-run: overwrite by id, no duplicates."""
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend)
        first = _slice(deps, ref, ChunkCursor())
        _slice(deps, ref, first.cursor)
        snapshot = {k: v.text for k, v in backend.rows.items()}
        again = _slice(deps, ref, first.cursor)
        assert {k: v.text for k, v in backend.rows.items()} == snapshot
        assert again.cursor == _slice(deps, ref, first.cursor).cursor

    def test_the_first_slice_clears_what_an_earlier_run_left(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend)
        stray = f"{CONV}::document::{ref.doc_ref}::0999"
        from persona.schema.documents import DocumentChunk

        older = DocumentChunk(  # a chunk an earlier chunker wrote
            id=stray,
            text="old",
            doc_ref=ref.doc_ref,
            format="txt",
            title="t",
            created_at=datetime.now(UTC),
        )
        deps.document_store().write(CONV, [older])
        assert stray in backend.rows
        _slice(deps, ref, ChunkCursor())
        assert stray in backend.rows  # the slice itself deletes nothing (fifth review F2)
        entry = document_index.BacklogEntry(
            persona_id=PERSONA,
            conversation_id=CONV,
            doc_ref=ref.doc_ref,
            added_at=datetime.now(UTC),
        )
        key = document_service.document_original_key(
            ref, owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV
        )
        storage.delete(f"{key}{document_index.DOCUMENT_INDEX_SIDECAR_SUFFIX}")  # a fresh start
        asyncio.run(document_index.index_one_slice(deps, entry, owner_id=OWNER))
        assert stray not in backend.rows  # its caller cleared it before submitting it

    def test_the_document_chunk_limit_makes_it_partial(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(document_index, "MAX_DOCUMENT_CHUNKS", 40)
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend)
        first = _slice(deps, ref, ChunkCursor())
        second = _slice(deps, ref, first.cursor)
        assert (second.state, len(backend.rows)) == ("partial", 40)
        (listed,) = _listed(storage)
        assert (listed.index_state, listed.chunks_indexed) == ("partial", 40)

    def test_a_document_with_no_stored_text_cannot_be_indexed(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        (storage._root / ref.workspace_path).with_name(f"{ref.doc_ref}.txt.text.json").unlink()
        with pytest.raises(DocumentIndexUnreadableError):
            _slice(_deps(storage, backend), ref, ChunkCursor())


class TestDeletes:
    """No orphan chunk or progress file survives a delete, whenever it lands."""

    def test_deleted_while_queued_writes_nothing(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend)
        document_service.remove_document(
            file_storage=storage,
            owner_id=OWNER,
            persona_id=PERSONA,
            conversation_id=CONV,
            doc_ref=ref.doc_ref,
            document_store=deps.document_store(),
        )
        outcome = _slice(deps, ref, ChunkCursor())
        assert (outcome.state, outcome.chunks_written) == ("gone", 0)
        assert backend.rows == {}
        assert _files(storage) == []

    def test_deleted_while_running_leaves_no_chunk_and_no_progress(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend)

        def _delete_mid_slice(write_number: int) -> None:
            if write_number == 3:  # the delete lands between two of the slice's batches
                document_service.remove_document(
                    file_storage=storage,
                    owner_id=OWNER,
                    persona_id=PERSONA,
                    conversation_id=CONV,
                    doc_ref=ref.doc_ref,
                    document_store=deps.document_store(),
                )

        backend.on_write = _delete_mid_slice
        outcome = _slice(deps, ref, ChunkCursor())
        assert outcome.state == "gone"
        assert backend.rows == {}
        assert _files(storage) == []

    def test_deleted_just_after_the_progress_is_written_leaves_nothing(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The last window: the delete lands after the slice's last write of all."""
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend)
        real_write = document_service.write_index_progress

        def _write_then_delete(*args: Any, **kwargs: Any) -> None:
            real_write(*args, **kwargs)
            for name in _files(storage):
                if not name.endswith(".index.json"):  # the delete's listing ran earlier
                    storage.delete(f"{OWNER}/{PERSONA}/conversations/{CONV}/documents/{name}")
            deps.document_store().delete_document(CONV, ref.doc_ref)

        monkeypatch.setattr(document_service, "write_index_progress", _write_then_delete)
        outcome = _slice(deps, ref, ChunkCursor())
        assert outcome.state == "gone"
        assert backend.rows == {}
        assert _files(storage) == []

    def test_a_document_whose_files_are_gone_loses_its_chunks_at_the_next_slice(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A delete cut short after the files, before the chunks: the next slice finishes it."""
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend)
        first = _slice(deps, ref, ChunkCursor())
        for name in _files(storage):
            storage.delete(f"{OWNER}/{PERSONA}/conversations/{CONV}/documents/{name}")
        assert backend.rows
        assert _slice(deps, ref, first.cursor).state == "gone"
        assert backend.rows == {}

    def test_deleted_after_a_slice_takes_its_chunks_and_progress(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend)
        _slice(deps, ref, ChunkCursor())
        document_service.remove_document(
            file_storage=storage,
            owner_id=OWNER,
            persona_id=PERSONA,
            conversation_id=CONV,
            doc_ref=ref.doc_ref,
            document_store=deps.document_store(),
        )
        assert backend.rows == {}
        assert _files(storage) == []


# ----- the A0 handler ---------------------------------------------------------------


class TestTheDeleteWindow:
    """Fourth review finding 9: no ``.index.json`` outlives a delete, whenever a slice writes."""

    def test_the_meta_sidecar_goes_first(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        _slice(_deps(storage, backend), ref, ChunkCursor())
        deleted: list[str] = []
        real_delete = storage.delete

        def _delete(key: str) -> bool:
            deleted.append(key)
            return real_delete(key)

        real_list = storage.list

        def _list_meta_last(prefix: str) -> Any:  # noqa: ANN401
            # whatever order a backend lists in, the meta sidecar still goes first
            return iter(sorted(real_list(prefix), key=lambda o: o.key.endswith(".meta.json")))

        monkeypatch.setattr(storage, "delete", _delete)
        monkeypatch.setattr(storage, "list", _list_meta_last)
        document_service.remove_document(
            file_storage=storage,
            owner_id=OWNER,
            persona_id=PERSONA,
            conversation_id=CONV,
            doc_ref=ref.doc_ref,
            document_store=DocumentStore(backend=backend),  # type: ignore[arg-type]
        )
        assert len(deleted) > 1
        assert deleted[0].endswith(".meta.json")
        assert _files(storage) == []

    @pytest.mark.parametrize("whole_conversation", [False, True])
    def test_a_progress_file_written_after_the_listing_is_taken_by_the_second_pass(
        self,
        storage: LocalFileStorage,
        backend: _Backend,
        monkeypatch: pytest.MonkeyPatch,
        whole_conversation: bool,
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        key = document_service.document_original_key(
            ref, owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV
        )
        assert key is not None
        real_list = storage.list
        listings = [0]

        def _list(prefix: str) -> Any:  # noqa: ANN401
            listed = list(real_list(prefix))
            listings[0] += 1
            if listings[0] == 1:
                # The slice's progress write lands between the listing and the deletes.
                document_service.write_index_progress(
                    storage,
                    key,
                    document_service.IndexProgress(state="indexing", chunks_indexed=32),
                )
            return iter(listed)

        monkeypatch.setattr(storage, "list", _list)
        store = DocumentStore(backend=backend)  # type: ignore[arg-type]
        if whole_conversation:
            document_service.remove_all_for_conversation(
                file_storage=storage,
                owner_id=OWNER,
                persona_id=PERSONA,
                conversation_id=CONV,
                document_store=store,
            )
        else:
            document_service.remove_document(
                file_storage=storage,
                owner_id=OWNER,
                persona_id=PERSONA,
                conversation_id=CONV,
                doc_ref=ref.doc_ref,
                document_store=store,
            )
        assert _files(storage) == []


class TestAConversationDeleteDropsItsBacklog:
    def test_both_backlogs(self) -> None:
        for backlog in (document_index.InMemoryIndexBacklog(),):
            backlog.add(OWNER, persona_id=PERSONA, conversation_id="c1", doc_ref="a")
            backlog.add(OWNER, persona_id=PERSONA, conversation_id="c1", doc_ref="b")
            backlog.add(OWNER, persona_id=PERSONA, conversation_id="c2", doc_ref="a")
            backlog.add("other", persona_id=PERSONA, conversation_id="c1", doc_ref="a")
            backlog.remove_conversation(OWNER, "c1")
            assert backlog.count(OWNER) == 1
            assert backlog.has(OWNER, conversation_id="c2", doc_ref="a")
            assert backlog.count("other") == 1
            backlog.remove_conversation(OWNER, "c2")
            assert backlog.count(OWNER) == 0
            backlog.remove_conversation(OWNER, "c2")  # idempotent

    def test_the_schedulers(
        self,
        storage: LocalFileStorage,
        backend: _Backend,
        job_scheduler: tuple[JobDocumentIndexScheduler, list[str]],
    ) -> None:
        scheduler, _ = job_scheduler
        local = LocalDocumentIndexQueue(_deps(storage, backend))
        for sched, backlog in ((scheduler, scheduler._backlog), (local, local._backlog)):
            backlog.add(OWNER, persona_id=PERSONA, conversation_id="c1", doc_ref="a")
            asyncio.run(sched.forget_conversation(owner_id=OWNER, conversation_id="c1"))
            assert backlog.count(OWNER) == 0


class _Context:
    def __init__(self, attempt: int = 1, max_attempts: int = 4, owner: str = OWNER) -> None:
        self.owner_id = owner
        self.job_id = "job-1"
        self.attempt = attempt
        self.max_attempts = max_attempts


class _Queue:
    """Records each owner turn the handler queues."""

    def __init__(self) -> None:
        self.turns: list[tuple[str, DocumentIndexJobPayload, datetime | None]] = []

    def enqueue(
        self, owner_id: str, payload: DocumentIndexJobPayload, scheduled_at: datetime | None
    ) -> None:
        self.turns.append((owner_id, payload, scheduled_at))


def _handler(deps: DocumentIndexDeps, queue: _Queue) -> DocumentIndexHandler:
    return DocumentIndexHandler(
        deps=deps,
        enqueue=queue.enqueue,
        attempt_of=lambda ctx: (ctx.attempt, ctx.max_attempts),
    )


def _wait(deps: DocumentIndexDeps, ref: Any, owner: str = OWNER, conv: str = CONV) -> None:  # noqa: ANN401
    deps.backlog.add(owner, persona_id=PERSONA, conversation_id=conv, doc_ref=ref.doc_ref)


def _run(handler: DocumentIndexHandler, payload: Any, context: _Context) -> None:  # noqa: ANN401
    asyncio.run(handler.handle(payload, context))  # type: ignore[arg-type]


class TestTheJobHandler:
    def test_each_turn_queues_the_owners_next_and_the_last_none(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        finished: list[str] = []
        deps = _deps(storage, backend, on_finished=finished.append)
        _wait(deps, ref)
        queue = _Queue()
        payload, ran = DocumentIndexJobPayload(), 0
        while True:
            before = len(queue.turns)
            _run(_handler(deps, queue), payload, _Context())
            ran += 1
            if len(queue.turns) == before:
                break
            owner, following, scheduled_at = queue.turns[-1]
            assert owner == OWNER
            assert scheduled_at is None  # straight to the back of the queue
            assert following.turn == payload.turn + 1
            assert following.cursor_doc_ref == ref.doc_ref
            payload = following
        assert ran > 1
        assert finished == [OWNER]
        assert deps.backlog.count(OWNER) == 0
        (listed,) = _listed(storage)
        assert listed.index_state == "ready"

    def test_a_turn_resumes_from_the_recorded_progress_and_duplicates_nothing(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A0 forced-kill: the same turn re-runs; it carries on from the progress."""
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend)
        _wait(deps, ref)
        handler = _handler(deps, _Queue())
        _run(handler, DocumentIndexJobPayload(), _Context(attempt=1))
        _run(handler, DocumentIndexJobPayload(), _Context(attempt=2))
        indexes = sorted(int(k.rsplit("::", 1)[1]) for k in backend.rows)
        assert indexes == list(range(2 * SLICE_CHUNKS))

    def test_an_owner_over_the_quota_is_parked_until_its_reset_and_embeds_nothing(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        quota = InMemoryIndexQuota()
        quota.add(OWNER, OWNER_CHUNKS_PER_HOUR)
        monkeypatch.setattr(quota, "reset_in", lambda _owner: 42.0)
        deps = _deps(storage, backend, quota=quota)
        _wait(deps, ref)
        queue = _Queue()
        before = datetime.now(UTC)
        _run(_handler(deps, queue), DocumentIndexJobPayload(), _Context())
        assert backend.rows == {}
        ((owner, payload, scheduled_at),) = queue.turns  # parked: one turn, once
        assert owner == OWNER
        assert payload.cursor_doc_ref == ref.doc_ref
        assert scheduled_at is not None
        assert before + timedelta(seconds=41) <= scheduled_at <= before + timedelta(seconds=44)
        assert deps.backlog.count(OWNER) == 1
        (listed,) = _listed(storage)
        assert listed.index_state == "pending"

    def test_a_park_is_never_longer_than_the_retry_bound(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        quota = InMemoryIndexQuota()
        quota.add(OWNER, OWNER_CHUNKS_PER_HOUR)  # the real reset: an hour away
        deps = _deps(storage, backend, quota=quota)
        _wait(deps, ref)
        queue = _Queue()
        before = datetime.now(UTC)
        _run(_handler(deps, queue), DocumentIndexJobPayload(), _Context())
        ((_, _, scheduled_at),) = queue.turns
        assert scheduled_at is not None
        assert scheduled_at <= before + timedelta(seconds=QUOTA_RETRY_SECONDS + 1)
        assert scheduled_at >= before + timedelta(seconds=QUOTA_RETRY_SECONDS - 1)

    def test_the_in_memory_quota_resets_when_its_oldest_charge_leaves_the_hour(self) -> None:
        quota = InMemoryIndexQuota(window_seconds=100.0)
        assert quota.reset_in(OWNER) == 0.0
        quota.add(OWNER, 5)
        assert 99.0 < quota.reset_in(OWNER) <= 100.0

    def test_the_quota_left_bounds_the_slice(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        quota = InMemoryIndexQuota()
        quota.add(OWNER, OWNER_CHUNKS_PER_HOUR - 10)
        deps = _deps(storage, backend, quota=quota)
        _wait(deps, ref)
        _run(_handler(deps, _Queue()), DocumentIndexJobPayload(), _Context())
        assert len(backend.rows) == 10
        assert quota.used(OWNER) == OWNER_CHUNKS_PER_HOUR

    def test_a_document_without_text_fails_and_the_turn_moves_on(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        broken = _retrieval_doc(storage, monkeypatch, "broken.txt")
        good = _retrieval_doc(storage, monkeypatch, "good.txt")
        (storage._root / broken.workspace_path).with_name(
            f"{broken.doc_ref}.txt.text.json"
        ).unlink()
        deps = _deps(storage, backend)
        _wait(deps, broken)
        _wait(deps, good)
        _run(_handler(deps, _Queue()), DocumentIndexJobPayload(), _Context())
        states = {r.doc_ref: r.index_state for r in _listed(storage)}
        assert states == {broken.doc_ref: "failed", good.doc_ref: "indexing"}
        assert not deps.backlog.has(OWNER, conversation_id=CONV, doc_ref=broken.doc_ref)
        assert all(f"::{good.doc_ref}::" in k for k in backend.rows)

    def test_gone_and_finished_documents_leave_without_a_turn_of_their_own(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        good = _retrieval_doc(storage, monkeypatch, "good.txt")
        deps = _deps(storage, backend)
        deps.backlog.add(OWNER, persona_id=PERSONA, conversation_id=CONV, doc_ref="gone-1a2b3c4d")
        _wait(deps, good)
        _run(_handler(deps, _Queue()), DocumentIndexJobPayload(), _Context())
        assert not deps.backlog.has(OWNER, conversation_id=CONV, doc_ref="gone-1a2b3c4d")
        assert len(backend.rows) == SLICE_CHUNKS  # the same turn went on to the real one

    def test_a_turn_steps_past_at_most_the_skip_limit(
        self, storage: LocalFileStorage, backend: _Backend
    ) -> None:
        deps = _deps(storage, backend)
        for n in range(document_index._TURN_SKIP_LIMIT + 5):
            deps.backlog.add(OWNER, persona_id=PERSONA, conversation_id=CONV, doc_ref=f"gone-{n}")
        queue = _Queue()
        _run(_handler(deps, queue), DocumentIndexJobPayload(), _Context())
        assert deps.backlog.count(OWNER) == 5
        assert len(queue.turns) == 1  # the rest wait for the owner's next turn

    def test_a_document_already_indexed_is_not_indexed_again(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A listing read just before the index finished can put it back in the backlog."""
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend)
        cursor = ChunkCursor()
        while True:
            outcome = _slice(deps, ref, cursor, limit=10_000)
            if outcome.state != "indexing":
                break
            cursor = outcome.cursor
        writes = len(backend.writes)
        _wait(deps, ref)
        _run(_handler(deps, _Queue()), DocumentIndexJobPayload(), _Context())
        assert len(backend.writes) == writes
        assert deps.backlog.count(OWNER) == 0

    def test_the_last_attempt_says_failed_first_and_success_overwrites_it(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        seen: list[str] = []

        def _record(_: int) -> None:
            if not seen:
                seen.append(_listed(storage)[0].index_state)

        backend.on_write = _record
        deps = _deps(storage, backend)
        _wait(deps, ref)
        _run(_handler(deps, _Queue()), DocumentIndexJobPayload(), _Context(attempt=4))
        assert seen == ["failed"]  # while the last try runs, the truth is "failed"
        assert _listed(storage)[0].index_state == "indexing"

    def test_a_last_attempt_that_fails_stays_failed_and_the_owner_goes_on(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = _retrieval_doc(storage, monkeypatch, "bad.txt")
        other = _retrieval_doc(storage, monkeypatch, "other.txt")

        def _boom(_: int) -> None:
            raise RuntimeError("the embedder fell over")

        backend.on_write = _boom
        deps = _deps(storage, backend)
        _wait(deps, bad)
        _wait(deps, other)
        queue = _Queue()
        with pytest.raises(PermanentJobError):
            _run(_handler(deps, queue), DocumentIndexJobPayload(), _Context(attempt=4))
        states = {r.doc_ref: r.index_state for r in _listed(storage)}
        assert states[bad.doc_ref] == "failed"
        assert not deps.backlog.has(OWNER, conversation_id=CONV, doc_ref=bad.doc_ref)
        ((owner, payload, scheduled_at),) = queue.turns
        assert (owner, payload.cursor_doc_ref, scheduled_at) == (OWNER, None, None)

    def test_a_last_attempt_that_fails_with_nothing_else_waiting_queues_nothing(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)

        def _boom(_: int) -> None:
            raise RuntimeError("the embedder fell over")

        backend.on_write = _boom
        deps = _deps(storage, backend)
        _wait(deps, ref)
        queue = _Queue()
        with pytest.raises(PermanentJobError):
            _run(_handler(deps, queue), DocumentIndexJobPayload(), _Context(attempt=4))
        assert queue.turns == []

    def test_an_earlier_attempt_that_fails_keeps_its_state_and_place_for_the_retry(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)

        def _boom(_: int) -> None:
            raise RuntimeError("transient")

        backend.on_write = _boom
        deps = _deps(storage, backend)
        _wait(deps, ref)
        with pytest.raises(RuntimeError):
            _run(_handler(deps, _Queue()), DocumentIndexJobPayload(), _Context(attempt=1))
        assert _listed(storage)[0].index_state == "pending"
        assert deps.backlog.has(OWNER, conversation_id=CONV, doc_ref=ref.doc_ref)

    def test_the_job_type_is_registered_serial(
        self, storage: LocalFileStorage, backend: _Backend
    ) -> None:
        from persona.jobs import JobRegistry

        registry = JobRegistry()
        engine = object()
        given = _deps(storage, backend)
        document_index.register_document_index_handler(
            registry,
            deps=given,
            rls_engine=engine,  # type: ignore[arg-type]
        )
        spec = registry.get(DOCUMENT_INDEX_JOB_TYPE)
        assert spec.serial is True
        assert registry.serial_types() == [DOCUMENT_INDEX_JOB_TYPE]
        # the jobs path reads the same Postgres backlog enqueue_document_index writes to,
        # never the in-memory one the given deps carried
        used = spec.handler._deps.backlog  # type: ignore[attr-defined]
        assert isinstance(given.backlog, document_index.InMemoryIndexBacklog)
        assert used is not given.backlog
        assert isinstance(used, document_index.PostgresIndexBacklog)
        assert used._engine is engine


class TestThePayloadIds:
    """Fourth review finding 11: a crafted job cannot name a key outside its owner's own."""

    @pytest.mark.parametrize(
        ("conversation_id", "doc_ref"),
        [
            ("cA/../../../../B/pB/conversations/cB", "report-1a2b3c4d"),  # the reviewer's
            ("cA", "../report"),
            ("cA", "a/b"),
            ("..", "report"),
            ("cA", "report..x"),
            ("", "report"),
            ("cA", ".hidden"),
        ],
    )
    def test_a_traversal_or_separator_is_refused(self, conversation_id: str, doc_ref: str) -> None:
        with pytest.raises(ValueError, match="accepted shape"):  # a ValidationError
            DocumentIndexJobPayload(cursor_conversation_id=conversation_id, cursor_doc_ref=doc_ref)
        with pytest.raises(ValueError, match="accepted shape"):
            document_index.InMemoryIndexBacklog().add(
                OWNER, persona_id=PERSONA, conversation_id=conversation_id, doc_ref=doc_ref
            )

    @pytest.mark.parametrize(
        ("persona_id", "conversation_id", "doc_ref"),
        [
            ("persona_0123abcd", "conv_0123abcd", "q3-report-1a2b3c4d"),
            ("astrid", "conv1", "notes.v2-1a2b3c4d"),
        ],
    )
    def test_real_ids_pass(self, persona_id: str, conversation_id: str, doc_ref: str) -> None:
        document_index.check_index_ids(
            persona_id=persona_id, conversation_id=conversation_id, doc_ref=doc_ref
        )
        payload = DocumentIndexJobPayload(
            cursor_conversation_id=conversation_id, cursor_doc_ref=doc_ref
        )
        assert payload.cursor_doc_ref == doc_ref

    def test_a_persona_id_with_a_separator_is_refused(self) -> None:
        with pytest.raises(ValueError, match="persona id"):
            document_index.check_index_ids(persona_id="p/x", conversation_id="c", doc_ref="d")

    def test_a_cursor_names_both_or_neither(self) -> None:
        with pytest.raises(ValueError, match="both its conversation and its document"):
            DocumentIndexJobPayload(cursor_conversation_id="conv1")

    def test_the_reviewers_old_shape_payload_is_refused(self) -> None:
        with pytest.raises(ValueError, match="Extra inputs are not permitted"):
            DocumentIndexJobPayload.model_validate(
                {
                    "persona_id": "pA",
                    "conversation_id": "cA/../../../../B/pB/conversations/cB",
                    "doc_ref": "report-1a2b3c4d",
                }
            )


class TestAnOlderGeneratedFile:
    """The coordinator's ruling: a generated file never indexed reads ``pending``, then is."""

    def _generated(self, storage: LocalFileStorage) -> document_service.DocumentRef:
        name = "board-game-plan-1a2b3c4d.md"
        prefix = f"{OWNER}/{PERSONA}/conversations/{CONV}/documents"
        storage.put(f"{prefix}/{name}", ("# Plan\n\n" + _long_text(30).decode()).encode())
        meta = {
            "doc_ref": "board-game-plan-1a2b3c4d",
            "filename": name,
            "title": "Board Game Plan",
            "format": "md",
            "workspace_path": f"{prefix}/{name}",
            "strategy": "retrieval",
            "token_count": 9000,
        }
        storage.put(f"{prefix}/{name}.meta.json", json.dumps(meta).encode())
        (ref,) = _listed(storage)
        return ref

    def test_it_reads_pending_and_an_upload_of_the_same_age_reads_ready(
        self, storage: LocalFileStorage
    ) -> None:
        ref = self._generated(storage)
        assert ref.index_state == "pending"
        uploaded = document_service.DocumentRef.model_validate(
            {
                **ref.model_dump(mode="json", exclude={"index_state", "chunks_indexed"}),
                "filename": "plan.md",
            }
        )
        assert uploaded.index_state == "ready"

    def test_the_job_re_reads_its_text_and_indexes_it(
        self, storage: LocalFileStorage, backend: _Backend
    ) -> None:
        from persona_api.services.document_parsing import ThreadDocumentParser

        ref = self._generated(storage)
        deps = _deps(storage, backend, parser=ThreadDocumentParser())
        _wait(deps, ref)
        _run(_handler(deps, _Queue()), DocumentIndexJobPayload(), _Context())
        assert backend.rows
        assert any(name.endswith(".md.text.json") for name in _files(storage))


# ----- scheduling ---------------------------------------------------------------------


@pytest.fixture
def job_scheduler(
    storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch
) -> tuple[JobDocumentIndexScheduler, list[str]]:
    """The jobs scheduler over an in-memory backlog, recording each owner turn it asks for."""
    turns: list[str] = []
    monkeypatch.setattr(
        document_index, "enqueue_owner_turn", lambda _engine, owner, *a, **k: turns.append(owner)
    )

    class _Q:
        _engine = object()

    scheduler = JobDocumentIndexScheduler(_Q(), storage)  # type: ignore[arg-type]
    scheduler._backlog = document_index.InMemoryIndexBacklog()  # type: ignore[assignment]
    return scheduler, turns


class TestTheJobScheduler:
    def test_an_upload_waits_in_its_owners_backlog_and_the_owner_gets_a_turn(
        self, job_scheduler: tuple[JobDocumentIndexScheduler, list[str]]
    ) -> None:
        scheduler, turns = job_scheduler
        asyncio.run(
            scheduler.schedule(
                owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV, doc_ref="r-1a2b3c4d"
            )
        )
        assert scheduler._backlog.has(OWNER, conversation_id=CONV, doc_ref="r-1a2b3c4d")
        assert turns == [OWNER]

    def test_the_backlog_is_capped_per_owner(
        self, job_scheduler: tuple[JobDocumentIndexScheduler, list[str]]
    ) -> None:
        scheduler, _ = job_scheduler
        for n in range(MAX_PENDING_DOCUMENTS_PER_OWNER - 1):
            scheduler._backlog.add(OWNER, persona_id=PERSONA, conversation_id=CONV, doc_ref=f"d{n}")
        scheduler.admit(OWNER)  # one place left
        scheduler._backlog.add(OWNER, persona_id=PERSONA, conversation_id=CONV, doc_ref="last")
        with pytest.raises(DocumentIndexBacklogFullError) as caught:
            scheduler.admit(OWNER)
        assert caught.value.context["reason"] == "index_backlog_full"
        scheduler.admit("someone-else")  # per owner

    def test_a_pick_up_respects_the_cap(
        self,
        storage: LocalFileStorage,
        monkeypatch: pytest.MonkeyPatch,
        job_scheduler: tuple[JobDocumentIndexScheduler, list[str]],
    ) -> None:
        """Fifth review: a pick-up puts back no more than the cap allows."""
        scheduler, _ = job_scheduler
        first = _retrieval_doc(storage, monkeypatch, "first.txt")
        second = _retrieval_doc(storage, monkeypatch, "second.txt")
        scheduler._backlog.add(
            OWNER, persona_id=PERSONA, conversation_id=CONV, doc_ref=first.doc_ref
        )
        for n in range(MAX_PENDING_DOCUMENTS_PER_OWNER - 1):
            scheduler._backlog.add(
                OWNER, persona_id=PERSONA, conversation_id="other", doc_ref=f"d{n}"
            )
        asyncio.run(
            scheduler.pick_up(
                owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV, refs=_listed(storage)
            )
        )
        assert scheduler._backlog.count(OWNER) == MAX_PENDING_DOCUMENTS_PER_OWNER
        assert scheduler._backlog.has(OWNER, conversation_id=CONV, doc_ref=first.doc_ref)
        assert not scheduler._backlog.has(OWNER, conversation_id=CONV, doc_ref=second.doc_ref)

    def test_the_cap_is_twenty(self) -> None:
        assert MAX_PENDING_DOCUMENTS_PER_OWNER == 20

    def test_a_pick_up_puts_back_pending_and_indexing_documents_whose_chain_was_lost(
        self,
        storage: LocalFileStorage,
        backend: _Backend,
        monkeypatch: pytest.MonkeyPatch,
        job_scheduler: tuple[JobDocumentIndexScheduler, list[str]],
    ) -> None:
        """Fourth review finding 9: a document stuck ``indexing`` with no job resumes."""
        scheduler, turns = job_scheduler
        pending = _retrieval_doc(storage, monkeypatch, "pending.txt")
        stuck = _retrieval_doc(storage, monkeypatch, "stuck.txt")
        _slice(_deps(storage, backend), stuck, ChunkCursor())  # then its chain was lost
        assert {r.index_state for r in _listed(storage)} == {"pending", "indexing"}
        asyncio.run(
            scheduler.pick_up(
                owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV, refs=_listed(storage)
            )
        )
        for ref in (pending, stuck):
            assert scheduler._backlog.has(OWNER, conversation_id=CONV, doc_ref=ref.doc_ref)
        assert turns == [OWNER]
        # and the owner's turn resumes the stuck one where it stopped
        deps = _deps(storage, backend, backlog=scheduler._backlog)
        _run(_handler(deps, _Queue()), DocumentIndexJobPayload(), _Context())
        indexes = sorted(
            int(k.rsplit("::", 1)[1]) for k in backend.rows if f"::{stuck.doc_ref}::" in k
        )
        assert indexes == list(range(SLICE_CHUNKS))  # not run again from zero

    def test_a_pick_up_leaves_ready_failed_and_whole_documents_alone(
        self,
        storage: LocalFileStorage,
        monkeypatch: pytest.MonkeyPatch,
        job_scheduler: tuple[JobDocumentIndexScheduler, list[str]],
    ) -> None:
        scheduler, turns = job_scheduler
        upload(
            file_storage=storage,
            owner_id=OWNER,
            persona_id=PERSONA,
            conversation_id=CONV,
            file_bytes=b"a short note",
            filename="note.txt",
        )
        failed = _retrieval_doc(storage, monkeypatch, "failed.txt")
        key = document_service.document_original_key(
            failed, owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV
        )
        assert key is not None
        document_service.write_index_progress(
            storage, key, document_service.IndexProgress(state="failed", chunks_indexed=0)
        )
        asyncio.run(
            scheduler.pick_up(
                owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV, refs=_listed(storage)
            )
        )
        assert scheduler._backlog.count(OWNER) == 0
        assert turns == []

    def test_a_pick_up_of_one_conversation_is_throttled(
        self,
        storage: LocalFileStorage,
        monkeypatch: pytest.MonkeyPatch,
        job_scheduler: tuple[JobDocumentIndexScheduler, list[str]],
    ) -> None:
        scheduler, turns = job_scheduler
        _retrieval_doc(storage, monkeypatch)
        now = [1000.0]
        monkeypatch.setattr(document_index.time, "monotonic", lambda: now[0])

        def _pick(conv: str = CONV) -> None:
            asyncio.run(
                scheduler.pick_up(
                    owner_id=OWNER, persona_id=PERSONA, conversation_id=conv, refs=_listed(storage)
                )
            )

        _pick()
        _pick()
        now[0] += document_index.PICK_UP_INTERVAL_SECONDS - 1
        _pick()
        assert turns == [OWNER]
        _pick("another-conversation")  # per conversation
        assert turns == [OWNER, OWNER]
        now[0] += 1
        _pick()
        assert turns == [OWNER, OWNER, OWNER]


def _owner_doc(
    storage: LocalFileStorage, monkeypatch: pytest.MonkeyPatch, owner: str, name: str
) -> document_service.DocumentRef:
    monkeypatch.setenv("PERSONA_DOC_INJECT_THRESHOLD", "100")
    return upload(
        file_storage=storage,
        owner_id=owner,
        persona_id=PERSONA,
        conversation_id=CONV,
        file_bytes=_long_text(),
        filename=name,
    )


class TestTheLocalQueue:
    def test_owners_take_turns_slice_by_slice_and_everything_finishes(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One owner's three documents do not go before another owner's one."""
        a = [_owner_doc(storage, monkeypatch, "ann", f"a{n}.txt") for n in range(3)]
        b = _owner_doc(storage, monkeypatch, "bob", "b.txt")
        order: list[str] = []
        real = document_index.run_slice

        def _spy(**kwargs: Any) -> Any:  # noqa: ANN401
            order.append(kwargs["owner_id"])
            return real(**kwargs)

        monkeypatch.setattr(document_index, "run_slice", _spy)

        async def _go() -> None:
            queue = LocalDocumentIndexQueue(_deps(storage, backend))
            queue.start()
            for ref in a:
                await queue.schedule(
                    owner_id="ann", persona_id=PERSONA, conversation_id=CONV, doc_ref=ref.doc_ref
                )
            await queue.schedule(
                owner_id="bob", persona_id=PERSONA, conversation_id=CONV, doc_ref=b.doc_ref
            )
            await asyncio.wait_for(queue.join(), timeout=120)
            await queue.aclose()

        asyncio.run(_go())
        assert order[:6] == ["ann", "bob", "ann", "bob", "ann", "bob"]
        for owner in ("ann", "bob"):
            listed = document_service.list_for_conversation(
                file_storage=storage, owner_id=owner, persona_id=PERSONA, conversation_id=CONV
            )
            assert {r.index_state for r in listed} == {"ready"}

    def test_the_least_served_owner_goes_first(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fifth review F3: a busy owner queued first still goes after a new one."""
        busy = _owner_doc(storage, monkeypatch, "ann", "a.txt")
        new = _owner_doc(storage, monkeypatch, "bob", "b.txt")
        quota = InMemoryIndexQuota()
        quota.add("ann", 640)
        order: list[str] = []
        real = document_index.run_slice

        def _spy(**kwargs: Any) -> Any:  # noqa: ANN401
            order.append(kwargs["owner_id"])
            return real(**kwargs)

        monkeypatch.setattr(document_index, "run_slice", _spy)

        async def _go() -> None:
            queue = LocalDocumentIndexQueue(_deps(storage, backend, quota=quota))
            await queue.schedule(
                owner_id="ann", persona_id=PERSONA, conversation_id=CONV, doc_ref=busy.doc_ref
            )
            await queue.schedule(
                owner_id="bob", persona_id=PERSONA, conversation_id=CONV, doc_ref=new.doc_ref
            )
            queue.start()
            await asyncio.wait_for(queue.join(), timeout=120)
            await queue.aclose()

        asyncio.run(_go())
        first_ann = order.index("ann")
        assert first_ann > 0
        assert set(order[:first_ann]) == {"bob"}  # bob is served until he is no longer least

    def test_an_owner_over_its_quota_is_parked_and_never_holds_the_lane(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        a = [_owner_doc(storage, monkeypatch, "ann", f"a{n}.txt") for n in range(5)]
        b = _owner_doc(storage, monkeypatch, "bob", "b.txt")
        quota = InMemoryIndexQuota()
        quota.add("ann", OWNER_CHUNKS_PER_HOUR)
        turns: list[str] = []
        real_turn = document_index.run_owner_turn

        async def _spy(deps: Any, owner_id: str, *args: Any, **kwargs: Any) -> Any:  # noqa: ANN401
            turns.append(owner_id)
            return await real_turn(deps, owner_id, *args, **kwargs)

        monkeypatch.setattr(document_index, "run_owner_turn", _spy)

        async def _go() -> None:
            queue = LocalDocumentIndexQueue(_deps(storage, backend, quota=quota))
            queue.start()
            for ref in a:
                await queue.schedule(
                    owner_id="ann", persona_id=PERSONA, conversation_id=CONV, doc_ref=ref.doc_ref
                )
            await queue.schedule(
                owner_id="bob", persona_id=PERSONA, conversation_id=CONV, doc_ref=b.doc_ref
            )
            await asyncio.wait_for(queue.join(), timeout=120)
            assert queue._parked == {"ann"}
            await queue.aclose()

        asyncio.run(_go())
        assert turns.count("ann") == 1  # one look, then parked until its reset
        assert all(f"::{b.doc_ref}::" in k for k in backend.rows)
        bob = document_service.list_for_conversation(
            file_storage=storage, owner_id="bob", persona_id=PERSONA, conversation_id=CONV
        )
        assert bob[0].index_state == "ready"

    def test_a_document_cut_short_by_a_restart_resumes_then_fails_after_three(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend)
        _slice(deps, ref, ChunkCursor())  # then the process "restarted"
        resumed: list[str] = []

        async def _pick_up_once() -> None:
            queue = LocalDocumentIndexQueue(_deps(storage, backend))
            queue._rejoin = resumed.append  # type: ignore[method-assign]
            await queue.pick_up(
                owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV, refs=_listed(storage)
            )

        for _ in range(3):
            asyncio.run(_pick_up_once())
        assert resumed == [OWNER, OWNER]
        assert _listed(storage)[0].index_state == "failed"

    def test_a_failing_turn_is_retried_then_the_document_fails_and_the_owner_goes_on(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        bad = _retrieval_doc(storage, monkeypatch, "bad.txt")
        good = _retrieval_doc(storage, monkeypatch, "good.txt")
        real_write = backend.upsert

        def _upsert(*, persona_id: str, store_kind: str, chunks: list[Any]) -> None:
            if any(f"::{bad.doc_ref}::" in c.id for c in chunks):
                raise RuntimeError("this one never embeds")
            real_write(persona_id=persona_id, store_kind=store_kind, chunks=chunks)

        monkeypatch.setattr(backend, "upsert", _upsert)
        from persona.jobs import RetryPolicy

        monkeypatch.setattr(
            document_index,
            "_RETRY",
            RetryPolicy(max_attempts=4, base_backoff_seconds=0.01, max_backoff_seconds=0.01),
        )

        async def _go() -> None:
            queue = LocalDocumentIndexQueue(_deps(storage, backend))
            queue.start()
            for ref in (bad, good):
                await queue.schedule(
                    owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV, doc_ref=ref.doc_ref
                )
            for _ in range(200):
                await asyncio.sleep(0.05)
                await queue.join()
                if not queue._parked and not queue._rotation:
                    break
            await queue.aclose()

        asyncio.run(_go())
        states = {r.doc_ref: r.index_state for r in _listed(storage)}
        assert states == {bad.doc_ref: "failed", good.doc_ref: "ready"}

    def test_the_local_queue_caps_the_backlog_too(
        self, storage: LocalFileStorage, backend: _Backend
    ) -> None:
        queue = LocalDocumentIndexQueue(_deps(storage, backend))
        for n in range(MAX_PENDING_DOCUMENTS_PER_OWNER):
            queue._backlog.add(OWNER, persona_id=PERSONA, conversation_id=CONV, doc_ref=f"d{n}")
        with pytest.raises(DocumentIndexBacklogFullError):
            queue.admit(OWNER)

    def test_the_local_pick_up_respects_the_cap(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        queue = LocalDocumentIndexQueue(_deps(storage, backend))
        for n in range(MAX_PENDING_DOCUMENTS_PER_OWNER):
            queue._backlog.add(OWNER, persona_id=PERSONA, conversation_id="other", doc_ref=f"d{n}")
        asyncio.run(
            queue.pick_up(
                owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV, refs=_listed(storage)
            )
        )
        assert queue._backlog.count(OWNER) == MAX_PENDING_DOCUMENTS_PER_OWNER
        assert not queue._backlog.has(OWNER, conversation_id=CONV, doc_ref=ref.doc_ref)


# ----- the indexer thread ---------------------------------------------------------------


class TestASliceHasAWallClockBound:
    """Fourth review finding 2: a hung slice cannot hold the index lane."""

    def test_a_slice_past_its_bound_is_abandoned_and_the_next_gets_a_fresh_thread(self) -> None:
        indexer = DocumentIndexer()
        release = threading.Event()
        threads: list[int] = []

        def _hang() -> str:
            threads.append(threading.get_ident())
            release.wait(30)
            return "late"

        def _quick() -> str:
            threads.append(threading.get_ident())
            return "done"

        async def _go() -> str:
            with pytest.raises(document_index.DocumentIndexSliceTimeoutError):
                await indexer.run(_hang, wall_seconds=0.2)
            assert indexer.abandoned_threads == 1
            return await asyncio.wait_for(indexer.run(_quick, wall_seconds=5), timeout=5)

        try:
            assert asyncio.run(_go()) == "done"
            assert threads[0] != threads[1]  # not queued behind the stuck one
        finally:
            release.set()
            indexer.close()
        for _ in range(100):
            if indexer.abandoned_threads == 0:
                break
            threading.Event().wait(0.05)
        assert indexer.abandoned_threads == 0  # it returned at last, and is forgotten

    def test_with_too_many_stuck_threads_indexing_stops_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(document_index, "MAX_ABANDONED_THREADS", 2)
        critical: list[str] = []
        monkeypatch.setattr(
            document_index._log, "critical", lambda msg, *a: critical.append(msg.format(*a))
        )
        indexer = DocumentIndexer()
        release = threading.Event()

        async def _go() -> None:
            for _ in range(2):
                with pytest.raises(document_index.DocumentIndexSliceTimeoutError):
                    await indexer.run(lambda: release.wait(30), wall_seconds=0.1)
            with pytest.raises(document_index.DocumentIndexerExhaustedError):
                await indexer.run(lambda: "never", wall_seconds=5)

        try:
            asyncio.run(_go())
        finally:
            release.set()
            indexer.close()
        assert critical
        assert "2 stuck threads" in critical[0]

    def test_a_slice_still_running_is_logged_as_holding_the_lane(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(document_index, "LANE_WARN_SECONDS", 0.05)
        errors: list[str] = []
        monkeypatch.setattr(
            document_index._log, "error", lambda msg, *a: errors.append(msg.format(*a))
        )
        indexer = DocumentIndexer()

        async def _go() -> None:
            await indexer.run(lambda: threading.Event().wait(0.3), wall_seconds=5, label="L1")
            await indexer.run(lambda: None, wall_seconds=5, label="L2")

        try:
            asyncio.run(_go())
        finally:
            indexer.close()
        assert len(errors) == 1
        assert "held the index lane" in errors[0]
        assert "L1" in errors[0]

    def test_an_abandoned_slice_writes_no_more_chunks_and_no_progress(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        stuck, release = threading.Event(), threading.Event()

        def _first_write_hangs(count: int) -> None:
            if count == 1:
                stuck.set()
                release.wait(30)

        backend.on_write = _first_write_hangs
        monkeypatch.setattr(document_index, "SLICE_WALL_SECONDS", 0.3)
        deps = _deps(storage, backend)
        _wait(deps, ref)
        with pytest.raises(document_index.DocumentIndexSliceTimeoutError):
            _run(_handler(deps, _Queue()), DocumentIndexJobPayload(), _Context(attempt=1))
        release.set()
        for _ in range(100):
            if deps.indexer.abandoned_threads == 0:
                break
            threading.Event().wait(0.05)
        deps.indexer.close()
        assert backend.writes == [EMBED_BATCH]  # the batch already inside the call, no more
        assert _listed(storage)[0].index_state == "pending"  # no progress written over
        assert deps.backlog.has(OWNER, conversation_id=CONV, doc_ref=ref.doc_ref)


def _exhausted_indexer() -> DocumentIndexer:
    """An indexer with as many stuck threads as it tolerates (calls that never return)."""
    from concurrent.futures import Future

    indexer = DocumentIndexer()
    for _ in range(document_index.MAX_ABANDONED_THREADS):
        indexer._stuck.add(Future())
    return indexer


class _GatedStorage(LocalFileStorage):
    """Holds the indexer thread inside one read, once, until released."""

    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.gate: str | None = None
        self.entered = threading.Event()
        self.release = threading.Event()

    def get(self, key: str) -> Any:  # noqa: ANN401
        gate = self.gate
        if (
            gate is not None
            and key.endswith(gate)
            and threading.current_thread().name.startswith("document-indexer")
        ):
            self.gate = None
            self.entered.set()
            self.release.wait(30)
        return super().get(key)


class TestAnAbandonedSliceNeverWrites:
    """Fifth review F2: nothing an abandoned slice does lands after its retry's writes."""

    @pytest.mark.parametrize("gate", [".text.json", ".index.json"])
    def test_the_retrys_chunks_survive_and_its_progress_never_goes_back(
        self, tmp_path: Path, backend: _Backend, monkeypatch: pytest.MonkeyPatch, gate: str
    ) -> None:
        """Stuck reading the text (before any write) or the progress (after its writes)."""
        storage = _GatedStorage(tmp_path)
        ref = _retrieval_doc(storage, monkeypatch)
        monkeypatch.setattr(document_index, "SLICE_WALL_SECONDS", 0.5)
        deps = _deps(storage, backend)
        _wait(deps, ref)
        storage.gate = gate
        with pytest.raises(document_index.DocumentIndexSliceTimeoutError):
            _run(_handler(deps, _Queue()), DocumentIndexJobPayload(), _Context(attempt=1))
        assert storage.entered.is_set()
        monkeypatch.setattr(document_index, "SLICE_WALL_SECONDS", 30.0)
        for _ in range(2):  # the retry, and the turn after it
            _run(_handler(deps, _Queue()), DocumentIndexJobPayload(), _Context(attempt=2))
        retry_rows = set(backend.rows)
        assert len(retry_rows) == 2 * SLICE_CHUNKS
        (listed,) = _listed(storage)
        assert (listed.index_state, listed.chunks_indexed) == ("indexing", 2 * SLICE_CHUNKS)
        storage.release.set()
        for _ in range(100):
            if deps.indexer.abandoned_threads == 0:
                break
            threading.Event().wait(0.05)
        deps.indexer.close()
        assert deps.indexer.abandoned_threads == 0
        assert set(backend.rows) == retry_rows  # nothing deleted, nothing added
        (listed,) = _listed(storage)
        assert (listed.index_state, listed.chunks_indexed) == ("indexing", 2 * SLICE_CHUNKS)

    def test_a_write_running_when_abandoned_lands_before_any_later_write(self) -> None:
        fence = document_index._fence_for("o", "p", "c", "d")
        order: list[str] = []
        inside, release = threading.Event(), threading.Event()
        abandoned = threading.Event()

        def _stale() -> bool:
            document_index._slice_abandoned.set(abandoned)

            def _write() -> None:
                inside.set()
                release.wait(30)
                order.append("stale write")

            first = document_index._fenced(fence, _write)
            later = document_index._fenced(fence, lambda: order.append("stale late"))
            return first and not later

        result: list[bool] = []
        stale = threading.Thread(target=lambda: result.append(_stale()))
        stale.start()
        assert inside.wait(5)
        abandoned.set()  # the caller gives up while the write is inside its call
        retry = threading.Thread(
            target=lambda: document_index._fenced(fence, lambda: order.append("retry"))
        )
        retry.start()
        threading.Event().wait(0.2)
        assert order == []  # the retry waits for the fence
        release.set()
        stale.join(5)
        retry.join(5)
        assert order == ["stale write", "retry"]
        assert result == [True]

    def test_a_fence_held_too_long_fails_the_write(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(document_index, "FENCE_WAIT_SECONDS", 0.1)
        fence = document_index._fence_for("o", "p", "c", "held")
        fence.acquire()
        try:
            with pytest.raises(document_index.DocumentIndexWriteBlockedError):
                document_index._fenced(fence, lambda: None)
        finally:
            fence.release()


class TestAStuckThreadNeverBlocksExit:
    """Fifth review F5: the indexer's threads are daemon threads."""

    def test_the_indexer_thread_is_a_daemon(self) -> None:
        indexer = DocumentIndexer()
        try:
            assert asyncio.run(indexer.run(lambda: threading.current_thread().daemon))
        finally:
            indexer.close()

    def test_a_process_with_an_abandoned_slice_exits(self) -> None:
        import subprocess

        script = (
            "import asyncio, threading\n"
            "import persona_api.services.document_index as di\n"
            "async def main():\n"
            "    ind = di.DocumentIndexer()\n"
            "    try:\n"
            "        await ind.run(threading.Event().wait, wall_seconds=0.5)\n"
            "    except di.DocumentIndexSliceTimeoutError:\n"
            "        print('abandoned', ind.abandoned_threads, flush=True)\n"
            "    ind.close()\n"
            "asyncio.run(main())\n"
        )
        done = subprocess.run(  # noqa: S603 - this interpreter, a fixed script
            [sys.executable, "-c", script], capture_output=True, text=True, timeout=60, check=False
        )
        assert done.returncode == 0, done.stderr[-2000:]
        assert "abandoned 1" in done.stdout


class TestTheIndexerPausesWhenItsThreadsAreStuck:
    """Fifth review F1: an exhausted indexer is a lane pause, never a document's failure."""

    @pytest.mark.parametrize("attempt", [1, 4])
    def test_the_turn_is_deferred_at_the_same_attempt_and_nothing_changes(
        self,
        storage: LocalFileStorage,
        backend: _Backend,
        monkeypatch: pytest.MonkeyPatch,
        attempt: int,
    ) -> None:
        from persona.errors import JobDeferredError

        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend, indexer=_exhausted_indexer())
        _wait(deps, ref)
        queue = _Queue()
        with pytest.raises(JobDeferredError) as caught:
            _run(_handler(deps, queue), DocumentIndexJobPayload(), _Context(attempt=attempt))
        assert caught.value.delay_seconds == document_index.INDEXER_PAUSE_SECONDS
        assert queue.turns == []  # the same job comes back; no second one
        assert _listed(storage)[0].index_state == "pending"  # not marked, even on the last
        assert deps.backlog.has(OWNER, conversation_id=CONV, doc_ref=ref.doc_ref)
        assert backend.writes == []

    def test_it_is_checked_before_the_last_attempt_marks_anything(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend, indexer=_exhausted_indexer())
        _wait(deps, ref)
        marked: list[str] = []
        monkeypatch.setattr(
            document_index, "_mark_failed", lambda _d, entry, _o: marked.append(entry.doc_ref)
        )
        with pytest.raises(document_index.DocumentIndexerExhaustedError):
            asyncio.run(document_index.run_owner_turn(deps, OWNER, None, last_attempt=True))
        assert marked == []
        assert deps.backlog.count(OWNER) == 1

    def test_the_local_queue_parks_the_owner_without_counting_an_attempt(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        deps = _deps(storage, backend, indexer=_exhausted_indexer())

        async def _go() -> LocalDocumentIndexQueue:
            queue = LocalDocumentIndexQueue(deps)
            queue.start()
            await queue.schedule(
                owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV, doc_ref=ref.doc_ref
            )
            await asyncio.wait_for(queue.join(), timeout=30)
            assert OWNER in queue._parked
            assert queue._attempts == {}
            await queue.aclose()
            return queue

        asyncio.run(_go())
        assert _listed(storage)[0].index_state == "pending"
        assert deps.backlog.count(OWNER) == 1

    def test_a_returned_thread_lets_indexing_resume(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        indexer = _exhausted_indexer()
        deps = _deps(storage, backend, indexer=indexer)
        _wait(deps, ref)
        assert indexer.exhausted
        stuck = next(iter(indexer._stuck))
        indexer._stuck.discard(stuck)  # what the done-callback does when the call returns
        assert not indexer.exhausted
        _run(_handler(deps, _Queue()), DocumentIndexJobPayload(), _Context(attempt=4))
        assert backend.writes
        assert _listed(storage)[0].index_state == "indexing"


class TestTheEmbedderIsWarmedAtNormalPriority:
    """Fourth review finding 5: helper threads are not created at the indexer's niceness."""

    def test_the_warm_up_runs_once_before_the_first_slice_on_another_thread(self) -> None:
        indexer = DocumentIndexer()
        seen: list[tuple[str, str]] = []

        def _warm() -> None:
            seen.append(("warm", threading.current_thread().name))

        def _slice() -> str:
            seen.append(("slice", threading.current_thread().name))
            return threading.current_thread().name

        indexer.warm_up_with(_warm)

        async def _go() -> None:
            await indexer.run(_slice)
            await indexer.run(_slice)

        try:
            asyncio.run(_go())
        finally:
            indexer.close()
        assert [kind for kind, _ in seen] == ["warm", "slice", "slice"]
        assert seen[0][1] == "document-indexer-warm-up"  # not the indexer thread
        assert seen[1][1] == "document-indexer_0"

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="per-thread niceness")
    def test_the_warm_up_thread_is_not_niced(self) -> None:
        indexer = DocumentIndexer()
        niceness: list[int] = []
        indexer.warm_up_with(
            lambda: niceness.append(os.getpriority(os.PRIO_PROCESS, threading.get_native_id()))
        )
        try:
            asyncio.run(indexer.run(lambda: None))
        finally:
            indexer.close()
        assert niceness == [os.getpriority(os.PRIO_PROCESS, 0)]
        assert niceness[0] < document_index.INDEXER_NICENESS

    def test_a_warm_up_that_hangs_counts_as_a_stuck_thread_and_the_slice_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Fifth review F6: the warm-up has a time bound."""
        monkeypatch.setattr(document_index, "WARM_UP_WALL_SECONDS", 0.2)
        indexer = DocumentIndexer()
        release = threading.Event()
        warms: list[int] = []
        indexer.warm_up_with(lambda: (warms.append(1), release.wait(30)))

        async def _go() -> tuple[str, str, int]:
            first = await indexer.run(lambda: "slice", wall_seconds=5)
            stuck = indexer.abandoned_threads
            second = await indexer.run(lambda: "again", wall_seconds=5)
            return first, second, stuck

        try:
            assert asyncio.run(_go()) == ("slice", "again", 1)
            assert warms == [1]  # never tried again
        finally:
            release.set()
            indexer.close()
        for _ in range(100):
            if indexer.abandoned_threads == 0:
                break
            threading.Event().wait(0.05)
        assert indexer.abandoned_threads == 0

    def test_a_warm_up_that_fails_is_logged_and_the_slice_still_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        warnings: list[str] = []
        monkeypatch.setattr(
            document_index._log, "warning", lambda msg, *a: warnings.append(msg.format(*a))
        )
        indexer = DocumentIndexer()

        def _boom() -> None:
            raise RuntimeError("no model here")

        indexer.warm_up_with(_boom)
        try:
            assert asyncio.run(indexer.run(lambda: 7)) == 7
            assert asyncio.run(indexer.run(lambda: 8)) == 8
        finally:
            indexer.close()
        assert len(warnings) == 1
        assert "no model here" in warnings[0]

    def test_the_app_warms_its_own_embedder(self) -> None:
        from types import SimpleNamespace

        from persona_api.app import _warm_embedder

        encoded: list[list[str]] = []
        app = SimpleNamespace(
            state=SimpleNamespace(embedder=SimpleNamespace(encode=encoded.append))
        )
        _warm_embedder(app)  # type: ignore[arg-type]
        assert encoded == [["warm up"]]
        _warm_embedder(SimpleNamespace(state=SimpleNamespace()))  # type: ignore[arg-type]


class TestTheDutyCycle:
    """Fourth review finding 4: the index's average CPU is capped, not only niced."""

    def test_the_default_is_a_quarter_of_a_core_and_it_is_settable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(document_index.INDEX_DUTY_CYCLE_ENV_VAR, raising=False)
        assert document_index.index_duty_cycle() == 0.25
        monkeypatch.setenv(document_index.INDEX_DUTY_CYCLE_ENV_VAR, "0.05")
        assert document_index.index_duty_cycle() == 0.05
        monkeypatch.setenv(document_index.INDEX_DUTY_CYCLE_ENV_VAR, "1")
        assert document_index.index_duty_cycle() == 1.0

    @pytest.mark.parametrize("raw", ["0", "0.01", "1.5", "nan", "half", "-0.3"])
    def test_an_unusable_setting_falls_back(
        self, raw: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(document_index.INDEX_DUTY_CYCLE_ENV_VAR, raw)
        assert document_index.index_duty_cycle() == 0.25

    @pytest.mark.parametrize(
        ("cpu", "duty", "pause"),
        [(1.0, 0.25, 3.0), (0.5, 0.5, 0.5), (2.0, 1.0, None), (0.0, 0.25, None)],
    )
    def test_the_pause_makes_the_work_that_share_of_the_time(
        self, cpu: float, duty: float, pause: float | None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        slept: list[float] = []
        monkeypatch.setattr(document_index.time, "sleep", slept.append)
        document_index._pace(cpu, duty)
        assert slept == ([] if pause is None else [pytest.approx(pause)])

    def test_each_batch_of_a_slice_is_paced_by_its_own_cpu(
        self, storage: LocalFileStorage, backend: _Backend, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        ref = _retrieval_doc(storage, monkeypatch)
        monkeypatch.setenv(document_index.INDEX_DUTY_CYCLE_ENV_VAR, "0.5")
        clock = [100.0]

        def _write(_: int) -> None:
            clock[0] += 0.2  # each batch costs 0.2 s of CPU

        backend.on_write = _write
        monkeypatch.setattr(document_index.time, "thread_time", lambda: clock[0])
        paced: list[tuple[float, float]] = []
        monkeypatch.setattr(document_index, "_pace", lambda cpu, duty: paced.append((cpu, duty)))
        _slice(_deps(storage, backend), ref, ChunkCursor())
        assert len(paced) == SLICE_CHUNKS // EMBED_BATCH
        assert all(cpu == pytest.approx(0.2) and duty == 0.5 for cpu, duty in paced)

    def test_an_abandoned_slice_stops_pausing(self) -> None:
        abandoned = threading.Event()
        abandoned.set()
        token = document_index._slice_abandoned.set(abandoned)
        try:
            started = time.monotonic()
            document_index._pace(100.0, 0.02)  # would be hours
            assert time.monotonic() - started < 1.0
        finally:
            document_index._slice_abandoned.reset(token)

    def test_the_slice_bound_stretches_below_a_quarter_core(self) -> None:
        bound = document_index.SLICE_WALL_SECONDS
        assert document_index.slice_wall_seconds(1.0) == bound
        assert document_index.slice_wall_seconds(0.25) == bound
        assert document_index.slice_wall_seconds(0.05) == pytest.approx(bound * 5)


class TestTheIndexerThread:
    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="niceness is Linux-only")
    def test_it_runs_at_niceness_nineteen_and_the_caller_does_not(self) -> None:
        indexer = DocumentIndexer()

        def _mine() -> int:
            return os.getpriority(os.PRIO_PROCESS, threading.get_native_id())

        try:
            inside = asyncio.run(indexer.run(_mine))
        finally:
            indexer.close()
        assert inside == document_index.INDEXER_NICENESS
        assert os.getpriority(os.PRIO_PROCESS, threading.get_native_id()) < inside

    def test_its_torch_runs_on_one_thread_and_no_other_threads_change(self) -> None:
        torch = pytest.importorskip("torch")
        # torch's own default, as a thread that has set nothing reads it, before the indexer
        # runs. It is not ``os.cpu_count()``: torch counts physical cores (2 on a 4-vCPU CI
        # runner), and OMP_NUM_THREADS overrides it.
        default: list[int] = []
        first = threading.Thread(target=lambda: default.append(torch.get_num_threads()))
        first.start()
        first.join()
        if default[0] == 1:
            pytest.skip("torch defaults to one thread here: a limit to one would not show")
        # an index that changed it for the whole process (even from its own thread, before
        # this thread first asked) would show here
        before = torch.get_num_threads()
        assert before == default[0]
        indexer = DocumentIndexer()
        try:
            inside = asyncio.run(indexer.run(torch.get_num_threads))
        finally:
            indexer.close()
        later: list[int] = []

        def _fresh() -> None:
            torch.ones(256, 256) @ torch.ones(256, 256)  # its first parallel region sets it up
            later.append(torch.get_num_threads())

        fresh = threading.Thread(target=_fresh)
        fresh.start()
        fresh.join()
        assert inside == 1
        assert torch.get_num_threads() == before
        assert later == [before]

    def test_it_runs_in_the_callers_owner_scope(self) -> None:
        from persona_api.middleware.rls_context import current_user_id

        indexer = DocumentIndexer()

        async def _go() -> str | None:
            token = current_user_id.set("owner-7")
            try:
                return await indexer.run(current_user_id.get)
            finally:
                current_user_id.reset(token)

        try:
            assert asyncio.run(_go()) == "owner-7"
        finally:
            indexer.close()


# ----- condition (c): one shared embedder, both paths at once ----------------------------


@pytest.fixture(scope="module")
def tiny_embedder(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """A real SentenceTransformer, BERT-shaped and tiny, built locally (no download)."""
    pytest.importorskip("sentence_transformers")
    import string

    from persona.stores.embedder import SentenceTransformerEmbedder
    from sentence_transformers import SentenceTransformer, models
    from transformers import BertConfig, BertModel, BertTokenizerFast

    out = tmp_path_factory.mktemp("tiny-bert")
    vocab = ["[PAD]", "[UNK]", "[CLS]", "[SEP]", "[MASK]"]
    chars = string.ascii_lowercase + string.digits + string.punctuation
    vocab += list(chars) + ["##" + c for c in chars]
    (out / "vocab.txt").write_text("\n".join(vocab))
    tokenizer = BertTokenizerFast(vocab_file=str(out / "vocab.txt"), do_lower_case=True)
    config = BertConfig(
        vocab_size=len(vocab),
        hidden_size=64,
        num_hidden_layers=2,
        num_attention_heads=2,
        intermediate_size=128,
        max_position_embeddings=256,
    )
    BertModel(config).save_pretrained(out / "hf")
    tokenizer.save_pretrained(out / "hf")
    word = models.Transformer(str(out / "hf"), max_seq_length=256)
    pooling = models.Pooling(word.get_word_embedding_dimension(), pooling_mode="cls")
    SentenceTransformer(modules=[word, pooling, models.Normalize()], device="cpu").save(
        str(out / "model")
    )
    return SentenceTransformerEmbedder(model_name=str(out / "model"), device="cpu")


class TestOneEmbedderServesChatAndTheIndexAtOnce:
    """Condition (c): the shared embedder stays correct while both paths encode together."""

    def test_concurrent_encodes_raise_nothing_and_agree(self, tiny_embedder: Any) -> None:
        texts = [f"document passage number {i} " * 12 for i in range(4)]
        query = "what does the lease say about the deposit"
        reference_batch = tiny_embedder.encode(texts)
        reference_query = tiny_embedder.encode([query])[0]
        indexer = DocumentIndexer()

        async def _both() -> tuple[list[Any], list[Any]]:
            index_side = [indexer.run(lambda: tiny_embedder.encode(texts)) for _ in range(25)]
            chat_side = [asyncio.to_thread(tiny_embedder.encode, [query]) for _ in range(25)]
            results = await asyncio.gather(*index_side, *chat_side)
            return list(results[:25]), list(results[25:])

        try:
            batches, queries = asyncio.run(_both())
        finally:
            indexer.close()
        for batch in batches:
            assert len(batch) == len(texts)
            for vector, expected in zip(batch, reference_batch, strict=True):
                assert vector == pytest.approx(expected, abs=1e-5)
        for (vector,) in queries:
            assert vector == pytest.approx(reference_query, abs=1e-5)
