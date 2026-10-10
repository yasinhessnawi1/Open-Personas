"""The document index: retrieval documents are chunked and embedded in the background (D-FV0-31).

The third security review found ingest (tokenise, chunk, embed) running inside the upload
request with no bound: a 12 KiB PDF took 1,224 s of api CPU and 1.4 GiB. An upload now
stores the document ``pending`` and returns; this module indexes it afterwards, a slice
at a time, on one low-priority thread.

**Where it runs.** A ``document_ingest`` job on the A0 jobs system, in the in-process
worker (durable, recovered through lease expiry, the ``file_extract`` pattern). Without a
jobs system (a community boot without the worker) the same slices run from an in-process
queue, :class:`LocalDocumentIndexQueue`.

**One thread, low priority, one core.** Every slice runs on :class:`DocumentIndexer`'s one
thread, never the default executor, so the embedder's CPU stays off the request path:

- On Linux the thread sets its own niceness to 19 when it starts (``setpriority`` on its
  thread id), so a chat turn's encode preempts it.
- torch's intra-op OpenMP pool is NOT that thread: at four cores an encode of one batch of
  four spent about 3.4 s in each of three pool threads for 3.9 s in the calling thread,
  and those pool threads were not niced. The thread therefore limits ITS OWN OpenMP
  thread count to one (``omp_set_num_threads`` from torch's own OpenMP runtime, called in
  this thread: the setting is per thread). Measured: all the work then runs in the niced
  thread, and the rest of the process keeps four (``torch.get_num_threads()`` in any other
  thread, and in a thread started later, still reads 4).
- macOS and Windows: there is no per-thread niceness, so the thread runs at normal
  priority (logged at start). The per-thread OpenMP limit still applies where torch's
  OpenMP runtime is found (``libomp`` on macOS); the index then competes for at most one
  core. If no runtime is found, the limit is not applied, and that is logged too.

**Slices.** A job embeds at most :data:`SLICE_CHUNKS` chunks, in batches of
:data:`EMBED_BATCH`, then queues its continuation, whose payload carries the chunk cursor
(:class:`persona.documents.chunker.ChunkCursor`), so the next slice resumes exactly and
nothing is encoded twice. Owners take turns slice by slice, least served first: when the
lane frees, the owner with the fewest chunks indexed in the last hour goes next, and owners
served alike take turns in order (:func:`enqueue_owner_turn`). ``document_ingest`` is a
SERIAL job type: the claim never takes one while another is claimed or running
(``JobTypeSpec.serial``), so the process embeds one slice at a time.

**Bounds.** At most :data:`~persona.documents.ingest.MAX_DOCUMENT_CHUNKS` chunks per
document (then the document is ``partial``), and :data:`OWNER_CHUNKS_PER_HOUR` per owner
over a rolling hour (then the slice waits :data:`QUOTA_RETRY_SECONDS` and tries again; the
document stays as it is). Each slice records its chunks and the indexer thread's own CPU
time (``time.thread_time``) in the log; the quota counts chunks.

**Idempotent, and no orphans.** Chunk ids are ``{conversation}::document::{doc_ref}::{n}``
and writes overwrite by id, so a slice that runs twice (a worker killed mid-slice, then the
lease expires) writes the same rows again. Before a document's first slice is submitted its
older chunks are deleted, so nothing from an earlier run survives. Every write holds the
document's fence and is skipped once its slice was abandoned (:func:`_fenced`), so an
abandoned slice never writes over its retry. Every slice reads the document's
``.meta.json`` first and stops, deleting the document's chunks, if it is gone; after writing
it checks again. The document delete removes the ``.meta.json`` before the chunks, so
either the delete's chunk removal runs after the slice wrote, or the slice sees the delete.

**Failure.** A document with no readable text (and no original to re-parse) fails at once
(``failed``). Any other error retries with the job system's backoff; the final attempt marks
the document ``failed`` before it starts, so a worker that dies during it still leaves the
truth behind, and a slice that succeeds overwrites it.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import ctypes
import dataclasses
import functools
import os
import re
import sys
import threading
import time
import uuid
import weakref
from collections import deque
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import TYPE_CHECKING, Any, Literal, Protocol, TypeVar

from persona.documents.chunker import ChunkCursor, iter_document_chunks
from persona.documents.ingest import MAX_DOCUMENT_CHUNKS, IngestStrategy
from persona.errors import JobDeferredError, PermanentJobError, PersonaError
from persona.jobs import MEDIUM_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from pydantic import Field, model_validator
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import text

from persona_api.services import document_service
from persona_api.services.artifact_metadata import (
    DOCUMENT_INDEX_SIDECAR_SUFFIX,
    SPEC_14_SIDECAR_SUFFIX,
)
from persona_api.services.document_service import (
    DocumentRef,
    DocumentUnreadableError,
    IndexProgress,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from persona.jobs import JobContext, JobRegistry
    from persona.stores.document_store import DocumentStore
    from sqlalchemy import Engine

    from persona_api.jobs.queue import JobQueue
    from persona_api.services.document_parsing import DocumentParser
    from persona_api.storage import FileStorage

__all__ = [
    "DOCUMENT_INDEX_JOB_TYPE",
    "EMBED_BATCH",
    "OWNER_CHUNKS_PER_HOUR",
    "QUOTA_RETRY_SECONDS",
    "SLICE_CHUNKS",
    "MAX_PENDING_DOCUMENTS_PER_OWNER",
    "BacklogEntry",
    "DocumentIndexBacklogFullError",
    "DocumentIndexDeps",
    "InMemoryIndexBacklog",
    "PostgresIndexBacklog",
    "check_index_ids",
    "enqueue_document_index",
    "DocumentIndexJobPayload",
    "DocumentIndexScheduler",
    "DocumentIndexer",
    "DocumentIndexerExhaustedError",
    "INDEXER_PAUSE_SECONDS",
    "InMemoryIndexQuota",
    "JobDocumentIndexScheduler",
    "LocalDocumentIndexQueue",
    "PostgresIndexQuota",
    "register_document_index_handler",
    "run_slice",
]

_log = get_logger("api.document_index")

#: The A0 job type.
DOCUMENT_INDEX_JOB_TYPE: str = "document_ingest"

#: The most chunks one slice embeds before it hands over (about 10 s of CPU on an idle core).
SLICE_CHUNKS: int = 32

#: Chunks embedded per encode call. One batch of four adds tens of MiB; eight added 140.
EMBED_BATCH: int = 4

#: The most chunks one owner's documents may embed in any hour (about ten minutes of CPU).
OWNER_CHUNKS_PER_HOUR: int = 2_000

#: The longest an owner over its quota is parked before its turn is tried again. The park
#: ends earlier, when the quota's oldest charge leaves the hour.
QUOTA_RETRY_SECONDS: int = 600

#: Retrieval documents one owner may have waiting for the index. An upload past it is
#: refused (429 ``index_backlog_full``) before anything is stored. Twenty documents of up
#: to 1,000 chunks each is ten hours of the owner's hourly quota.
MAX_PENDING_DOCUMENTS_PER_OWNER: int = 20

#: The fewest seconds between two pick-ups of one conversation in this process.
PICK_UP_INTERVAL_SECONDS: float = 30.0

#: Documents found gone or finished that one turn may step past before it stops.
_TURN_SKIP_LIMIT = 20

#: ``statement_timeout`` for the index's own SQL (backlog, quota, its job).
INDEX_STATEMENT_TIMEOUT_MS: int = 15_000

#: The niceness the indexer thread gives itself on Linux.
INDEXER_NICENESS: int = 19

#: Retries for a slice, the job system's ladder (the last attempt marks the document failed).
_RETRY = RetryPolicy(max_attempts=4, base_backoff_seconds=10.0, max_backoff_seconds=300.0)

_T = TypeVar("_T")

SliceState = Literal["gone", "indexing", "ready", "partial"]


#: A persona, conversation or document id as the index accepts it: no path separator, no
#: ``..``, nothing a storage key could be steered with (fourth review finding 11). Ids are
#: ``persona_<hex>`` / ``conv_<hex>`` in production and short slugs in the community
#: edition and tests, so the shape is checked, not a uuid.
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SAFE_DOC_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,199}$")


def check_index_ids(*, persona_id: str, conversation_id: str, doc_ref: str) -> None:
    """Refuse ids that could name a key outside the owner's own documents.

    Raises:
        ValueError: An id is not of the accepted shape.
    """
    if not _SAFE_ID.match(persona_id):
        msg = "a persona id is not of the accepted shape"
        raise ValueError(msg)
    _check_document_ids(conversation_id=conversation_id, doc_ref=doc_ref)


def _check_document_ids(*, conversation_id: str, doc_ref: str) -> None:
    if not _SAFE_ID.match(conversation_id):
        msg = "a conversation id is not of the accepted shape"
        raise ValueError(msg)
    if not _SAFE_DOC_REF.match(doc_ref) or ".." in doc_ref:
        msg = "a document id is not of the accepted shape"
        raise ValueError(msg)


class DocumentIndexJobPayload(JobPayload):
    """One turn of one OWNER's index: the cursor into that owner's waiting documents.

    The job is the owner's, not a document's (fourth review finding 1): a turn indexes one
    slice of the document at the cursor, then queues the owner's next turn at the back of
    the queue. The cursor names the document the last turn worked on; ``None`` starts at
    the owner's oldest waiting document.
    """

    turn: int = Field(default=0, ge=0)
    cursor_added_at: datetime | None = None
    cursor_conversation_id: str | None = None
    cursor_doc_ref: str | None = None

    @model_validator(mode="after")
    def _ids_are_safe(self) -> DocumentIndexJobPayload:
        if (self.cursor_conversation_id is None) != (self.cursor_doc_ref is None):
            msg = "a cursor names both its conversation and its document, or neither"
            raise ValueError(msg)
        if self.cursor_conversation_id is not None and self.cursor_doc_ref is not None:
            _check_document_ids(
                conversation_id=self.cursor_conversation_id, doc_ref=self.cursor_doc_ref
            )
        return self


def document_index_idempotency_key(owner_id: str) -> str:
    """A fresh key per owner turn; one queued turn per owner is kept by a lock, not the key."""
    return f"{DOCUMENT_INDEX_JOB_TYPE}:{owner_id}:{uuid.uuid4().hex}"


@dataclass(frozen=True)
class BacklogEntry:
    """One document waiting for the index, in its owner's backlog."""

    persona_id: str
    conversation_id: str
    doc_ref: str
    added_at: datetime


class DocumentIndexBacklogFullError(PersonaError):
    """The owner already has :data:`MAX_PENDING_DOCUMENTS_PER_OWNER` documents waiting."""


class DocumentIndexUnreadableError(PersonaError):
    """The document has no text the index can read, and none can be had. Permanent."""


@dataclass(frozen=True)
class SliceOutcome:
    """What one slice did. ``state`` is the document's index state after it."""

    state: SliceState
    chunks_written: int
    cursor: ChunkCursor
    cpu_seconds: float


# ----- the indexer thread ------------------------------------------------------


_thread_state = threading.local()


def _lower_this_threads_priority() -> None:
    """Give the indexer thread niceness 19 (Linux; per thread, by its native id)."""
    if not sys.platform.startswith("linux"):
        _log.info(
            "document indexer runs at normal priority: per-thread niceness is Linux-only "
            "(platform={})",
            sys.platform,
        )
        return
    try:
        os.setpriority(os.PRIO_PROCESS, threading.get_native_id(), INDEXER_NICENESS)
    except OSError as exc:
        _log.warning("document indexer could not lower its priority: {}", exc)


def _openmp_runtime() -> Any:  # noqa: ANN401 - a ctypes library handle
    """torch's own OpenMP runtime library, or ``None`` when it cannot be found."""
    try:
        import torch  # noqa: PLC0415 - the embedder's dependency, loaded on first use
    except ImportError:
        return None
    lib = Path(torch.__file__).parent / "lib"
    prefixes = ("libgomp", "libomp", "libiomp5", "vcomp")
    suffixes = (".so", ".1", ".dylib", ".dll")
    for candidate in sorted(lib.glob("*")) if lib.is_dir() else ():
        if candidate.name.startswith(prefixes) and candidate.suffix in suffixes:
            try:
                return ctypes.CDLL(str(candidate))
            except OSError:
                continue
    return None


def single_threaded_torch() -> bool:
    """Limit torch's intra-op parallelism to one thread, in THIS thread only. Once per thread.

    ``torch.set_num_threads`` would change every thread's setting. OpenMP's
    ``omp_set_num_threads`` changes only the calling thread's, and torch reads the calling
    thread's setting for each parallel region. torch first initialises a thread's setting
    from its global value on that thread's first parallel call, so
    ``torch.get_num_threads()`` runs first, then the limit, then a check.

    Returns whether this thread's encodes now run on one thread.
    """
    limited: bool | None = getattr(_thread_state, "limited", None)
    if limited is not None:
        return limited
    limited = False
    try:
        import torch  # noqa: PLC0415 - see _openmp_runtime

        torch.get_num_threads()
        runtime = _openmp_runtime()
        if runtime is not None:
            runtime.omp_set_num_threads(1)
            limited = torch.get_num_threads() == 1
    except Exception as exc:  # noqa: BLE001 - never fail a slice over a speed setting
        _log.warning("document indexer could not limit torch to one thread: {}", exc)
    if not limited:
        _log.warning(
            "document indexer encodes with torch's full thread pool: its OpenMP runtime "
            "was not found, so the encode's pool threads run at normal priority"
        )
    _thread_state.limited = limited
    return limited


def _on_one_core(fn: Callable[[], _T]) -> _T:
    single_threaded_torch()
    return fn()


#: The share of one core the index may use on average (fourth review finding 4). After
#: each batch the indexer thread sleeps long enough that its CPU over CPU-plus-sleep stays
#: at this fraction. Niceness only orders the index behind chat; on a burstable VM (Fly's
#: shared CPUs) any CPU spends the machine's burst balance, and once that is gone chat is
#: throttled too, so the index's average is capped as well. A quarter of a core is about
#: 2,800 chunks an hour at the measured 0.32 s of CPU per chunk: one owner's hourly quota
#: still fits in the hour. Whether Fly throttles is for the owner to verify on Fly.
INDEX_DUTY_CYCLE: float = 0.25

#: Env var for :data:`INDEX_DUTY_CYCLE` (0.02 to 1.0; 1.0 switches the pacing off).
INDEX_DUTY_CYCLE_ENV_VAR: str = "PERSONA_DOC_INDEX_DUTY_CYCLE"


def index_duty_cycle() -> float:
    """The duty cycle from :data:`INDEX_DUTY_CYCLE_ENV_VAR`, else :data:`INDEX_DUTY_CYCLE`."""
    raw = os.environ.get(INDEX_DUTY_CYCLE_ENV_VAR)
    if not raw:
        return INDEX_DUTY_CYCLE
    try:
        value = float(raw)
    except ValueError:
        value = 0.0
    if not 0.02 <= value <= 1.0:  # also refuses nan
        _log.warning("{} must be 0.02 to 1.0; using {}", INDEX_DUTY_CYCLE_ENV_VAR, INDEX_DUTY_CYCLE)
        return INDEX_DUTY_CYCLE
    return value


def _pace(cpu_seconds: float, duty: float) -> None:
    """Sleep so that ``cpu_seconds`` of work is ``duty`` of the time it took (indexer thread).

    Stops sleeping early when the slice is abandoned.
    """
    if duty >= 1.0 or cpu_seconds <= 0:
        return
    pause = cpu_seconds * (1.0 / duty - 1.0)
    abandoned = _slice_abandoned.get()
    if abandoned is not None:
        abandoned.wait(pause)
    else:
        time.sleep(pause)


def slice_wall_seconds(duty: float) -> float:
    """The slice bound at ``duty``: :data:`SLICE_WALL_SECONDS`, longer below a quarter core."""
    return SLICE_WALL_SECONDS * max(1.0, 0.25 / max(duty, 0.02))


#: The longest one slice may run on the indexer thread (fourth review finding 2). A slice
#: is about 10 s of CPU on an idle core; this allows a busy core and the duty cycle many
#: times over. Past it the attempt fails and the thread is replaced.
SLICE_WALL_SECONDS: float = 300.0

#: A slice still running after this long is logged as holding the index lane.
LANE_WARN_SECONDS: float = 120.0

#: The longest the embedder warm-up may run before it counts as a stuck thread (F6).
WARM_UP_WALL_SECONDS: float = 120.0

#: Abandoned indexer threads (each stuck in a call that never returned) this process
#: tolerates before it stops indexing.
MAX_ABANDONED_THREADS: int = 3

#: How long an owner's turn waits when the indexer is out of threads (fifth review F1).
#: The turn is put back, due this much later, at the same attempt; nothing is marked and
#: the backlog is left as it is. A stuck call that returns frees its thread, and indexing
#: resumes on the next turn after that; otherwise a restart does.
INDEXER_PAUSE_SECONDS: float = 120.0

#: Set while a slice runs: whether the slice was given up on (its late writes stop).
_slice_abandoned: contextvars.ContextVar[threading.Event | None] = contextvars.ContextVar(
    "document_index_slice_abandoned", default=None
)

#: Set while a slice runs: the ``statement_timeout`` its store calls get.
_slice_statement_timeout_ms: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "document_index_statement_timeout_ms", default=None
)

_bounded_engines: weakref.WeakSet[Any] = weakref.WeakSet()

#: How long a write waits for its document's fence before the slice fails (transient).
FENCE_WAIT_SECONDS: float = 30.0

#: The write fences: one lock per stripe of documents, process wide (fifth review F2).
_FENCE_STRIPES = 64
_fences: tuple[threading.Lock, ...] = tuple(threading.Lock() for _ in range(_FENCE_STRIPES))


class DocumentIndexWriteBlockedError(PersonaError):
    """A write waited past :data:`FENCE_WAIT_SECONDS` for its document's fence. Transient."""


def _fence_for(
    owner_id: str, persona_id: str, conversation_id: str, doc_ref: str
) -> threading.Lock:
    """The fence every index write to this document holds."""
    return _fences[hash((owner_id, persona_id, conversation_id, doc_ref)) % _FENCE_STRIPES]


def _fenced(fence: threading.Lock, write: Callable[[], object], *, cleanup: bool = False) -> bool:
    """Run ``write`` under the document's fence unless this slice was abandoned. True if it ran.

    Fifth review F2: an abandoned slice must never write. The attempt token is the slice's
    abandonment event (:data:`_slice_abandoned`), and it is read under the fence, so once a
    slice is abandoned none of its writes starts. A write already running when it was
    abandoned finishes first, and every later write to the document (its retry's, the
    fresh start's delete, a ``failed`` mark) waits for the fence, so nothing the abandoned
    slice wrote lands after anything its retry wrote. ``cleanup`` skips the token: for
    removing a DELETED document's leftovers, which no retry can own.

    Raises:
        DocumentIndexWriteBlockedError: The fence stayed held (a write stuck in a call).
    """
    if not fence.acquire(timeout=FENCE_WAIT_SECONDS):
        msg = "a document index write is still blocked by an earlier slice"
        raise DocumentIndexWriteBlockedError(msg)
    try:
        if not cleanup and _given_up():
            return False
        write()
        return True
    finally:
        fence.release()


def install_statement_bound(engine: Any) -> None:  # noqa: ANN401 - a SQLAlchemy Engine
    """Bound the index's store calls on ``engine`` with ``statement_timeout``.

    A ``begin`` listener issues ``SET LOCAL statement_timeout`` when the transaction is
    begun inside a slice (the store opens its own transactions, so the index cannot do
    it per call). Transaction-local: nothing outlives the transaction on the pooled
    connection, and outside a slice the listener only reads a context variable.
    Idempotent per engine; Postgres only.
    """
    if engine is None or engine in _bounded_engines:
        return

    @sqlalchemy_event.listens_for(engine, "begin")
    def _bound_index_statements(conn: Any) -> None:  # noqa: ANN401 - a SQLAlchemy Connection
        timeout_ms = _slice_statement_timeout_ms.get()
        if timeout_ms is not None and conn.dialect.name == "postgresql":
            conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(timeout_ms)}")

    _bounded_engines.add(engine)


class DocumentIndexSliceTimeoutError(PersonaError):
    """A slice ran past :data:`SLICE_WALL_SECONDS`; its thread was replaced. Transient."""


class DocumentIndexerExhaustedError(PersonaError):
    """Too many indexer threads are stuck; this process indexes nothing for now.

    A lane pause, not a failure of the document or its owner: the turn is put back at the
    same attempt (:data:`INDEXER_PAUSE_SECONDS`), nothing is marked ``failed``, and the
    backlog keeps every document.
    """


class _DaemonThreadExecutor:
    """One daemon thread that runs submitted calls in order (fifth review F5).

    :class:`~concurrent.futures.ThreadPoolExecutor` threads are joined when the interpreter
    exits, so one abandoned slice stuck in a call that never returns kept the process
    from exiting. A daemon thread is not joined. The thread starts with the first call,
    runs ``initializer`` first, and is named ``{name}_0`` like the pool's.
    """

    def __init__(self, name: str, initializer: Callable[[], None]) -> None:
        self._name = name
        self._initializer = initializer
        self._work: SimpleQueue[tuple[ConcurrentFuture[Any], Callable[[], Any]] | None] = (
            SimpleQueue()
        )
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._closed = False

    def submit(self, fn: Callable[..., _T], *args: Any) -> ConcurrentFuture[_T]:  # noqa: ANN401
        future: ConcurrentFuture[_T] = ConcurrentFuture()
        with self._lock:
            if self._closed:
                msg = "cannot run new work after shutdown"
                raise RuntimeError(msg)
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._loop, name=f"{self._name}_0", daemon=True
                )
                self._thread.start()
            self._work.put((future, functools.partial(fn, *args)))
        return future

    def _loop(self) -> None:
        self._initializer()
        while (item := self._work.get()) is not None:
            future, call = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                result = call()
            except BaseException as exc:  # noqa: BLE001 - handed to the caller, as the pool does
                future.set_exception(exc)
            else:
                future.set_result(result)

    def shutdown(self, *, wait: bool = False, cancel_futures: bool = True) -> None:
        with self._lock:
            self._closed = True
            if cancel_futures:
                while True:
                    try:
                        item = self._work.get_nowait()
                    except Empty:
                        break
                    if item is not None:
                        item[0].cancel()
            self._work.put(None)
            thread = self._thread
        if wait and thread is not None:
            thread.join()


class DocumentIndexer:
    """The one thread every slice of every document runs on, in this process.

    A slice that runs past its wall-clock bound cannot be stopped: a thread cannot be
    killed, and a native call (an encode, a database read) does not see Python's
    interrupts. So the slice is abandoned instead. Its attempt fails, which ends its
    job's lease renewal and lets another owner's turn run; it is told it was abandoned,
    so it writes nothing more after the call it is stuck in returns (at most the one
    batch already inside that call); and it gets a fresh thread, so the next slice does
    not queue behind the stuck one. The stuck thread is left to finish or to stay stuck.
    After :data:`MAX_ABANDONED_THREADS` such threads are still stuck, the process pauses
    indexing and says so loudly (and on ``/healthz``): every turn is put back at the same
    attempt and no document is marked, so a stuck call that returns, or a restart, resumes
    everything where it was.
    """

    def __init__(self) -> None:
        self._executor = self._new_executor()
        self._stuck: set[ConcurrentFuture[Any]] = set()
        self._warm_up: Callable[[], object] | None = None
        self._warmed = False
        self._warm_lock = threading.Lock()

    def warm_up_with(self, fn: Callable[[], object]) -> None:
        """Run ``fn`` once, on a normal-priority thread, before the first slice.

        The embedder starts its helper threads (the tokenizer's pool) on first use, and a
        thread inherits its creator's niceness: if the niced indexer thread encoded
        first, the pool chat tokenizes on would run at niceness 19 too (fourth security
        review finding 5). The app warms the embedder this way; a failure is logged and
        indexing goes on.
        """
        self._warm_up = fn

    async def _warm_bounded(self) -> None:
        """Run the warm-up on its own normal-priority daemon thread, bounded (fifth review F6).

        The thread is started from the event loop's thread, so it is not niced. A warm-up
        still running after :data:`WARM_UP_WALL_SECONDS` is treated like a stuck slice: it
        is counted among the abandoned threads (so enough of them pause indexing) and is
        never tried again; the slice goes on.
        """
        done: ConcurrentFuture[None] = ConcurrentFuture()
        done.set_running_or_notify_cancel()  # so the caller's timeout cannot cancel it

        def _warm() -> None:
            try:
                self._warm_once()
            finally:
                done.set_result(None)

        threading.Thread(target=_warm, name="document-indexer-warm-up", daemon=True).start()
        try:
            await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(done)), WARM_UP_WALL_SECONDS)
        except TimeoutError:
            self._warmed = True  # never again: the embedder it warms is what hangs
            if not done.done():
                self._stuck.add(done)
                done.add_done_callback(self._stuck.discard)
            _log.error(
                "the embedder warm-up ran past {} s; its thread is counted as stuck ({} stuck)",
                WARM_UP_WALL_SECONDS,
                len(self._stuck),
            )

    def _warm_once(self) -> None:
        with self._warm_lock:
            if self._warmed or self._warm_up is None:
                return
            try:
                self._warm_up()
            except Exception as exc:  # noqa: BLE001 - a warm-up never stops the index
                _log.warning("document indexer could not warm the embedder first: {}", exc)
            self._warmed = True

    @staticmethod
    def _new_executor() -> _DaemonThreadExecutor:
        return _DaemonThreadExecutor("document-indexer", _lower_this_threads_priority)

    @property
    def abandoned_threads(self) -> int:
        """Abandoned slices whose thread has not returned yet."""
        return len(self._stuck)

    @property
    def exhausted(self) -> bool:
        """Whether too many threads are stuck to index anything (``/healthz`` degrades)."""
        return len(self._stuck) >= MAX_ABANDONED_THREADS

    async def ensure_ready(self) -> None:
        """Warm the embedder once, then refuse if too many threads are stuck.

        A turn calls this before it changes anything, so a refusal leaves every document
        and the backlog as they were.

        Raises:
            DocumentIndexerExhaustedError: Too many abandoned threads are still stuck.
        """
        if self._warm_up is not None and not self._warmed:
            await self._warm_bounded()
        if self.exhausted:
            _log.critical(
                "document indexer has {} stuck threads; indexing pauses until one returns "
                "or the process restarts",
                len(self._stuck),
            )
            msg = "the document indexer is stuck; indexing is paused"
            raise DocumentIndexerExhaustedError(msg, context={"stuck": str(len(self._stuck))})

    async def run(
        self, fn: Callable[[], _T], *, wall_seconds: float | None = None, label: str = ""
    ) -> _T:
        """Run ``fn`` on the indexer thread, in the caller's context (its owner scope).

        The thread's torch runs on one core (:func:`single_threaded_torch`) before ``fn``.
        With ``wall_seconds``, a call still running past it is abandoned (see the class)
        and :class:`DocumentIndexSliceTimeoutError` is raised; one still running after
        :data:`LANE_WARN_SECONDS` is logged first.

        Raises:
            DocumentIndexSliceTimeoutError: ``fn`` ran past ``wall_seconds``.
            DocumentIndexerExhaustedError: Too many abandoned threads are still stuck.
        """
        await self.ensure_ready()
        abandoned = threading.Event()
        marker = _slice_abandoned.set(abandoned)
        bound = _slice_statement_timeout_ms.set(INDEX_STATEMENT_TIMEOUT_MS)
        try:
            context = contextvars.copy_context()
        finally:
            _slice_statement_timeout_ms.reset(bound)
            _slice_abandoned.reset(marker)
        loop = asyncio.get_running_loop()
        started = time.monotonic()
        future = self._executor.submit(context.run, _on_one_core, fn)
        warning = loop.call_later(
            LANE_WARN_SECONDS,
            lambda: _log.error(
                "document index slice has held the index lane for {:.0f} s: {}",
                time.monotonic() - started,
                label,
            ),
        )
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), wall_seconds)
        except TimeoutError:
            abandoned.set()
            self._abandon(future)
            _log.error(
                "document index slice ran past {} s and was abandoned; a fresh indexer "
                "thread takes over ({} stuck): {}",
                wall_seconds,
                len(self._stuck),
                label,
            )
            msg = "the document index slice took too long"
            raise DocumentIndexSliceTimeoutError(
                msg, context={"wall_seconds": str(wall_seconds)}
            ) from None
        finally:
            warning.cancel()

    def _abandon(self, future: ConcurrentFuture[Any]) -> None:
        if not future.done():
            self._stuck.add(future)
            future.add_done_callback(self._stuck.discard)
        old, self._executor = self._executor, self._new_executor()
        old.shutdown(wait=False, cancel_futures=True)

    def close(self) -> None:
        """Stop taking work; a slice already running finishes (its job is re-run if lost)."""
        self._executor.shutdown(wait=False, cancel_futures=True)


def _given_up() -> bool:
    """Whether the slice running in this thread was abandoned by its caller."""
    abandoned = _slice_abandoned.get()
    return abandoned is not None and abandoned.is_set()


# ----- the per-owner quota -------------------------------------------------------


class IndexQuota(Protocol):
    """Chunks each owner's documents embedded over the last hour."""

    def used(self, owner_id: str) -> int: ...

    def add(self, owner_id: str, chunks: int) -> None: ...

    def reset_in(self, owner_id: str) -> float:
        """Seconds until the owner's oldest charge in the hour leaves it."""
        ...


class InMemoryIndexQuota:
    """The quota in this process only: the fallback queue's (one community owner)."""

    def __init__(self, *, window_seconds: float = 3_600.0) -> None:
        self._window = window_seconds
        self._spent: dict[str, deque[tuple[float, int]]] = {}
        self._lock = threading.Lock()

    def used(self, owner_id: str) -> int:
        horizon = time.monotonic() - self._window
        with self._lock:
            spent = self._spent.setdefault(owner_id, deque())
            while spent and spent[0][0] < horizon:
                spent.popleft()
            return sum(chunks for _, chunks in spent)

    def add(self, owner_id: str, chunks: int) -> None:
        if chunks > 0:
            with self._lock:
                self._spent.setdefault(owner_id, deque()).append((time.monotonic(), chunks))

    def reset_in(self, owner_id: str) -> float:
        with self._lock:
            spent = self._spent.get(owner_id)
            if not spent:
                return 0.0
            return max(0.0, spent[0][0] + self._window - time.monotonic())


#: The quota rides the platform limiter's table: ten-minute buckets, the last six summed.
_QUOTA_ENDPOINT = "document_index_chunks"
_QUOTA_BUCKET_SECONDS = 600

_QUOTA_USED_SQL = text(
    "SELECT COALESCE(SUM(request_count), 0) FROM rate_limit_buckets "
    "WHERE user_id = :u AND endpoint = :e AND window_start > :since"
)
_QUOTA_OLDEST_SQL = text(
    "SELECT MIN(window_start) FROM rate_limit_buckets "
    "WHERE user_id = :u AND endpoint = :e AND window_start > :since"
)
_QUOTA_ADD_SQL = text(
    "INSERT INTO rate_limit_buckets (user_id, endpoint, window_start, request_count) "
    "VALUES (:u, :e, :w, :n) "
    "ON CONFLICT (user_id, endpoint, window_start) "
    "DO UPDATE SET request_count = rate_limit_buckets.request_count + :n"
)


def _bucket(moment: datetime) -> datetime:
    seconds = int(moment.timestamp()) // _QUOTA_BUCKET_SECONDS * _QUOTA_BUCKET_SECONDS
    return datetime.fromtimestamp(seconds, tz=UTC)


class PostgresIndexQuota:
    """The quota in ``rate_limit_buckets``, shared by every process (not under RLS).

    A rolling hour at ten-minute steps: the six most recent buckets, the current one
    included, are summed.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    @staticmethod
    def _since() -> datetime:
        since = _bucket(datetime.now(UTC)) - timedelta(seconds=3_600 - _QUOTA_BUCKET_SECONDS)
        return since - timedelta(seconds=1)

    def used(self, owner_id: str) -> int:
        with self._engine.begin() as conn:
            _bound(conn)
            value = conn.execute(
                _QUOTA_USED_SQL, {"u": owner_id, "e": _QUOTA_ENDPOINT, "since": self._since()}
            ).scalar_one()
        return int(value)

    def reset_in(self, owner_id: str) -> float:
        """When the oldest counted bucket leaves the hour (it counts until its start + 1 h)."""
        with self._engine.begin() as conn:
            _bound(conn)
            oldest: datetime | None = conn.execute(
                _QUOTA_OLDEST_SQL, {"u": owner_id, "e": _QUOTA_ENDPOINT, "since": self._since()}
            ).scalar_one()
        if oldest is None:
            return 0.0
        if oldest.tzinfo is None:
            oldest = oldest.replace(tzinfo=UTC)
        return max(0.0, (oldest + timedelta(seconds=3_600) - datetime.now(UTC)).total_seconds())

    def add(self, owner_id: str, chunks: int) -> None:
        if chunks <= 0:
            return
        with self._engine.begin() as conn:
            _bound(conn)
            conn.execute(
                _QUOTA_ADD_SQL,
                {
                    "u": owner_id,
                    "e": _QUOTA_ENDPOINT,
                    "w": _bucket(datetime.now(UTC)),
                    "n": chunks,
                },
            )


def _bound(conn: Any) -> None:  # noqa: ANN401 - a SQLAlchemy connection
    """Bound this transaction's statements (fourth review finding 2). Postgres only."""
    if conn.dialect.name == "postgresql":
        conn.execute(text(f"SET LOCAL statement_timeout = {INDEX_STATEMENT_TIMEOUT_MS}"))


# ----- each owner's backlog ----------------------------------------------------------


class IndexBacklog(Protocol):
    """Each owner's retrieval documents waiting for the index, oldest first."""

    def add(self, owner_id: str, *, persona_id: str, conversation_id: str, doc_ref: str) -> None:
        """Add a document; a document already waiting keeps its place."""
        ...

    def count(self, owner_id: str) -> int: ...

    def has(self, owner_id: str, *, conversation_id: str, doc_ref: str) -> bool: ...

    def at_or_after(self, owner_id: str, cursor: BacklogEntry | None) -> BacklogEntry | None:
        """The waiting document at ``cursor`` or the first after it, else the oldest."""
        ...

    def remove(self, owner_id: str, *, conversation_id: str, doc_ref: str) -> None: ...

    def remove_conversation(self, owner_id: str, conversation_id: str) -> None:
        """Drop every waiting document of a deleted conversation."""
        ...


def _order(entry: BacklogEntry) -> tuple[datetime, str, str]:
    return (entry.added_at, entry.conversation_id, entry.doc_ref)


class InMemoryIndexBacklog:
    """The backlog in this process only: the in-process queue's, and tests'."""

    def __init__(self) -> None:
        self._entries: dict[str, dict[tuple[str, str], BacklogEntry]] = {}
        self._lock = threading.Lock()

    def add(self, owner_id: str, *, persona_id: str, conversation_id: str, doc_ref: str) -> None:
        check_index_ids(persona_id=persona_id, conversation_id=conversation_id, doc_ref=doc_ref)
        with self._lock:
            entries = self._entries.setdefault(owner_id, {})
            entries.setdefault(
                (conversation_id, doc_ref),
                BacklogEntry(persona_id, conversation_id, doc_ref, datetime.now(UTC)),
            )

    def count(self, owner_id: str) -> int:
        with self._lock:
            return len(self._entries.get(owner_id, {}))

    def has(self, owner_id: str, *, conversation_id: str, doc_ref: str) -> bool:
        with self._lock:
            return (conversation_id, doc_ref) in self._entries.get(owner_id, {})

    def at_or_after(self, owner_id: str, cursor: BacklogEntry | None) -> BacklogEntry | None:
        with self._lock:
            ordered = sorted(self._entries.get(owner_id, {}).values(), key=_order)
        if not ordered:
            return None
        if cursor is not None:
            for entry in ordered:
                if _order(entry) >= _order(cursor):
                    return entry
        return ordered[0]

    def remove(self, owner_id: str, *, conversation_id: str, doc_ref: str) -> None:
        with self._lock:
            entries = self._entries.get(owner_id)
            if entries is not None:
                entries.pop((conversation_id, doc_ref), None)
                if not entries:
                    del self._entries[owner_id]

    def remove_conversation(self, owner_id: str, conversation_id: str) -> None:
        with self._lock:
            entries = self._entries.get(owner_id)
            if entries is None:
                return
            for key in [k for k in entries if k[0] == conversation_id]:
                del entries[key]
            if not entries:
                del self._entries[owner_id]

    def owners(self) -> list[str]:
        """Every owner with a document waiting (the in-process queue's rotation)."""
        with self._lock:
            return list(self._entries)


_BACKLOG_ADD_SQL = text(
    "INSERT INTO document_index_backlog (owner_id, persona_id, conversation_id, doc_ref) "
    "VALUES (:o, :p, :c, :d) ON CONFLICT DO NOTHING"
)
_BACKLOG_COUNT_SQL = text("SELECT count(*) FROM document_index_backlog WHERE owner_id = :o")
_BACKLOG_HAS_SQL = text(
    "SELECT 1 FROM document_index_backlog "
    "WHERE owner_id = :o AND conversation_id = :c AND doc_ref = :d"
)
_BACKLOG_AT_OR_AFTER_SQL = text(
    "SELECT persona_id, conversation_id, doc_ref, added_at FROM document_index_backlog "
    "WHERE owner_id = :o AND (added_at, conversation_id, doc_ref) >= (:a, :c, :d) "
    "ORDER BY added_at, conversation_id, doc_ref LIMIT 1"
)
_BACKLOG_OLDEST_SQL = text(
    "SELECT persona_id, conversation_id, doc_ref, added_at FROM document_index_backlog "
    "WHERE owner_id = :o ORDER BY added_at, conversation_id, doc_ref LIMIT 1"
)
_BACKLOG_REMOVE_CONVERSATION_SQL = text(
    "DELETE FROM document_index_backlog WHERE owner_id = :o AND conversation_id = :c"
)
_BACKLOG_REMOVE_SQL = text(
    "DELETE FROM document_index_backlog "
    "WHERE owner_id = :o AND conversation_id = :c AND doc_ref = :d"
)


class PostgresIndexBacklog:
    """The backlog in ``document_index_backlog`` (migration 060), in the owner's RLS scope."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def _run(self, owner_id: str, statement: Any, params: dict[str, Any]) -> Any:  # noqa: ANN401
        from persona_api.db.engine import rls_connection  # noqa: PLC0415 - the db layer, late

        with rls_connection(self._engine, owner_id) as conn:
            _bound(conn)
            return conn.execute(statement, {"o": owner_id, **params}).all()

    def add(self, owner_id: str, *, persona_id: str, conversation_id: str, doc_ref: str) -> None:
        check_index_ids(persona_id=persona_id, conversation_id=conversation_id, doc_ref=doc_ref)
        self._write(
            owner_id, _BACKLOG_ADD_SQL, {"p": persona_id, "c": conversation_id, "d": doc_ref}
        )

    def _write(self, owner_id: str, statement: Any, params: dict[str, Any]) -> None:  # noqa: ANN401
        from persona_api.db.engine import rls_connection  # noqa: PLC0415 - the db layer, late

        with rls_connection(self._engine, owner_id) as conn:
            _bound(conn)
            conn.execute(statement, {"o": owner_id, **params})

    def count(self, owner_id: str) -> int:
        return int(self._run(owner_id, _BACKLOG_COUNT_SQL, {})[0][0])

    def has(self, owner_id: str, *, conversation_id: str, doc_ref: str) -> bool:
        return bool(self._run(owner_id, _BACKLOG_HAS_SQL, {"c": conversation_id, "d": doc_ref}))

    def at_or_after(self, owner_id: str, cursor: BacklogEntry | None) -> BacklogEntry | None:
        rows = []
        if cursor is not None:
            rows = self._run(
                owner_id,
                _BACKLOG_AT_OR_AFTER_SQL,
                {"a": cursor.added_at, "c": cursor.conversation_id, "d": cursor.doc_ref},
            )
        if not rows:
            rows = self._run(owner_id, _BACKLOG_OLDEST_SQL, {})
        if not rows:
            return None
        persona_id, conversation_id, doc_ref, added_at = rows[0]
        return BacklogEntry(str(persona_id), str(conversation_id), str(doc_ref), added_at)

    def remove(self, owner_id: str, *, conversation_id: str, doc_ref: str) -> None:
        self._write(owner_id, _BACKLOG_REMOVE_SQL, {"c": conversation_id, "d": doc_ref})

    def remove_conversation(self, owner_id: str, conversation_id: str) -> None:
        self._write(owner_id, _BACKLOG_REMOVE_CONVERSATION_SQL, {"c": conversation_id})


# ----- one slice -------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentIndexDeps:
    """What a slice needs, composed once per process."""

    file_storage: FileStorage
    document_store: Callable[[], DocumentStore]
    indexer: DocumentIndexer
    quota: IndexQuota
    #: Re-parses a document stored without its text (an older generated file). ``None``
    #: makes such a document fail instead.
    parser: DocumentParser | None = None
    #: Called with the owner when a document's index finishes (the sidebar ping).
    on_finished: Callable[[str], None] | None = None
    #: Each owner's waiting documents (the jobs system's lives in Postgres).
    backlog: IndexBacklog = field(default_factory=InMemoryIndexBacklog)


def run_slice(
    *,
    deps: DocumentIndexDeps,
    owner_id: str,
    persona_id: str,
    conversation_id: str,
    doc_ref: str,
    cursor: ChunkCursor,
    limit: int,
) -> SliceOutcome:
    """Embed up to ``limit`` chunks of the document from ``cursor``.

    Synchronous; :class:`DocumentIndexer` runs it on its thread, on one core.

    Raises:
        DocumentIndexUnreadableError: The document has no readable stored text.
    """
    started = time.thread_time()
    storage = deps.file_storage
    store = deps.document_store()
    fence = _fence_for(owner_id, persona_id, conversation_id, doc_ref)
    ref = document_service.find_document(
        file_storage=storage,
        owner_id=owner_id,
        persona_id=persona_id,
        conversation_id=conversation_id,
        doc_ref=doc_ref,
    )
    original_key = (
        document_service.document_original_key(
            ref, owner_id=owner_id, persona_id=persona_id, conversation_id=conversation_id
        )
        if ref is not None
        else None
    )
    if ref is None or original_key is None:
        _fenced(fence, lambda: store.delete_document(conversation_id, doc_ref), cleanup=True)
        return SliceOutcome("gone", 0, cursor, time.thread_time() - started)
    sections = document_service.load_stored_sections(storage, original_key)
    if sections is None:
        msg = "the document has no stored text to index"
        raise DocumentIndexUnreadableError(msg, context={"doc_ref": doc_ref})

    # A fresh start's delete of older chunks is the caller's, before this slice is
    # submitted (:func:`index_one_slice`): done here, an abandoned slice would run it
    # after its retry wrote (fifth review F2).
    room = max(0, min(limit, MAX_DOCUMENT_CHUNKS - cursor.index))
    duty = index_duty_cycle()
    batch_started = time.thread_time()
    batch: list[Any] = []
    written = 0
    last = cursor
    more = False
    for chunk, after in iter_document_chunks(
        sections,
        conversation_id=conversation_id,
        doc_ref=doc_ref,
        document_format=ref.format,
        title=ref.title,
        start=cursor,
    ):
        if written + len(batch) >= room:
            more = True
            break
        batch.append(chunk)
        last = after
        if len(batch) == EMBED_BATCH:
            if not _fenced(fence, functools.partial(store.write, conversation_id, batch)):
                break
            written += len(batch)
            batch = []
            _pace(time.thread_time() - batch_started, duty)
            batch_started = time.thread_time()
    if (
        batch
        and not _given_up()
        and _fenced(fence, functools.partial(store.write, conversation_id, batch))
    ):
        written += len(batch)
    meta_key = f"{original_key}{SPEC_14_SIDECAR_SUFFIX}"
    if _given_up():
        return _abandoned(
            fence, store, storage, conversation_id, doc_ref, original_key, written, cursor, started
        )

    state: Literal["indexing", "ready", "partial"]
    if not more:
        state = "ready"
    elif last.index >= MAX_DOCUMENT_CHUNKS:
        state = "partial"
    else:
        state = "indexing"
    previous = document_service.read_index_progress(storage, original_key)
    progress = IndexProgress(
        state=state,
        chunks_indexed=last.index,
        section=last.section,
        offset=last.offset,
        restarts=previous.restarts if previous is not None else 0,
    )
    if not _fenced(
        fence,
        functools.partial(document_service.write_index_progress, storage, original_key, progress),
    ):
        return _abandoned(
            fence, store, storage, conversation_id, doc_ref, original_key, written, cursor, started
        )
    # The delete removes the ``.meta.json`` before the chunks: a delete that ran during
    # this slice is seen here, after every write, and what the slice wrote goes with it.
    if not storage.exists(meta_key):
        _fenced(
            fence,
            lambda: _forget(store, storage, conversation_id, doc_ref, original_key),
            cleanup=True,
        )
        return SliceOutcome("gone", written, last, time.thread_time() - started)
    return SliceOutcome(state, written, last, time.thread_time() - started)


def _abandoned(  # noqa: PLR0913 - the slice's own state, handed over whole
    fence: threading.Lock,
    store: DocumentStore,
    storage: FileStorage,
    conversation_id: str,
    doc_ref: str,
    original_key: str,
    written: int,
    cursor: ChunkCursor,
    started: float,
) -> SliceOutcome:
    """The end of a slice its caller gave up on: its retry owns the document now.

    It writes no chunk and no progress. If the document was deleted meanwhile, it removes
    what it left behind (a deleted document has no retry to race).
    """
    if not storage.exists(f"{original_key}{SPEC_14_SIDECAR_SUFFIX}"):
        _fenced(
            fence,
            lambda: _forget(store, storage, conversation_id, doc_ref, original_key),
            cleanup=True,
        )
    return SliceOutcome("indexing", written, cursor, time.thread_time() - started)


def _forget(
    store: DocumentStore,
    storage: FileStorage,
    conversation_id: str,
    doc_ref: str,
    original_key: str,
) -> None:
    """The document was deleted while its slice ran: drop what the slice left behind."""
    store.delete_document(conversation_id, doc_ref)
    storage.delete(f"{original_key}{DOCUMENT_INDEX_SIDECAR_SUFFIX}")


def _resolve(
    deps: DocumentIndexDeps, entry: BacklogEntry, owner_id: str
) -> tuple[DocumentRef, str] | None:
    """The document and its original's key, or ``None`` if it is gone."""
    ref = document_service.find_document(
        file_storage=deps.file_storage,
        owner_id=owner_id,
        persona_id=entry.persona_id,
        conversation_id=entry.conversation_id,
        doc_ref=entry.doc_ref,
    )
    if ref is None:
        return None
    original_key = document_service.document_original_key(
        ref,
        owner_id=owner_id,
        persona_id=entry.persona_id,
        conversation_id=entry.conversation_id,
    )
    return None if original_key is None else (ref, original_key)


def _mark_failed(deps: DocumentIndexDeps, entry: BacklogEntry, owner_id: str) -> None:
    """Record ``failed`` for the document, keeping its progress. Nothing if it is gone."""
    found = _resolve(deps, entry, owner_id)
    if found is None:
        return
    _, original_key = found

    def _mark() -> None:
        previous = document_service.read_index_progress(deps.file_storage, original_key)
        document_service.write_index_progress(
            deps.file_storage,
            original_key,
            IndexProgress(
                state="failed",
                chunks_indexed=previous.chunks_indexed if previous is not None else 0,
                section=previous.section if previous is not None else 0,
                offset=previous.offset if previous is not None else 0,
                restarts=previous.restarts if previous is not None else 0,
            ),
        )

    # Ordered after any write an abandoned slice of this document still has running.
    _fenced(_fence_for(owner_id, entry.persona_id, entry.conversation_id, entry.doc_ref), _mark)


def _clear_for_a_fresh_start(deps: DocumentIndexDeps, entry: BacklogEntry, owner_id: str) -> None:
    """Delete the document's older chunks before its first slice is submitted.

    On the caller's side, under the document's fence (fifth review F2): an abandoned
    slice that later returns can then never delete what this start's slices wrote.
    """
    store = deps.document_store()
    bound = _slice_statement_timeout_ms.set(INDEX_STATEMENT_TIMEOUT_MS)
    try:
        _fenced(
            _fence_for(owner_id, entry.persona_id, entry.conversation_id, entry.doc_ref),
            lambda: store.delete_document(entry.conversation_id, entry.doc_ref),
        )
    finally:
        _slice_statement_timeout_ms.reset(bound)


async def _ensure_text(deps: DocumentIndexDeps, payload: BacklogEntry, owner_id: str) -> None:
    """Make sure the document's text is stored, re-parsing an older file that has none.

    Raises:
        DocumentIndexUnreadableError: There is no text and none can be had (permanent).
        DocumentUnreadableError: The re-parse was refused or failed for now (transient).
    """
    ref = await asyncio.to_thread(
        document_service.find_document,
        file_storage=deps.file_storage,
        owner_id=owner_id,
        persona_id=payload.persona_id,
        conversation_id=payload.conversation_id,
        doc_ref=payload.doc_ref,
    )
    if ref is None:
        return  # deleted: the slice sees it and cleans up
    original_key = document_service.document_original_key(
        ref,
        owner_id=owner_id,
        persona_id=payload.persona_id,
        conversation_id=payload.conversation_id,
    )
    if original_key is None:
        msg = "the document's file name is not its own"
        raise DocumentIndexUnreadableError(msg, context={"doc_ref": payload.doc_ref})
    stored = await asyncio.to_thread(
        document_service.load_stored_sections, deps.file_storage, original_key
    )
    if stored is not None:
        return
    if ref.text_stored or deps.parser is None:
        msg = "the document's stored text is missing"
        raise DocumentIndexUnreadableError(msg, context={"doc_ref": payload.doc_ref})
    try:
        # Re-parses through the worker pool and stores the text (finding 12's backfill).
        await document_service.get_document_text(
            file_storage=deps.file_storage,
            owner_id=owner_id,
            persona_id=payload.persona_id,
            conversation_id=payload.conversation_id,
            ref=ref,
            parser=deps.parser,
        )
    except DocumentUnreadableError as exc:
        if exc.context.get("reason") in ("original_missing", "stored_text_missing", "corrupt"):
            msg = "the document could not be read to index it"
            raise DocumentIndexUnreadableError(
                msg, context={"doc_ref": payload.doc_ref, "reason": exc.context["reason"]}
            ) from exc
        raise


def _start_cursor(
    deps: DocumentIndexDeps, entry: BacklogEntry, owner_id: str
) -> ChunkCursor | None:
    """Where the document's index resumes, from its recorded progress.

    ``None`` when there is nothing to do: the document is gone, or its index is already
    ``ready``, ``partial`` or ``failed`` (a listing read just before it finished can put it
    back in the backlog). No progress, or ``pending``, starts from the beginning.
    """
    found = _resolve(deps, entry, owner_id)
    if found is None:
        return ChunkCursor()  # the slice sees it is gone and cleans up
    _, original_key = found
    progress = document_service.read_index_progress(deps.file_storage, original_key)
    if progress is None or progress.state == "pending":
        return ChunkCursor()
    if progress.state == "indexing":
        return ChunkCursor(progress.section, progress.offset, progress.chunks_indexed)
    return None


async def index_one_slice(
    deps: DocumentIndexDeps,
    entry: BacklogEntry,
    *,
    owner_id: str,
    before_slice: Callable[[], Awaitable[None]] | None = None,
) -> SliceOutcome | None:
    """Run one slice of the document ``entry`` names. ``None`` when the owner's quota is spent.

    The slice resumes from the document's recorded progress. ``before_slice`` runs once
    the slice is certain to start (quota and text in hand). A document already finished
    comes back as a ``ready`` outcome that wrote nothing.

    Raises:
        DocumentIndexUnreadableError: Permanent; the caller marks the document failed.
        DocumentIndexerExhaustedError: The indexer is out of threads; checked before
            anything is read or marked, so nothing has changed.
    """
    await deps.indexer.ensure_ready()
    used = await asyncio.to_thread(deps.quota.used, owner_id)
    room = min(SLICE_CHUNKS, OWNER_CHUNKS_PER_HOUR - used)
    if room <= 0:
        _log.info(
            "document index waiting for its owner's quota ref={} used={}", entry.doc_ref, used
        )
        return None
    cursor = await asyncio.to_thread(_start_cursor, deps, entry, owner_id)
    if cursor is None:
        return SliceOutcome("ready", 0, ChunkCursor(), 0.0)
    await _ensure_text(deps, entry, owner_id)
    if before_slice is not None:
        await before_slice()
    if cursor == ChunkCursor():
        await asyncio.to_thread(_clear_for_a_fresh_start, deps, entry, owner_id)
    outcome = await deps.indexer.run(
        functools.partial(
            run_slice,
            deps=deps,
            owner_id=owner_id,
            persona_id=entry.persona_id,
            conversation_id=entry.conversation_id,
            doc_ref=entry.doc_ref,
            cursor=cursor,
            limit=room,
        ),
        wall_seconds=slice_wall_seconds(index_duty_cycle()),
        label=f"owner={owner_id} ref={entry.doc_ref}",
    )
    await asyncio.to_thread(deps.quota.add, owner_id, outcome.chunks_written)
    _log.info(
        "document index slice ref={} chunks={} total={} state={} cpu_s={:.2f}",
        entry.doc_ref,
        outcome.chunks_written,
        outcome.cursor.index,
        outcome.state,
        outcome.cpu_seconds,
    )
    if outcome.state in ("ready", "partial") and deps.on_finished is not None:
        deps.on_finished(owner_id)
    return outcome


@dataclass(frozen=True)
class TurnResult:
    """What one owner turn did, and what comes next for that owner.

    ``parked_for`` is set when the owner's quota is spent: the next turn waits that many
    seconds. ``cursor`` is the document the next turn starts at; ``more`` says whether the
    owner has documents waiting at all.
    """

    more: bool
    cursor: BacklogEntry | None = None
    parked_for: float | None = None
    worked: bool = False


async def run_owner_turn(
    deps: DocumentIndexDeps,
    owner_id: str,
    cursor: BacklogEntry | None,
    *,
    last_attempt: bool = False,
) -> TurnResult:
    """One turn of ``owner_id``'s index: at most one slice that embeds, then stop.

    Starts at ``cursor`` in the owner's backlog. A document found gone or already
    finished costs no embedding: it leaves the backlog and the turn moves on, up to
    :data:`_TURN_SKIP_LIMIT` of them. A document with no readable text is marked
    ``failed`` and leaves. On ``last_attempt`` the document is marked ``failed`` before
    its slice runs, so a worker that dies during it leaves the truth behind; a slice that
    succeeds overwrites it, and one that raises leaves the document failed and out of the
    backlog before the error is raised.

    Raises:
        Exception: Whatever the slice raised, for the caller's retry.
    """
    backlog = deps.backlog
    entry = await asyncio.to_thread(backlog.at_or_after, owner_id, cursor)
    skipped = 0
    while entry is not None:
        current = entry

        async def _last_try_says_failed(target: BacklogEntry = current) -> None:
            await asyncio.to_thread(_mark_failed, deps, target, owner_id)

        try:
            outcome = await index_one_slice(
                deps,
                current,
                owner_id=owner_id,
                before_slice=_last_try_says_failed if last_attempt else None,
            )
        except DocumentIndexUnreadableError:
            await asyncio.to_thread(_mark_failed, deps, current, owner_id)
            outcome = SliceOutcome("gone", 0, ChunkCursor(), 0.0)
        except DocumentIndexerExhaustedError:
            raise  # a lane pause: the document keeps its place and its state
        except Exception:
            if last_attempt:
                await asyncio.to_thread(
                    backlog.remove,
                    owner_id,
                    conversation_id=current.conversation_id,
                    doc_ref=current.doc_ref,
                )
            raise
        if outcome is None:
            reset = await asyncio.to_thread(deps.quota.reset_in, owner_id)
            return TurnResult(
                more=True, cursor=current, parked_for=min(max(reset, 1.0), QUOTA_RETRY_SECONDS)
            )
        if outcome.state != "indexing":
            await asyncio.to_thread(
                backlog.remove,
                owner_id,
                conversation_id=current.conversation_id,
                doc_ref=current.doc_ref,
            )
        if outcome.chunks_written > 0 or outcome.state == "indexing":
            following = await asyncio.to_thread(backlog.at_or_after, owner_id, current)
            return TurnResult(more=following is not None, cursor=following, worked=True)
        skipped += 1
        if skipped >= _TURN_SKIP_LIMIT:
            following = await asyncio.to_thread(backlog.at_or_after, owner_id, current)
            return TurnResult(more=following is not None, cursor=following)
        entry = await asyncio.to_thread(backlog.at_or_after, owner_id, current)
    return TurnResult(more=False)


# ----- the A0 job --------------------------------------------------------------------


def _payload_for(turn: int, cursor: BacklogEntry | None) -> DocumentIndexJobPayload:
    if cursor is None:
        return DocumentIndexJobPayload(turn=turn)
    return DocumentIndexJobPayload(
        turn=turn,
        cursor_added_at=cursor.added_at,
        cursor_conversation_id=cursor.conversation_id,
        cursor_doc_ref=cursor.doc_ref,
    )


def _cursor_of(payload: DocumentIndexJobPayload) -> BacklogEntry | None:
    if payload.cursor_conversation_id is None or payload.cursor_doc_ref is None:
        return None
    return BacklogEntry(
        persona_id="",
        conversation_id=payload.cursor_conversation_id,
        doc_ref=payload.cursor_doc_ref,
        added_at=payload.cursor_added_at or datetime.fromtimestamp(0, tz=UTC),
    )


class DocumentIndexHandler:
    """The ``document_ingest`` handler: one owner turn, then that owner's next turn."""

    def __init__(
        self,
        *,
        deps: DocumentIndexDeps,
        enqueue: Callable[[str, DocumentIndexJobPayload, datetime | None], None],
        attempt_of: Callable[[JobContext], tuple[int, int]],
    ) -> None:
        self._deps = deps
        self._enqueue = enqueue
        self._attempt_of = attempt_of

    async def handle(self, payload: DocumentIndexJobPayload, context: JobContext) -> None:
        owner_id = context.owner_id
        attempt, max_attempts = await asyncio.to_thread(self._attempt_of, context)
        try:
            result = await run_owner_turn(
                self._deps, owner_id, _cursor_of(payload), last_attempt=attempt >= max_attempts
            )
        except DocumentIndexerExhaustedError as exc:
            # A lane pause, not this owner's failure: the same turn, later, at the same
            # attempt (fifth review F1).
            msg = "the document indexer is paused"
            raise JobDeferredError(
                msg, delay_seconds=INDEXER_PAUSE_SECONDS, context=dict(exc.context)
            ) from exc
        except Exception as exc:
            if attempt >= max_attempts:
                # The document is failed and out of the backlog: the owner's other
                # documents carry on in a turn of their own.
                if await asyncio.to_thread(self._deps.backlog.count, owner_id):
                    await asyncio.to_thread(
                        self._enqueue, owner_id, _payload_for(payload.turn + 1, None), None
                    )
                msg = "the document could not be indexed"
                raise PermanentJobError(msg, context={"error": type(exc).__name__}) from exc
            raise
        if not result.more:
            return
        later = (
            datetime.now(UTC) + timedelta(seconds=result.parked_for)
            if result.parked_for is not None
            else None
        )
        await asyncio.to_thread(
            self._enqueue, owner_id, _payload_for(payload.turn + 1, result.cursor), later
        )


_ATTEMPT_SQL = text("SELECT attempt, max_attempts FROM jobs WHERE id = :id")


def _job_attempt(context: JobContext) -> tuple[int, int]:
    """This job's attempt and its limit, read through the owner's own connection."""
    with context.connection() as conn:
        row = conn.execute(_ATTEMPT_SQL, {"id": context.job_id}).first()
    if row is None:
        return 1, _RETRY.max_attempts
    return int(row[0]), min(int(row[1]), _RETRY.max_attempts)


_OWNER_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtextextended(:k, 0))")
_OWNER_QUEUED_SQL = text(
    "SELECT 1 FROM jobs WHERE owner_id = :o AND type = :t AND state = 'queued' LIMIT 1"
)
_OWNER_REPRIORITISE_SQL = text(
    "UPDATE jobs SET priority = :p WHERE owner_id = :o AND type = :t AND state = 'queued'"
)


def _served_priority(conn: Any, owner_id: str) -> int:  # noqa: ANN401 - a SQLAlchemy connection
    """The owner's turn priority: minus the chunks it was served in the last hour.

    Least served first (fifth review F3, the orchestrator's ruling): the claim takes the
    highest priority, then the earliest, so when the lane frees, the owner with the fewest
    chunks indexed in the hour goes next, and owners served alike take turns in order. A
    new or idle owner's first slice is never behind busy owners.
    """
    served = conn.execute(
        _QUOTA_USED_SQL,
        {"u": owner_id, "e": _QUOTA_ENDPOINT, "since": PostgresIndexQuota._since()},
    ).scalar_one()
    return -int(served)


def enqueue_owner_turn(
    engine: Engine,
    owner_id: str,
    payload: DocumentIndexJobPayload | None = None,
    scheduled_at: datetime | None = None,
) -> bool:
    """Queue the owner's next index turn unless one is already queued. True if queued.

    One transaction in the owner's RLS scope, under a per-owner advisory lock, so two
    callers cannot both see "none queued" and both insert. The turn that is running is
    not ``queued``, so it can queue its own successor; a retry of a failed attempt goes
    back to ``queued`` on its own row, which can make two for that owner, never more.

    The turn's priority is minus the chunks the owner was served in the last hour
    (:func:`_served_priority`); an owner's queued turn has its priority refreshed instead.
    Other job types keep priority 0, so a busy owner's turn is claimed after due jobs of
    other types in the same claim; the index is background work, and serial, so this
    only orders it within a claim.
    """
    from persona_api.db.engine import rls_connection  # noqa: PLC0415 - the db layer, late
    from persona_api.db.models import jobs  # noqa: PLC0415

    values: dict[str, Any] = {
        "type": DOCUMENT_INDEX_JOB_TYPE,
        "owner_id": owner_id,
        "payload": (payload or DocumentIndexJobPayload()).model_dump(mode="json"),
        "idempotency_key": document_index_idempotency_key(owner_id),
        "max_attempts": _RETRY.max_attempts,
    }
    if scheduled_at is not None:
        values["scheduled_at"] = scheduled_at
    with rls_connection(engine, owner_id) as conn:
        _bound(conn)
        conn.execute(_OWNER_LOCK_SQL, {"k": f"{DOCUMENT_INDEX_JOB_TYPE}:{owner_id}"})
        priority = _served_priority(conn, owner_id)
        if conn.execute(_OWNER_QUEUED_SQL, {"o": owner_id, "t": DOCUMENT_INDEX_JOB_TYPE}).first():
            conn.execute(
                _OWNER_REPRIORITISE_SQL,
                {"o": owner_id, "t": DOCUMENT_INDEX_JOB_TYPE, "p": priority},
            )
            return False
        conn.execute(jobs.insert().values(**values, priority=priority))
    return True


def register_document_index_handler(
    registry: JobRegistry,
    *,
    deps: DocumentIndexDeps,
    rls_engine: Engine,
) -> None:
    """Register ``document_ingest``: one job per owner turn, serial, on the index's ladder.

    The jobs path keeps its backlog in Postgres on ``rls_engine``, the same place
    :func:`enqueue_document_index` writes to, whatever backlog ``deps`` carried.
    """
    deps = dataclasses.replace(deps, backlog=PostgresIndexBacklog(rls_engine))

    def _enqueue(
        owner_id: str, payload: DocumentIndexJobPayload, scheduled_at: datetime | None
    ) -> None:
        enqueue_owner_turn(rls_engine, owner_id, payload, scheduled_at)

    registry.register(
        JobTypeSpec(
            type=DOCUMENT_INDEX_JOB_TYPE,
            payload_model=DocumentIndexJobPayload,
            handler=DocumentIndexHandler(deps=deps, enqueue=_enqueue, attempt_of=_job_attempt),
            idempotency_key=lambda _payload: document_index_idempotency_key("turn"),
            retry=_RETRY,
            lease=MEDIUM_LEASE,
            serial=True,
        )
    )


# ----- scheduling ------------------------------------------------------------------


def enqueue_document_index(
    queue: JobQueue, *, owner_id: str, persona_id: str, conversation_id: str, doc_ref: str
) -> None:
    """Put a document in its owner's backlog and make sure the owner has a turn queued."""
    engine = queue._engine  # noqa: SLF001 - the queue's own engine and RLS contract
    PostgresIndexBacklog(engine).add(
        owner_id, persona_id=persona_id, conversation_id=conversation_id, doc_ref=doc_ref
    )
    enqueue_owner_turn(engine, owner_id)


class DocumentIndexScheduler(Protocol):
    """Starts a document's index, and picks up documents whose index is not running."""

    async def schedule(
        self, *, owner_id: str, persona_id: str, conversation_id: str, doc_ref: str
    ) -> None: ...

    async def pick_up(
        self,
        *,
        owner_id: str,
        persona_id: str,
        conversation_id: str,
        refs: Iterable[DocumentRef],
    ) -> None: ...

    def admit(self, owner_id: str) -> None:
        """Refuse one more waiting document past :data:`MAX_PENDING_DOCUMENTS_PER_OWNER`.

        Raises:
            DocumentIndexBacklogFullError: The owner's backlog is full.
        """
        ...

    async def forget_conversation(self, *, owner_id: str, conversation_id: str) -> None:
        """A conversation is being deleted: none of its documents wait any longer."""
        ...


def _waiting(refs: Iterable[DocumentRef], states: tuple[str, ...]) -> list[DocumentRef]:
    return [r for r in refs if r.strategy == IngestStrategy.RETRIEVAL and r.index_state in states]


def _room_for(backlog: IndexBacklog, owner_id: str, conversation_id: str, doc_ref: str) -> bool:
    """Whether a pick-up may put this document in the backlog: it is there, or there is room."""
    if backlog.has(owner_id, conversation_id=conversation_id, doc_ref=doc_ref):
        return True
    if backlog.count(owner_id) >= MAX_PENDING_DOCUMENTS_PER_OWNER:
        _log.info("document index backlog full; the pick-up puts back the rest later")
        return False
    return True


def _admit(backlog: IndexBacklog, owner_id: str) -> None:
    waiting = backlog.count(owner_id)
    if waiting >= MAX_PENDING_DOCUMENTS_PER_OWNER:
        msg = "too many of your documents are still waiting to be indexed; try again later"
        raise DocumentIndexBacklogFullError(
            msg,
            context={
                "reason": "index_backlog_full",
                "waiting": str(waiting),
                "retry_after_seconds": "60",
            },
        )


class _PickUpThrottle:
    """At most one pick-up of a conversation per :data:`PICK_UP_INTERVAL_SECONDS` here."""

    _MAX_ENTRIES = 10_000

    def __init__(self) -> None:
        self._last: dict[tuple[str, str], float] = {}

    def allow(self, owner_id: str, conversation_id: str) -> bool:
        now = time.monotonic()
        key = (owner_id, conversation_id)
        last = self._last.get(key)
        if last is not None and now - last < PICK_UP_INTERVAL_SECONDS:
            return False
        if len(self._last) >= self._MAX_ENTRIES:
            horizon = now - PICK_UP_INTERVAL_SECONDS
            self._last = {k: v for k, v in self._last.items() if v >= horizon}
        self._last[key] = now
        return True


class JobDocumentIndexScheduler:
    """Schedules through the A0 jobs system (cloud, and community with the worker)."""

    def __init__(self, queue: JobQueue, file_storage: FileStorage) -> None:
        self._engine = queue._engine  # noqa: SLF001 - the queue's own engine and RLS contract
        self._backlog = PostgresIndexBacklog(self._engine)
        self._file_storage = file_storage
        self._throttle = _PickUpThrottle()

    async def schedule(
        self, *, owner_id: str, persona_id: str, conversation_id: str, doc_ref: str
    ) -> None:
        await asyncio.to_thread(
            self._backlog.add,
            owner_id,
            persona_id=persona_id,
            conversation_id=conversation_id,
            doc_ref=doc_ref,
        )
        await asyncio.to_thread(enqueue_owner_turn, self._engine, owner_id)

    def admit(self, owner_id: str) -> None:
        _admit(self._backlog, owner_id)

    async def forget_conversation(self, *, owner_id: str, conversation_id: str) -> None:
        await asyncio.to_thread(self._backlog.remove_conversation, owner_id, conversation_id)

    async def pick_up(
        self,
        *,
        owner_id: str,
        persona_id: str,
        conversation_id: str,
        refs: Iterable[DocumentRef],
    ) -> None:
        """Put back in the backlog each document still ``pending`` or ``indexing``.

        A document whose turn chain was lost (a request cancelled between its store and
        its schedule, the last continuation's enqueue failing, a file generated before
        the index existed) is waiting again, and the owner gets a turn. A document still
        in the backlog keeps its place. Throttled per conversation. The backlog cap holds
        here too (fifth review): past :data:`MAX_PENDING_DOCUMENTS_PER_OWNER` waiting, the
        rest are put back by a later pick-up, once there is room.
        """
        waiting = _waiting(refs, ("pending", "indexing"))
        if not waiting or not self._throttle.allow(owner_id, conversation_id):
            return
        for ref in waiting:
            if not await asyncio.to_thread(
                _room_for, self._backlog, owner_id, conversation_id, ref.doc_ref
            ):
                break
            try:
                await asyncio.to_thread(
                    self._backlog.add,
                    owner_id,
                    persona_id=persona_id,
                    conversation_id=conversation_id,
                    doc_ref=ref.doc_ref,
                )
            except ValueError:
                _log.warning("a document id the index refuses: {}", ref.doc_ref)
        await asyncio.to_thread(enqueue_owner_turn, self._engine, owner_id)


#: Restarts after which an index the local queue keeps losing is marked failed.
_LOCAL_MAX_RESTARTS = 3

#: Attempts the local queue gives one turn.
_LOCAL_MAX_ATTEMPTS = 3


class LocalDocumentIndexQueue:
    """The same owner turns without a jobs system: an in-process rotation of owners.

    For a community boot without the in-process worker. Owners take turns, one slice at a
    time, the owner served the fewest chunks this hour first (ties in turn order), oldest
    document first within an owner; an owner over its quota is parked until
    its reset and then rejoins the rotation. Nothing here is durable: a document left
    ``indexing`` by a restart is resumed from its recorded progress when the owner next
    opens or uses its conversation (:meth:`pick_up`), and is marked ``failed`` after
    :data:`_LOCAL_MAX_RESTARTS` such resumes.
    """

    def __init__(self, deps: DocumentIndexDeps) -> None:
        self._deps = deps
        self._backlog = deps.backlog
        self._rotation: deque[str] = deque()
        self._cursors: dict[str, BacklogEntry | None] = {}
        self._attempts: dict[str, int] = {}
        self._parked: set[str] = set()
        self._wake = asyncio.Event()
        self._idle = asyncio.Event()
        self._idle.set()
        self._task: asyncio.Task[None] | None = None
        self._timers: set[asyncio.TimerHandle] = set()

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def aclose(self) -> None:
        for timer in self._timers:
            timer.cancel()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def join(self) -> None:
        """Wait until no owner is in the rotation and nothing runs (tests, a clean stop)."""
        while self._rotation or not self._idle.is_set():
            await self._idle.wait()
            await asyncio.sleep(0)

    def _rejoin(self, owner_id: str) -> None:
        if owner_id not in self._rotation and owner_id not in self._parked:
            self._rotation.append(owner_id)
            self._idle.clear()
            self._wake.set()

    def admit(self, owner_id: str) -> None:
        _admit(self._backlog, owner_id)

    async def forget_conversation(self, *, owner_id: str, conversation_id: str) -> None:
        self._backlog.remove_conversation(owner_id, conversation_id)

    async def schedule(
        self, *, owner_id: str, persona_id: str, conversation_id: str, doc_ref: str
    ) -> None:
        self._backlog.add(
            owner_id, persona_id=persona_id, conversation_id=conversation_id, doc_ref=doc_ref
        )
        self._rejoin(owner_id)

    async def pick_up(
        self,
        *,
        owner_id: str,
        persona_id: str,
        conversation_id: str,
        refs: Iterable[DocumentRef],
    ) -> None:
        """Queue each waiting document this process is not already indexing."""
        for ref in _waiting(refs, ("pending", "indexing")):
            if self._backlog.has(owner_id, conversation_id=conversation_id, doc_ref=ref.doc_ref):
                continue
            if not _room_for(self._backlog, owner_id, conversation_id, ref.doc_ref):
                break  # the cap holds here too; a later pick-up puts back the rest
            original_key = document_service.document_original_key(
                ref, owner_id=owner_id, persona_id=persona_id, conversation_id=conversation_id
            )
            if original_key is None:
                continue
            progress = await asyncio.to_thread(
                document_service.read_index_progress, self._deps.file_storage, original_key
            )
            if progress is not None and progress.state == "indexing":
                restarts = progress.restarts + 1
                state = "failed" if restarts >= _LOCAL_MAX_RESTARTS else progress.state
                await asyncio.to_thread(
                    document_service.write_index_progress,
                    self._deps.file_storage,
                    original_key,
                    progress.model_copy(update={"restarts": restarts, "state": state}),
                )
                if state == "failed":
                    _log.warning(
                        "document index lost {} times, now failed: {}", restarts, ref.doc_ref
                    )
                    continue
            await self.schedule(
                owner_id=owner_id,
                persona_id=persona_id,
                conversation_id=conversation_id,
                doc_ref=ref.doc_ref,
            )

    async def _run(self) -> None:
        while True:
            if not self._rotation:
                self._idle.set()
                self._wake.clear()
                await self._wake.wait()
                continue
            owner_id = await self._least_served()
            try:
                await self._turn(owner_id)
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - one owner's failure never stops the queue
                _log.exception("document index turn failed owner={}", owner_id)

    async def _least_served(self) -> str:
        """The owner in the rotation served the fewest chunks this hour; ties in turn order."""
        waiting = list(self._rotation)
        served = [await asyncio.to_thread(self._deps.quota.used, owner) for owner in waiting]
        owner_id = waiting[served.index(min(served))]
        self._rotation.remove(owner_id)
        return owner_id

    async def _turn(self, owner_id: str) -> None:
        attempt = self._attempts.get(owner_id, 0) + 1
        try:
            result = await run_owner_turn(
                self._deps,
                owner_id,
                self._cursors.get(owner_id),
                last_attempt=attempt >= _LOCAL_MAX_ATTEMPTS,
            )
        except DocumentIndexerExhaustedError:
            # A lane pause: the owner comes back later at the same attempt (fifth review F1).
            self._later(INDEXER_PAUSE_SECONDS, owner_id)
            return
        except Exception:
            if attempt >= _LOCAL_MAX_ATTEMPTS:
                self._attempts.pop(owner_id, None)
                self._cursors.pop(owner_id, None)
                self._rejoin(owner_id)
            else:
                self._attempts[owner_id] = attempt
                self._later(_RETRY.backoff_for(attempt), owner_id)
            raise
        self._attempts.pop(owner_id, None)
        self._cursors[owner_id] = result.cursor
        if not result.more:
            self._cursors.pop(owner_id, None)
        elif result.parked_for is not None:
            self._later(result.parked_for, owner_id)
        else:
            self._rejoin(owner_id)

    def _later(self, delay: float, owner_id: str) -> None:
        loop = asyncio.get_running_loop()
        self._parked.add(owner_id)

        def _fire() -> None:
            self._timers.discard(timer)
            self._parked.discard(owner_id)
            self._rejoin(owner_id)

        timer = loop.call_later(delay, _fire)
        self._timers.add(timer)
