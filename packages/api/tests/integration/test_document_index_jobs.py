"""The document index on the real A0 jobs system (D-FV0-31), against Postgres.

- The serial-type claim: one ``document_ingest`` in flight at a time, other types unhindered.
- One job per OWNER (fourth review finding 1): however many documents wait, an owner has
  one turn queued; owners take turns; an owner over its quota is one parked job.
- The backlog under RLS.
- The A0 forced-kill re-run: a worker dies after a slice wrote its chunks and before the
  job completed; the lease lapses, the job is reclaimed and re-run by the real worker, and
  the store holds no duplicate.
- The per-owner quota in ``rate_limit_buckets``, and when it resets.
- Migrations ``059_jobs_inflight_by_type`` and ``060_document_index_backlog``.
"""

# ruff: noqa: ANN401, ARG001, ARG002, PLC0415, SLF001 - fixtures, fakes and private seams.
from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from alembic import command
from alembic.config import Config
from persona.jobs import JobRegistry
from persona.stores.document_store import DocumentStore
from persona_api.jobs import JobQueue, Worker
from persona_api.services import document_service
from persona_api.services.document_index import (
    DOCUMENT_INDEX_JOB_TYPE,
    OWNER_CHUNKS_PER_HOUR,
    SLICE_CHUNKS,
    DocumentIndexDeps,
    DocumentIndexer,
    PostgresIndexBacklog,
    PostgresIndexQuota,
    enqueue_document_index,
    enqueue_owner_turn,
    register_document_index_handler,
)
from persona_api.storage import LocalFileStorage
from sqlalchemy import create_engine, text

if TYPE_CHECKING:
    from collections.abc import Iterator

    from persona.schema.chunks import PersonaChunk
    from sqlalchemy.engine import Engine

pytestmark = pytest.mark.integration

OWNER, PERSONA, CONV = "user_a", "astrid", "conv1"


@pytest.fixture
def app_engine(migrated_engine: Engine) -> Engine:
    app_url = os.environ.get("APP_DATABASE_URL")
    if not app_url:
        pytest.skip("APP_DATABASE_URL not set")
    return create_engine(app_url.replace("+asyncpg", "+psycopg"))


@pytest.fixture
def seeded(migrated_engine: Engine) -> Engine:
    with migrated_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO users (id, email) VALUES "
                "('user_a','a@example.com'),('user_b','b@example.com')"
            )
        )
    return migrated_engine


# ----- the serial claim ---------------------------------------------------------------


def _enqueue(queue: JobQueue, owner: str, job_type: str, key: str) -> None:
    queue.enqueue(type=job_type, owner_id=owner, payload={}, idempotency_key=key)


def test_one_serial_job_is_in_flight_at_a_time_and_other_types_still_run(seeded: Engine) -> None:
    queue = JobQueue(seeded)
    _enqueue(queue, "user_a", DOCUMENT_INDEX_JOB_TYPE, "a1")
    _enqueue(queue, "user_b", DOCUMENT_INDEX_JOB_TYPE, "b1")
    _enqueue(queue, "user_b", "other", "o1")
    serial = [DOCUMENT_INDEX_JOB_TYPE]
    claimed = []
    while batch := queue.claim(worker_id="w", lease_seconds=60, limit=1, serial_types=serial):
        claimed += batch
    assert sorted(c.type for c in claimed) == [DOCUMENT_INDEX_JOB_TYPE, "other"]
    first = next(c for c in claimed if c.type == DOCUMENT_INDEX_JOB_TYPE)
    assert queue.mark_running(job_id=first.id, worker_id="w")
    assert queue.claim(worker_id="w", lease_seconds=60, serial_types=serial) == []
    assert queue.complete(job_id=first.id, worker_id="w")
    (second,) = queue.claim(worker_id="w", lease_seconds=60, serial_types=serial)
    assert second.type == DOCUMENT_INDEX_JOB_TYPE
    assert second.owner_id != first.owner_id


def test_without_serial_types_the_claim_is_as_before(seeded: Engine) -> None:
    queue = JobQueue(seeded)
    _enqueue(queue, "user_a", DOCUMENT_INDEX_JOB_TYPE, "a1")
    _enqueue(queue, "user_b", DOCUMENT_INDEX_JOB_TYPE, "b1")
    assert len(queue.claim(worker_id="w", lease_seconds=60, limit=5)) == 2


def test_the_claim_uses_the_in_flight_by_type_index(seeded: Engine) -> None:
    with seeded.begin() as conn:
        definition = conn.execute(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_jobs_inflight_by_type'")
        ).scalar_one()
    assert "(type)" in definition
    assert "claimed" in definition
    assert "running" in definition


# ----- the forced-kill re-run ---------------------------------------------------------


class _Backend:
    def __init__(self) -> None:
        self.rows: dict[str, PersonaChunk] = {}
        self.upserts = 0

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        self.upserts += len(chunks)
        for chunk in chunks:
            self.rows[chunk.id] = chunk

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return list(self.rows.values())

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        for chunk_id in ids:
            self.rows.pop(chunk_id, None)

    def query(self, **_: Any) -> list[PersonaChunk]:
        return []

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.rows.clear()


def _document(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[LocalFileStorage, str]:
    from _document_upload import upload

    monkeypatch.setenv("PERSONA_DOC_INJECT_THRESHOLD", "100")
    storage = LocalFileStorage(tmp_path / "workspace")
    (tmp_path / "workspace").mkdir()
    body = "\n\n".join(f"Paragraph {p}. " + "word " * 120 for p in range(90)).encode()
    ref = upload(
        file_storage=storage,
        owner_id=OWNER,
        persona_id=PERSONA,
        conversation_id=CONV,
        file_bytes=body,
        filename="report.txt",
    )
    return storage, ref.doc_ref


def _jobs(engine: Engine) -> list[tuple[str, str, int]]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT idempotency_key, state, attempt FROM jobs "
                "WHERE type = 'document_ingest' ORDER BY created_at"
            )
        ).all()
    return [(r[0], r[1], r[2]) for r in rows]


def _registry(storage: LocalFileStorage, backend: _Backend, app_engine: Engine) -> Any:
    registry = JobRegistry()
    indexer = DocumentIndexer()
    register_document_index_handler(
        registry,
        deps=DocumentIndexDeps(
            file_storage=storage,
            document_store=lambda: DocumentStore(backend=backend),  # type: ignore[arg-type]
            indexer=indexer,
            quota=PostgresIndexQuota(app_engine),
            backlog=PostgresIndexBacklog(app_engine),
        ),
        rls_engine=app_engine,
    )
    return registry, indexer


def test_a_worker_killed_after_a_slice_re_runs_it_without_duplicates(
    seeded: Engine, app_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    storage, doc_ref = _document(tmp_path, monkeypatch)
    backend = _Backend()
    registry, indexer = _registry(storage, backend, app_engine)
    enqueue_document_index(
        JobQueue(seeded), owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV, doc_ref=doc_ref
    )
    queue = JobQueue(seeded)
    worker = Worker(
        dispatch_engine=seeded, rls_engine=app_engine, registry=registry, worker_id="w1"
    )

    # The "killed" worker: it claims the owner's turn and runs the handler, then dies
    # before the job is marked complete (nothing marks it; its lease simply lapses).
    (record,) = queue.claim(worker_id="dead", lease_seconds=5, serial_types=registry.serial_types())
    assert queue.mark_running(job_id=record.id, worker_id="dead")
    spec = registry.get(DOCUMENT_INDEX_JOB_TYPE)
    payload = registry.parse_payload(record.type, record.payload)

    class _Ctx:
        owner_id = OWNER
        job_id = record.id

        def connection(self) -> Any:
            from persona_api.db.engine import rls_connection

            return rls_connection(app_engine, OWNER)

    asyncio.run(spec.handler.handle(payload, _Ctx()))  # type: ignore[arg-type]
    assert len(backend.rows) == SLICE_CHUNKS
    assert [state for _, state, _ in _jobs(seeded)] == ["running", "queued"]  # its next turn

    # The lease lapses; the sweep returns the turn to the queue; the real worker re-runs
    # it, resuming from the recorded progress, and queues no third turn.
    assert queue.reclaim_expired(now=datetime.now(UTC) + timedelta(seconds=30)) == 1
    for _ in range(80):
        if not asyncio.run(worker.run_once(batch=1)):
            break
    indexer.close()
    ids = sorted(backend.rows)
    assert ids == [f"{CONV}::document::{doc_ref}::{i:04d}" for i in range(len(ids))]
    assert all(state == "succeeded" for _, state, _ in _jobs(seeded))
    (listed,) = document_service.list_for_conversation(
        file_storage=storage, owner_id=OWNER, persona_id=PERSONA, conversation_id=CONV
    )
    assert (listed.index_state, listed.chunks_indexed) == ("ready", len(ids))
    assert PostgresIndexBacklog(app_engine).count(OWNER) == 0


# ----- one job per owner ------------------------------------------------------------------


def _queued(engine: Engine) -> dict[str, int]:
    with engine.begin() as conn:
        rows = conn.execute(
            text(
                "SELECT owner_id, count(*) FROM jobs WHERE type = 'document_ingest' "
                "AND state = 'queued' GROUP BY owner_id"
            )
        ).all()
    return {str(r[0]): int(r[1]) for r in rows}


def test_an_owner_has_one_turn_queued_however_many_documents_wait(
    seeded: Engine, app_engine: Engine
) -> None:
    queue = JobQueue(app_engine)
    for n in range(50):
        enqueue_document_index(
            queue, owner_id="user_a", persona_id=PERSONA, conversation_id=CONV, doc_ref=f"d{n}"
        )
    enqueue_document_index(
        queue, owner_id="user_b", persona_id=PERSONA, conversation_id=CONV, doc_ref="b0"
    )
    assert _queued(seeded) == {"user_a": 1, "user_b": 1}
    assert PostgresIndexBacklog(app_engine).count("user_a") == 50


def test_concurrent_schedules_queue_one_turn(seeded: Engine, app_engine: Engine) -> None:
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _n: enqueue_owner_turn(app_engine, "user_a"), range(16)))
    assert results.count(True) == 1
    assert _queued(seeded) == {"user_a": 1}


def test_the_backlog_is_owner_scoped_under_rls(seeded: Engine, app_engine: Engine) -> None:
    from persona_api.db.engine import rls_connection
    from sqlalchemy.exc import DBAPIError

    backlog = PostgresIndexBacklog(app_engine)
    backlog.add("user_a", persona_id=PERSONA, conversation_id=CONV, doc_ref="secret")
    assert backlog.count("user_a") == 1
    assert backlog.count("user_b") == 0
    assert backlog.at_or_after("user_b", None) is None
    with pytest.raises(DBAPIError), rls_connection(app_engine, "user_b") as conn:
        conn.execute(
            text(
                "INSERT INTO document_index_backlog (owner_id, persona_id, conversation_id, "
                "doc_ref) VALUES ('user_a', 'p', 'c', 'planted')"
            )
        )


def test_owners_take_turns_and_an_owner_over_its_quota_is_parked_not_polled(
    seeded: Engine, app_engine: Engine, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reviewer's starvation, small: user_a has spent its hour and has documents waiting."""
    storage, doc_ref = _document(tmp_path, monkeypatch)  # user_a's
    from _document_upload import upload

    b_ref = upload(
        file_storage=storage,
        owner_id="user_b",
        persona_id=PERSONA,
        conversation_id=CONV,
        file_bytes="\n\n".join(f"Paragraph {p}. " + "word " * 120 for p in range(90)).encode(),
        filename="b.txt",
    )
    backend = _Backend()
    registry, indexer = _registry(storage, backend, app_engine)
    PostgresIndexQuota(app_engine).add("user_a", OWNER_CHUNKS_PER_HOUR)
    queue = JobQueue(seeded)
    enqueue_document_index(
        queue, owner_id="user_a", persona_id=PERSONA, conversation_id=CONV, doc_ref=doc_ref
    )
    enqueue_document_index(
        queue, owner_id="user_b", persona_id=PERSONA, conversation_id=CONV, doc_ref=b_ref.doc_ref
    )
    worker = Worker(
        dispatch_engine=seeded, rls_engine=app_engine, registry=registry, worker_id="w1"
    )
    turns = 0
    while asyncio.run(worker.run_once(batch=1)):
        turns += 1
        assert turns < 60
    indexer.close()
    assert backend.rows
    assert all(f"::{b_ref.doc_ref}::" in k for k in backend.rows)  # user_b indexed in full
    with seeded.begin() as conn:
        parked = conn.execute(
            text(
                "SELECT count(*), min(scheduled_at) FROM jobs WHERE type = 'document_ingest' "
                "AND owner_id = 'user_a' AND state = 'queued'"
            )
        ).one()
    assert parked[0] == 1  # one parked turn, not one per poll
    assert parked[1] > datetime.now(UTC) + timedelta(minutes=5)
    with seeded.begin() as conn:
        a_turns = conn.execute(
            text("SELECT count(*) FROM jobs WHERE type = 'document_ingest' AND owner_id = 'user_a'")
        ).scalar_one()
    assert a_turns == 2  # the turn that found the quota spent, and the parked one


# ----- least served first (fifth review F3) ---------------------------------------------


def _claim_owner(engine: Engine) -> str:
    queue = JobQueue(engine)
    (job,) = queue.claim(
        worker_id="w", lease_seconds=60, limit=1, serial_types=[DOCUMENT_INDEX_JOB_TYPE]
    )
    assert queue.mark_running(job_id=job.id, worker_id="w")
    assert queue.complete(job_id=job.id, worker_id="w")
    return job.owner_id


def test_the_least_served_owner_is_claimed_first_and_equals_in_turn(
    seeded: Engine, app_engine: Engine
) -> None:
    with seeded.begin() as conn:
        conn.execute(text("INSERT INTO users (id, email) VALUES ('user_c', 'c@example.com')"))
    quota = PostgresIndexQuota(app_engine)
    quota.add("user_a", 640)  # busy
    quota.add("user_b", 32)  # one slice served
    # queued oldest first: the busy owner, then the one-slice owner, then a new owner
    for owner in ("user_a", "user_b", "user_c"):
        assert enqueue_owner_turn(app_engine, owner)
    with seeded.begin() as conn:
        priorities = dict(
            conn.execute(
                text("SELECT owner_id, priority FROM jobs WHERE type = 'document_ingest'")
            ).all()
        )
    assert priorities == {"user_a": -640, "user_b": -32, "user_c": 0}
    assert [_claim_owner(seeded) for _ in range(3)] == ["user_c", "user_b", "user_a"]
    # served alike: the one that waited longest goes first
    quota.add("user_c", 32)
    for owner in ("user_b", "user_c"):
        assert enqueue_owner_turn(app_engine, owner)
    assert [_claim_owner(seeded) for _ in range(2)] == ["user_b", "user_c"]


def test_a_queued_turn_has_its_priority_refreshed(seeded: Engine, app_engine: Engine) -> None:
    assert enqueue_owner_turn(app_engine, "user_a")
    PostgresIndexQuota(app_engine).add("user_a", 96)
    assert not enqueue_owner_turn(app_engine, "user_a")  # one queued: refreshed, not added
    with seeded.begin() as conn:
        rows = conn.execute(
            text("SELECT priority FROM jobs WHERE type = 'document_ingest' AND state = 'queued'")
        ).all()
    assert [r[0] for r in rows] == [-96]


# ----- the quota ------------------------------------------------------------------------


def test_the_quota_sums_an_owners_last_hour_only(seeded: Engine, app_engine: Engine) -> None:
    quota = PostgresIndexQuota(app_engine)
    quota.add("user_a", 30)
    quota.add("user_a", 12)
    quota.add("user_b", 7)
    assert (quota.used("user_a"), quota.used("user_b")) == (42, 7)
    with seeded.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rate_limit_buckets (user_id, endpoint, window_start, request_count) "
                "VALUES ('user_a', 'document_index_chunks', now() - interval '2 hours', 999)"
            )
        )
    assert quota.used("user_a") == 42


def test_the_quota_resets_when_its_oldest_bucket_leaves_the_hour(
    seeded: Engine, app_engine: Engine
) -> None:
    quota = PostgresIndexQuota(app_engine)
    assert quota.reset_in("user_a") == 0.0
    with seeded.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO rate_limit_buckets (user_id, endpoint, window_start, request_count) "
                "VALUES ('user_a', 'document_index_chunks', "
                "date_trunc('hour', now()) - interval '40 minutes', 5)"
            )
        )
        oldest = conn.execute(
            text("SELECT date_trunc('hour', now()) - interval '40 minutes'")
        ).scalar_one()
    expected = (oldest + timedelta(hours=1) - datetime.now(UTC)).total_seconds()
    assert abs(quota.reset_in("user_a") - max(0.0, expected)) < 5


# ----- migration 059 --------------------------------------------------------------------

_API_DIR = Path(__file__).resolve().parents[2]


def _config(url: str) -> Config:
    cfg = Config(str(_API_DIR / "alembic.ini"))
    cfg.set_main_option("script_location", str(_API_DIR / "alembic"))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def _index(url: str) -> str | None:
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            return conn.execute(
                text(
                    "SELECT indexdef FROM pg_indexes WHERE indexname = 'idx_jobs_inflight_by_type'"
                )
            ).scalar()
    finally:
        engine.dispose()


@pytest.fixture
def clean_db(database_url: str) -> Iterator[str]:
    def _reset() -> None:
        engine = create_engine(database_url)
        with engine.begin() as conn:
            conn.execute(text("DROP SCHEMA public CASCADE"))
            conn.execute(text("CREATE SCHEMA public"))
        engine.dispose()

    _reset()
    yield database_url
    _reset()


def test_059_builds_the_index_on_a_deployed_db_and_its_downgrade_drops_only_it(
    clean_db: str,
) -> None:
    cfg = _config(clean_db)
    command.upgrade(cfg, "head")
    assert _index(clean_db) is not None  # a fresh database: create_all built it already
    command.downgrade(cfg, "058_jobs_live_task_leg_index")
    assert _index(clean_db) is None
    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:
            others = conn.execute(
                text("SELECT count(*) FROM pg_indexes WHERE indexname = 'idx_jobs_live_task_leg'")
            ).scalar_one()
    finally:
        engine.dispose()
    assert others == 1
    command.upgrade(cfg, "head")  # a deployed database: 059 genuinely creates it
    definition = _index(clean_db)
    assert definition is not None
    assert "WHERE (state = ANY (ARRAY['claimed'::text, 'running'::text]))" in definition


# ----- migration 060 --------------------------------------------------------------------


def test_060_builds_the_backlog_under_rls_and_turns_old_jobs_into_rows(clean_db: str) -> None:
    cfg = _config(clean_db)
    command.upgrade(cfg, "059_jobs_inflight_by_type")
    engine = create_engine(clean_db)
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS document_index_backlog"))
            conn.execute(text("INSERT INTO users (id, email) VALUES ('user_a', 'a@x')"))
            jobs = [
                ("astrid", "conv1", "doc0", "queued"),
                ("astrid", "conv1", "doc1", "queued"),
                ("astrid", "conv1", "doc2", "succeeded"),
                # ids the index refuses: not copied (fifth review)
                ("astrid", "cA/../../B/pB/conversations/cB", "doc3", "queued"),
                ("astrid", "conv1", "../doc4", "queued"),
                ("astrid", "conv1", "doc..5", "queued"),
                ("astrid", "conv1", "d/oc7", "queued"),  # a separator without ".."
                ("a/b", "conv1", "doc6", "queued"),
            ]
            for n, (persona, conversation, doc_ref, state) in enumerate(jobs):
                conn.execute(
                    text(
                        "INSERT INTO jobs (type, owner_id, payload, idempotency_key, state) "
                        "VALUES ('document_ingest', 'user_a', CAST(:p AS jsonb), :k, :s)"
                    ),
                    {
                        "p": json.dumps(
                            {
                                "persona_id": persona,
                                "conversation_id": conversation,
                                "doc_ref": doc_ref,
                                "slice": 0,
                            }
                        ),
                        "k": f"document_ingest:old:{n}",
                        "s": state,
                    },
                )
        command.upgrade(cfg, "060_document_index_backlog")
        with engine.begin() as conn:
            rows = conn.execute(
                text("SELECT owner_id, doc_ref FROM document_index_backlog ORDER BY doc_ref")
            ).all()
            jobs_left = conn.execute(
                text("SELECT state FROM jobs WHERE type = 'document_ingest'")
            ).all()
            rls = conn.execute(
                text(
                    "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                    "WHERE relname = 'document_index_backlog'"
                )
            ).one()
            policy = conn.execute(
                text("SELECT qual FROM pg_policies WHERE tablename = 'document_index_backlog'")
            ).scalar_one()
        assert [tuple(r) for r in rows] == [("user_a", "doc0"), ("user_a", "doc1")]
        assert [r[0] for r in jobs_left] == ["succeeded"]
        assert tuple(rls) == (True, True)
        assert "owner_id" in policy
        command.downgrade(cfg, "059_jobs_inflight_by_type")
        with engine.begin() as conn:
            gone = conn.execute(text("SELECT to_regclass('document_index_backlog')")).scalar()
        assert gone is None
        command.upgrade(cfg, "head")
    finally:
        engine.dispose()


# ----- a document's chunks deleted by id prefix (fourth review finding 3) -----------------


def test_a_documents_chunks_go_by_id_prefix_and_wildcards_match_only_themselves(
    seeded: Engine,
) -> None:
    from persona.schema.chunks import PersonaChunk
    from persona.stores.embedder import HashEmbedder
    from persona.stores.postgres import PostgresBackend

    backend = PostgresBackend(engine=seeded, embedder=HashEmbedder())
    now = datetime.now(UTC)

    def _chunks(persona: str, kind: str, doc: str) -> None:
        backend.upsert(
            persona_id=persona,
            store_kind=kind,
            chunks=[
                PersonaChunk(
                    id=f"{persona}::document::{doc}::{n:04d}",
                    text=f"{doc} {n}",
                    metadata={"doc_ref": doc},
                    created_at=now,
                )
                for n in range(3)
            ],
        )

    _chunks("conv1", "document", "a_b-1")
    _chunks("conv1", "document", "aXb-1")  # "_" must not match "X"
    _chunks("conv1", "document", "a_b-10")  # the "::" ends the prefix
    _chunks("conv1", "document", "a%-1")
    _chunks("conv2", "document", "a_b-1")  # another conversation
    _chunks("conv1", "episodic", "a_b-1")  # another kind
    # the same id prefix under another persona: the persona scope alone keeps it
    backend.upsert(
        persona_id="conv2",
        store_kind="document",
        chunks=[
            PersonaChunk(
                id="conv1::document::a_b-1::9999",
                text="foreign",
                metadata={"doc_ref": "a_b-1"},
                created_at=now,
            )
        ],
    )

    def _left(persona: str, kind: str) -> set[str]:
        return {c.id.split("::")[2] for c in backend.get_all(persona_id=persona, store_kind=kind)}

    backend.delete_by_id_prefix(
        persona_id="conv1", store_kind="document", prefix="conv1::document::a_b-1::"
    )
    assert _left("conv1", "document") == {"aXb-1", "a_b-10", "a%-1"}
    backend.delete_by_id_prefix(
        persona_id="conv1", store_kind="document", prefix="conv1::document::a%-1::"
    )
    assert _left("conv1", "document") == {"aXb-1", "a_b-10"}
    assert {c.id for c in backend.get_all(persona_id="conv2", store_kind="document")} == {
        "conv2::document::a_b-1::0000",
        "conv2::document::a_b-1::0001",
        "conv2::document::a_b-1::0002",
        "conv1::document::a_b-1::9999",
    }
    assert _left("conv1", "episodic") == {"a_b-1"}


# ----- statement_timeout on the index's database calls (fourth review finding 2) ----------


def test_the_index_store_calls_inside_a_slice_get_a_statement_timeout_and_nothing_else_does(
    seeded: Engine,
) -> None:
    from persona_api.services import document_index

    document_index.install_statement_bound(seeded)
    document_index.install_statement_bound(seeded)  # idempotent

    def _timeout() -> str:
        with seeded.begin() as conn:
            return str(conn.execute(text("SHOW statement_timeout")).scalar_one())

    default = _timeout()
    indexer = DocumentIndexer()
    try:
        inside = asyncio.run(indexer.run(_timeout, wall_seconds=30))
    finally:
        indexer.close()
    assert inside == f"{document_index.INDEX_STATEMENT_TIMEOUT_MS // 1000}s"
    assert _timeout() == default  # transaction-local: the pooled connection keeps nothing


def test_the_backlog_and_quota_transactions_bound_their_statements(app_engine: Engine) -> None:
    from persona_api.db.engine import rls_connection
    from persona_api.services import document_index

    with rls_connection(app_engine, "user_a") as conn:
        document_index._bound(conn)
        value = conn.execute(text("SHOW statement_timeout")).scalar_one()
    assert value == f"{document_index.INDEX_STATEMENT_TIMEOUT_MS // 1000}s"


def test_a_deleted_conversation_leaves_the_backlog(seeded: Engine, app_engine: Engine) -> None:
    backlog = PostgresIndexBacklog(app_engine)
    for conv, doc in (("c1", "a"), ("c1", "b"), ("c2", "a")):
        backlog.add("user_a", persona_id=PERSONA, conversation_id=conv, doc_ref=doc)
    backlog.add("user_b", persona_id=PERSONA, conversation_id="c1", doc_ref="a")
    backlog.remove_conversation("user_a", "c1")
    assert backlog.count("user_a") == 1
    assert backlog.has("user_a", conversation_id="c2", doc_ref="a")
    assert backlog.count("user_b") == 1
