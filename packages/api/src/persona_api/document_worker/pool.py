"""The api side of untrusted document parsing: a small pool of worker processes (D-FV0-31).

Every untrusted parse (an upload, and the re-parse of an older whole-inject document
that has no stored text) runs in a worker process started by this pool:

- **A fresh interpreter.** ``python -P -m persona_api.document_worker.worker``, never a
  fork of the api. Its environment is an allowlist (:data:`WORKER_ENV_ALLOWLIST`), so it
  holds no database URL, provider key or other secret. It gets a path and plain data,
  and answers with plain data; it never sees the database, the storage backend or the
  RLS context. Persisting, ingesting and auditing stay in the api.
- **Limits.** ``RLIMIT_AS`` (:data:`MEMORY_LIMIT_BYTES`), ``RLIMIT_CPU`` per job
  (:data:`CPU_SECONDS`) and no core dumps, set by the worker on itself where the platform
  allows (see :mod:`persona_api.document_worker.worker`), and a wall-clock deadline
  (:data:`DEADLINE_SECONDS`) after which this side kills it, on every platform.
- **Recycling.** A worker is replaced after :data:`MAX_JOBS_PER_WORKER` jobs and after
  any crash, kill, timeout or memory failure. Workers start lazily, on first use.
- **One owner per worker, and a spare.** A worker serves one owner for its whole life.
  One spare worker is kept started with no owner, so a job for a new owner takes it
  (bound to that owner from then on) instead of waiting for a fresh interpreter, and
  another spare starts in the background (third security review finding 13).
- **Bounded waiting.** With every worker busy, a parse waits in a queue at most
  :data:`QUEUE_DEPTH` deep: an upload for at most the upload wait
  (:data:`UPLOAD_WAIT_SECONDS`, settable with :data:`UPLOAD_WAIT_ENV_VAR`), a re-parse
  for at most :data:`REPARSE_WAIT_SECONDS`. A full queue or an expired wait raises
  :class:`DocumentParserBusyError` (503 with ``Retry-After``). Owners take turns; an
  owner whose file hit a limit in the last ten minutes goes after every other owner.
- **One upload per user.** A second upload parse from the same owner while one is in
  flight raises :class:`DocumentUploadInProgressError` (429). Re-parses are exempt.
- **Charged by CPU.** Each job charges its owner the CPU seconds the worker used for it,
  read from ``/proc/<pid>/stat`` on Linux. Where there is no ``/proc`` (macOS, Windows)
  the job's wall seconds are charged instead, which over-charges an owner whose job
  waited for a busy CPU. A job killed at the wall-clock deadline is charged its wall
  seconds everywhere: it held the worker that long.
- **No default-executor thread.** The blocking pipe read happens on a thread pool this
  pool owns, one thread per worker, so the api's default executor is never held by a
  parse.

The worker runs as the same OS user as the api. A worker compromised through a bug in
native parsing code could read what that user can read and return it as text or page
images; a separate user or a syscall filter is the next step and is not part of this.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import functools
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import weakref
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from persona.documents.errors import (
    CorruptDocumentError,
    MissingDependencyError,
    UnsupportedFormatError,
)
from persona.documents.parsers import (
    MAX_DOCUMENT_SECTIONS,
    ParseResult,
    cap_document_text,
)
from persona.documents.parsers.csv import ROW_CAP_ENV_VAR
from persona.documents.parsers.pdf import NO_TEXT_LAYER_THRESHOLD_ENV_VAR
from persona.errors import PersonaError
from persona.logging import get_logger

from persona_api.document_worker.admission import (
    DocumentAdmission,
    DocumentParseBudgetError,
    DocumentParseCooldownError,
    DocumentUploadInProgressError,
    UploadLease,
)
from persona_api.document_worker.protocol import read_frame, write_frame
from persona_api.document_worker.untrusted import ParsedDocument, resolve_raster_dpi

if TYPE_CHECKING:
    from collections.abc import Mapping
    from concurrent.futures import Future

__all__ = [
    "CPU_SECONDS",
    "DEADLINE_SECONDS",
    "DEFAULT_WORKERS",
    "MAX_JOBS_PER_WORKER",
    "MEMORY_LIMIT_BYTES",
    "QUEUE_DEPTH",
    "REPARSE_WAIT_SECONDS",
    "RETRY_AFTER_SECONDS",
    "UPLOAD_WAIT_ENV_VAR",
    "UPLOAD_WAIT_SECONDS",
    "WORKERS_ENV_VAR",
    "WORKER_ENV_ALLOWLIST",
    "WORKER_MODULE",
    "DocumentParseBudgetError",
    "DocumentParseCooldownError",
    "DocumentParserBusyError",
    "DocumentParserUnavailableError",
    "DocumentUploadInProgressError",
    "ParsePurpose",
    "ProcessDocumentParser",
    "upload_wait_from_env",
    "worker_environment",
    "workers_from_env",
]

_log = get_logger("api.document_worker")

#: Why a parse is run: an upload (one per user at a time) or the re-parse of a stored
#: document that has no extracted-text sidecar (exempt from the per-user limit).
ParsePurpose = Literal["upload", "reparse"]

#: The worker's module, run with ``python -P -m``.
WORKER_MODULE: str = "persona_api.document_worker.worker"

#: Env var for the number of worker processes.
WORKERS_ENV_VAR: str = "PERSONA_DOC_PARSER_WORKERS"

#: One worker: the production VM has 2 GB, and one worker may use up to
#: :data:`MEMORY_LIMIT_BYTES`.
DEFAULT_WORKERS: int = 1

#: Address-space limit per worker. Measured on genuine large files (D-FV0-31): the
#: largest peak was 531 MB of address space (316 MB resident) with the old heavy
#: import, and 606 MB for the worst file the parsers' own bounds let through.
MEMORY_LIMIT_BYTES: int = 1024 * 1024 * 1024

#: CPU seconds per job: about 1.5 times the worst genuine file (19.5 s of CPU,
#: D-FV0-31 measurements).
CPU_SECONDS: int = 30

#: Wall-clock seconds per job before this side kills the worker. Twice the CPU limit,
#: because a shared CPU can be throttled to half speed and wall time stretches.
DEADLINE_SECONDS: float = 60.0

#: Jobs a worker serves before it is replaced.
MAX_JOBS_PER_WORKER: int = 25

#: Parses that may wait for a busy pool; one more is refused at once.
QUEUE_DEPTH: int = 4

#: The longest an upload waits for a worker before it is refused. Fly's proxy closes
#: a connection that carries no bytes for about 60 s (research A11-R-2), and an upload
#: sends nothing back until it is parsed and stored, so the wait, a CPU-bound parse up
#: to :data:`CPU_SECONDS` and the store must fit under 55 s: 15 + 30 + a few. A longer
#: wait would serve more uploads behind a slow job instead of refusing them, but a
#: request the proxy cuts is lost after its parse has been paid for.
UPLOAD_WAIT_SECONDS: float = 15.0

#: Env var for the upload wait, in seconds (1 to 300). Raise it only behind a proxy
#: that keeps a silent request open longer.
UPLOAD_WAIT_ENV_VAR: str = "PERSONA_DOC_PARSER_UPLOAD_WAIT_SECONDS"

#: The longest a re-parse waits for a worker before the turn goes without that text.
REPARSE_WAIT_SECONDS: float = 5.0

#: The ``Retry-After`` a busy refusal carries.
RETRY_AFTER_SECONDS: int = 10

#: The longest a worker may take to start and report ready.
START_TIMEOUT_SECONDS: float = 30.0

#: The only environment variables a worker inherits. Process plumbing the interpreter
#: needs, plus the two parser tuning knobs the parsers read from the environment.
#: Nothing else crosses, so no secret can (a test builds the secret names from the
#: settings and from ``.env.example`` and checks each one is withheld).
WORKER_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "TEMP",
        "TMP",
        "TMPDIR",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "PYTHONPATH",
        ROW_CAP_ENV_VAR,
        NO_TEXT_LAYER_THRESHOLD_ENV_VAR,
    }
)

#: The page image names the worker may return: ``page-NNNN.png`` in the render directory.
_PAGE_IMAGE_NAME = re.compile(r"^page-\d{4}\.png$")

_SIGXCPU: int | None = getattr(signal, "SIGXCPU", None)


class DocumentParserBusyError(PersonaError):
    """Every worker is busy and the queue is full, or the wait for one expired.

    ``context`` carries ``reason`` (``queue_full`` / ``wait_expired``) and
    ``retry_after_seconds``.
    """


class DocumentParserUnavailableError(PersonaError):
    """A worker could not be started, or the pool is closed."""


def worker_environment(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment a worker gets: only :data:`WORKER_ENV_ALLOWLIST`."""
    return {key: value for key, value in environ.items() if key.upper() in WORKER_ENV_ALLOWLIST}


def workers_from_env() -> int:
    """The worker count from :data:`WORKERS_ENV_VAR` (1 to 8), else :data:`DEFAULT_WORKERS`."""
    raw = os.environ.get(WORKERS_ENV_VAR)
    if not raw:
        return DEFAULT_WORKERS
    try:
        count = int(raw)
    except ValueError:
        count = 0
    if not 1 <= count <= 8:
        _log.warning("{} must be 1 to 8; using {}", WORKERS_ENV_VAR, DEFAULT_WORKERS)
        return DEFAULT_WORKERS
    return count


def upload_wait_from_env() -> float:
    """The upload wait from :data:`UPLOAD_WAIT_ENV_VAR` (1 to 300 s), else the default."""
    raw = os.environ.get(UPLOAD_WAIT_ENV_VAR)
    if not raw:
        return UPLOAD_WAIT_SECONDS
    try:
        seconds = float(raw)
    except ValueError:
        seconds = 0.0
    if not 1 <= seconds <= 300:  # also refuses nan
        _log.warning("{} must be 1 to 300; using {}", UPLOAD_WAIT_ENV_VAR, UPLOAD_WAIT_SECONDS)
        return UPLOAD_WAIT_SECONDS
    return seconds


_CLOCK_TICKS: int | None = None


def _process_cpu_seconds(pid: int) -> float | None:
    """The CPU seconds (user plus system) process ``pid`` has used, or ``None``.

    Read from ``/proc/<pid>/stat``, so Linux only; ``None`` elsewhere, or when the
    process is gone. Readable for a killed process until it is reaped.
    """
    global _CLOCK_TICKS  # noqa: PLW0603 (one sysconf call, cached)
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
        if _CLOCK_TICKS is None:
            _CLOCK_TICKS = os.sysconf("SC_CLK_TCK")
        # The command name is in parentheses and may hold spaces: count after its end.
        fields = raw[raw.rindex(b")") + 2 :].split()
        return (int(fields[11]) + int(fields[12])) / _CLOCK_TICKS
    except (OSError, ValueError, IndexError, AttributeError):
        return None


@dataclass
class _Worker:
    process: subprocess.Popen[bytes]
    #: An empty directory the worker runs in, removed with it.
    workdir: str = ""
    jobs_done: int = 0
    #: The owner whose jobs this worker serves; it never serves another (F5). A spare
    #: has none until the job that takes it.
    owner_id: str | None = None
    #: The worker's CPU seconds when it was killed, read before it was reaped.
    cpu_at_kill: float | None = None


@dataclass
class _Slot:
    """One worker's place in the pool. Only the thread running its job touches it."""

    worker: _Worker | None = None


def _kill(worker: _Worker | None) -> int | None:
    """Kill a worker and wait for it; its exit status."""
    if worker is None:
        return None
    process = worker.process
    if process.returncode is None:
        # Read before the wait reaps it, after which its pid may name another process.
        worker.cpu_at_kill = _process_cpu_seconds(process.pid)
    with contextlib.suppress(OSError):
        process.kill()
    for stream in (process.stdin, process.stdout):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()
    with contextlib.suppress(subprocess.TimeoutExpired):
        process.wait(timeout=5)
    _remove_workdir(worker)
    return process.returncode


def _remove_workdir(worker: _Worker) -> None:
    if worker.workdir:
        shutil.rmtree(worker.workdir, ignore_errors=True)


def _hit_limit(outcome: dict[str, Any]) -> bool:
    """Whether a job ended at a real worker limit: its owner then cools down.

    The deadline, the CPU or memory limit, a crash, and a reply the api could not read
    or that was too large. Not an unexpected error inside the worker (``internal``: for
    one, its render directory was removed under it when its request was cancelled),
    and not a failure in the api (``api_error``): neither is the file's doing.
    """
    if outcome.get("kind") == "failed":
        return outcome.get("failure") != "api_error"
    return outcome.get("kind") == "error" and outcome.get("error") in (
        "memory_limit",
        "reply_too_large",
    )


def _clear_render_dir(render_dir: object) -> None:
    """Empty a failed job's render directory: a killed render can leave pages behind.

    Entries are removed without following links; a directory the worker made is removed
    with ``shutil.rmtree``, which does not follow links inside it either.
    """
    if not isinstance(render_dir, str):
        return
    try:
        entries = list(os.scandir(render_dir))
    except OSError:
        return
    for entry in entries:
        with contextlib.suppress(OSError):
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path)
            else:
                Path(entry.path).unlink()


def _kill_all(slots: tuple[_Slot, ...]) -> None:
    for slot in slots:
        worker, slot.worker = slot.worker, None
        _kill(worker)


class ProcessDocumentParser:
    """Runs every untrusted parse in a limited, killable worker process.

    Implements :class:`persona_api.services.document_parsing.DocumentParser`. The
    defaults are the production values; tests pass smaller ones.
    """

    def __init__(
        self,
        *,
        workers: int | None = None,
        memory_limit_bytes: int = MEMORY_LIMIT_BYTES,
        cpu_seconds: int = CPU_SECONDS,
        deadline_seconds: float = DEADLINE_SECONDS,
        max_jobs_per_worker: int = MAX_JOBS_PER_WORKER,
        queue_depth: int = QUEUE_DEPTH,
        upload_wait_seconds: float | None = None,
        reparse_wait_seconds: float = REPARSE_WAIT_SECONDS,
        retry_after_seconds: int = RETRY_AFTER_SECONDS,
        start_timeout_seconds: float = START_TIMEOUT_SECONDS,
        admission: DocumentAdmission | None = None,
        spare_worker: bool = True,
    ) -> None:
        count = workers if workers is not None else workers_from_env()
        #: Per-owner admission: upload leases, the worker-time budget, the cooldown.
        self.admission = admission if admission is not None else DocumentAdmission()
        self._memory_limit_bytes = memory_limit_bytes
        self._cpu_seconds = cpu_seconds
        self._deadline_seconds = deadline_seconds
        self._max_jobs_per_worker = max_jobs_per_worker
        self._queue_depth = queue_depth
        self._upload_wait_seconds = (
            upload_wait_seconds if upload_wait_seconds is not None else upload_wait_from_env()
        )
        self._reparse_wait_seconds = reparse_wait_seconds
        self._retry_after_seconds = retry_after_seconds
        self._start_timeout_seconds = start_timeout_seconds
        self._slots: tuple[_Slot, ...] = tuple(_Slot() for _ in range(count))
        self._idle: list[_Slot] = list(self._slots)
        # The spare: a started worker with no owner yet. Guarded by _spare_lock, filled
        # on a thread of its own so no job waits for it.
        self._spare_enabled = spare_worker
        self._spare = _Slot()
        self._spare_lock = threading.Lock()
        self._spare_starting = False
        self._spare_thread: ThreadPoolExecutor | None = None
        # Waiting parses, queued per owner. Owners are served in turn (round robin), so
        # one owner's queued parses cannot keep another owner waiting behind them all.
        self._waiters: dict[str, collections.deque[asyncio.Future[_Slot]]] = {}
        self._waiter_threads: ThreadPoolExecutor | None = None
        self._closed = False
        self._limits_reported = False
        # A pool dropped without aclose() (a test app whose lifespan never ran) still
        # kills its workers; they also exit on their own when their stdin closes.
        self._finalizer = weakref.finalize(self, _kill_all, (*self._slots, self._spare))

    @property
    def worker_pids(self) -> tuple[int, ...]:
        """The live workers' process ids (for tests and diagnostics)."""
        return tuple(
            slot.worker.process.pid
            for slot in self._slots
            if slot.worker is not None and slot.worker.process.poll() is None
        )

    @property
    def spare_pid(self) -> int | None:
        """The spare worker's process id, if one is started (for tests and diagnostics)."""
        with self._spare_lock:
            spare = self._spare.worker
        return spare.process.pid if spare is not None else None

    async def parse(
        self,
        path: Path,
        *,
        render_dir: Path | None,
        owner_id: str,
        purpose: ParsePurpose,
        lease: UploadLease | None = None,
    ) -> ParsedDocument:
        """Parse ``path`` in a worker process.

        Args:
            path: The file.
            render_dir: Where page images go, or ``None``.
            owner_id: Whose parse this is: the budget, the cooldown, the upload slot and
                the queue are all per owner.
            purpose: ``upload`` or ``reparse``.
            lease: An upload's lease, taken by the route with
                :meth:`DocumentAdmission.begin_upload` so it covers the store too. An
                upload without one takes its own, held until the worker is done.

        Raises:
            UnsupportedFormatError, CorruptDocumentError, MissingDependencyError: As
                the parsers raise them. A file that exhausts a worker limit, or crashes
                the worker, is a :class:`CorruptDocumentError` with reason
                ``parse_limit``.
            DocumentUploadInProgressError: An upload without a lease, and this owner
                has an upload in flight.
            DocumentParseBudgetError, DocumentParseCooldownError: The owner is over
                their worker-time budget, or cooling down after a limit. Re-parses too.
            DocumentParserBusyError: No worker became free in time.
            DocumentParserUnavailableError: A worker could not start, or the pool is
                closed.
        """
        if self._closed:
            msg = "the document parser is shut down"
            raise DocumentParserUnavailableError(msg, context={"reason": "closed"})
        loop = asyncio.get_running_loop()
        own_lease: UploadLease | None = None
        if purpose == "upload" and lease is None:
            lease = own_lease = self.admission.begin_upload(owner_id)
        else:
            self.admission.check(owner_id)
        try:
            wait = self._upload_wait_seconds if purpose == "upload" else self._reparse_wait_seconds
            slot = await self._acquire(loop, wait, owner_id)
            job = {
                "job_id": uuid.uuid4().hex,
                "path": str(path),
                "render_dir": str(render_dir) if render_dir is not None else None,
                "dpi": resolve_raster_dpi(),
            }
            if lease is not None:
                lease.hold()  # released when the worker is done with this job
            try:
                done = self._threads().submit(self._run_job, slot, job, owner_id)
            except BaseException:
                if lease is not None:
                    lease.release()
                self._release(slot)
                raise
            # The slot, the job's hold on the lease and the owner's charge all settle
            # only when the worker is really done with the job, even if this request is
            # cancelled while it waits.
            done.add_done_callback(functools.partial(self._job_done, loop, slot, owner_id, lease))
            outcome = await asyncio.wrap_future(done)
        finally:
            if own_lease is not None:
                own_lease.release()
        return self._parsed(outcome, path=path)

    async def aclose(self) -> None:
        """Kill every worker and stop the waiter threads."""
        self._closed = True
        for queue in self._waiters.values():
            for waiter in queue:
                waiter.cancel()
        self._waiters.clear()
        await asyncio.to_thread(_kill_all, self._slots)
        with self._spare_lock:
            spare, self._spare.worker = self._spare.worker, None
        await asyncio.to_thread(_kill, spare)
        if self._waiter_threads is not None:
            self._waiter_threads.shutdown(wait=False, cancel_futures=True)
        if self._spare_thread is not None:
            self._spare_thread.shutdown(wait=False, cancel_futures=True)
        self._finalizer.detach()

    # ----- waiting for a worker (event loop) ---------------------------------------

    def _busy(self, reason: str) -> DocumentParserBusyError:
        return DocumentParserBusyError(
            "the document parser is busy; try again shortly",
            context={"reason": reason, "retry_after_seconds": str(self._retry_after_seconds)},
        )

    def _waiting(self) -> int:
        return sum(len(queue) for queue in self._waiters.values())

    async def _acquire(
        self, loop: asyncio.AbstractEventLoop, wait_seconds: float, owner_id: str
    ) -> _Slot:
        if self._idle and not self._waiters:
            return self._idle.pop()
        if self._waiting() >= self._queue_depth:
            raise self._busy("queue_full")
        waiter: asyncio.Future[_Slot] = loop.create_future()
        self._waiters.setdefault(owner_id, collections.deque()).append(waiter)
        try:
            await asyncio.wait_for(asyncio.shield(waiter), wait_seconds)
        except BaseException as exc:
            self._forget_waiter(owner_id, waiter)
            if waiter.done() and not waiter.cancelled():
                # A worker was handed over just as the wait ended: pass it on.
                self._release(waiter.result())
            else:
                waiter.cancel()
            if isinstance(exc, TimeoutError):
                raise self._busy("wait_expired") from None
            raise
        return waiter.result()

    def _forget_waiter(self, owner_id: str, waiter: asyncio.Future[_Slot]) -> None:
        queue = self._waiters.get(owner_id)
        if queue is None:
            return
        with contextlib.suppress(ValueError):
            queue.remove(waiter)
        if not queue:
            del self._waiters[owner_id]

    def _release(self, slot: _Slot) -> None:
        """Hand a free worker to the next owner in turn, else mark it idle.

        The owner at the front of the rotation gets it, then moves to the back if they
        have more waiting: owners take turns, whatever order they queued in. A suspect
        owner (a limit hit within ten minutes) is passed over while any other owner
        waits, so one owner's hostile files cannot keep the others waiting.
        """
        while self._waiters:
            owner_id = self._next_owner()
            queue = self._waiters.pop(owner_id)
            waiter = queue.popleft()
            if queue:
                self._waiters[owner_id] = queue
            if not waiter.done():
                waiter.set_result(slot)
                return
        self._idle.append(slot)

    def _next_owner(self) -> str:
        """The first waiting owner in turn who is not suspect, else the first in turn."""
        for owner_id in self._waiters:
            if not self.admission.is_suspect(owner_id):
                return owner_id
        return next(iter(self._waiters))

    def _job_done(
        self,
        loop: asyncio.AbstractEventLoop,
        slot: _Slot,
        owner_id: str,
        lease: UploadLease | None,
        done: Future[Any],
    ) -> None:
        """The worker is finished with a job: charge its owner, free its hold and slot."""
        if not done.cancelled() and done.exception() is None:
            outcome = done.result()
            self.admission.charge(
                owner_id,
                float(outcome.get("seconds", 0.0)),
                hit_limit=_hit_limit(outcome),
            )
        if lease is not None:
            lease.release()
        try:
            loop.call_soon_threadsafe(self._release, slot)
        except RuntimeError:
            # The loop that started the job is closed, so nothing on it is waiting:
            # put the worker straight back.
            self._idle.append(slot)

    def _threads(self) -> ThreadPoolExecutor:
        if self._waiter_threads is None:
            self._waiter_threads = ThreadPoolExecutor(
                max_workers=len(self._slots), thread_name_prefix="document-parse-waiter"
            )
        return self._waiter_threads

    # ----- running a job (a waiter thread) -------------------------------------------

    def _run_job(self, slot: _Slot, job: dict[str, Any], owner_id: str) -> dict[str, Any]:
        """Run one job on the slot's worker; the outcome carries the seconds to charge.

        The CPU seconds the worker used for the job where they can be read, else the
        wall seconds (see the module docstring).
        """
        started = time.monotonic()
        try:
            outcome = self._run_job_timed(slot, job, owner_id)
        except DocumentParserUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001 (F8: never leave a worker in an unknown state)
            worker, slot.worker = slot.worker, None
            _kill(worker)
            _clear_render_dir(job.get("render_dir"))
            _log.warning("document parse job failed in the api: {}", type(exc).__name__)
            outcome = {"kind": "failed", "failure": "api_error", "pid": None}
        wall = time.monotonic() - started
        cpu = outcome.pop("cpu_seconds", None)
        outcome["seconds"] = wall if cpu is None or outcome.get("failure") == "deadline" else cpu
        return outcome

    def _run_job_timed(
        self,
        slot: _Slot,
        job: dict[str, Any],
        owner_id: str,
    ) -> dict[str, Any]:
        worker = slot.worker
        if worker is not None and worker.owner_id not in (None, owner_id):
            # F5: a worker never serves two owners. A compromised worker could otherwise
            # read the next owner's file or answer for it; a fresh one costs 0.2 s.
            slot.worker = None
            self._retire(worker)
            worker = None
        if worker is None or worker.process.poll() is not None:
            _kill(worker)
            slot.worker = None
            worker = self._take_spare() or self._start_worker()
            slot.worker = worker
        # Bound for life: a spare, or a worker started for this job, had no owner yet.
        worker.owner_id = owner_id
        self._fill_spare()
        cpu_before = _process_cpu_seconds(worker.process.pid)
        reply, failure = self._exchange(worker, job)
        cpu_after = (
            worker.cpu_at_kill if failure is not None else _process_cpu_seconds(worker.process.pid)
        )
        cpu = None if cpu_before is None or cpu_after is None else max(0.0, cpu_after - cpu_before)
        if failure is not None:
            slot.worker = None
            _clear_render_dir(job.get("render_dir"))
            return {
                "kind": "failed",
                "failure": failure,
                "pid": worker.process.pid,
                "cpu_seconds": cpu,
            }
        if reply is not None and reply.get("kind") != "ok":
            _clear_render_dir(job.get("render_dir"))
        assert reply is not None  # noqa: S101 (_exchange returns one or the other)
        if reply.get("kind") == "ok":
            # Built here, in the waiter thread, so a reply that is not a valid result kills
            # the worker that sent it (F8) instead of failing later with the worker kept.
            render_dir = job.get("render_dir")
            try:
                reply["parsed"] = ParsedDocument(
                    parse_result=self._checked_result(reply["parse_result"]),
                    page_images=self._page_images(
                        reply.get("page_images") or [],
                        Path(render_dir) if render_dir else None,
                    ),
                )
            except Exception:  # noqa: BLE001 (any malformed reply)
                slot.worker = None
                _kill(worker)
                _clear_render_dir(render_dir)
                return {
                    "kind": "failed",
                    "failure": "reply",
                    "pid": worker.process.pid,
                    "cpu_seconds": cpu,
                }
        reply["cpu_seconds"] = cpu
        worker.jobs_done += 1
        if reply.get("leaving") or worker.jobs_done >= self._max_jobs_per_worker:
            slot.worker = None
            self._retire(worker)
        return reply

    @staticmethod
    def _checked_result(raw: object) -> ParseResult:
        """A reply's parse result, within the document bounds whatever the worker sent.

        A worker caps its own result, but a compromised one need not (third security
        review finding 11: 48 million characters in one frame). More sections than a
        genuine worker ever sends (:data:`MAX_DOCUMENT_SECTIONS`, or sheet names) are
        refused before validation builds an object for each; the text budget and the
        section cap are applied again to what validates.
        """
        if not isinstance(raw, dict):
            msg = "the parse result is not an object"
            raise TypeError(msg)
        for field in ("sections", "sheet_names"):
            items = raw.get(field)
            if isinstance(items, list) and len(items) > MAX_DOCUMENT_SECTIONS:
                msg = f"the parse result has too many {field}"
                raise ValueError(msg)
        return cap_document_text(ParseResult.model_validate(raw))

    # ----- the spare worker (its own thread) -------------------------------------------

    def _take_spare(self) -> _Worker | None:
        """The spare worker, if a live one is ready; it has never served an owner."""
        with self._spare_lock:
            spare, self._spare.worker = self._spare.worker, None
        if spare is None:
            return None
        if spare.process.poll() is not None:
            _kill(spare)
            return None
        return spare

    def _fill_spare(self) -> None:
        """Start a spare in the background unless one is ready or starting."""
        if not self._spare_enabled:
            return
        with self._spare_lock:
            if self._closed or self._spare.worker is not None or self._spare_starting:
                return
            self._spare_starting = True
            if self._spare_thread is None:
                self._spare_thread = ThreadPoolExecutor(
                    max_workers=1, thread_name_prefix="document-parse-spare"
                )
            spare_thread = self._spare_thread
        try:
            spare_thread.submit(self._start_spare)
        except RuntimeError:  # shut down by aclose meanwhile
            with self._spare_lock:
                self._spare_starting = False

    def _start_spare(self) -> None:
        try:
            spare: _Worker | None = self._start_worker()
        except DocumentParserUnavailableError:
            spare = None
        with self._spare_lock:
            self._spare_starting = False
            if spare is not None and not self._closed and self._spare.worker is None:
                self._spare.worker, spare = spare, None
        _kill(spare)  # not kept: the pool closed, or a spare was already there

    # ----- starting a worker ---------------------------------------------------------

    def _start_worker(self) -> _Worker:
        command = [
            sys.executable,
            "-P",
            "-m",
            WORKER_MODULE,
            "--memory-bytes",
            str(self._memory_limit_bytes),
            "--cpu-seconds",
            str(self._cpu_seconds),
        ]
        # F5: the worker runs in an empty directory of its own, never the api's working
        # directory (where a .env may sit).
        workdir = tempfile.mkdtemp(prefix="persona-docworker-")
        try:
            process = subprocess.Popen(  # noqa: S603 (fixed argv, no shell)
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                env=worker_environment(os.environ),
                cwd=workdir,
                close_fds=True,
            )
        except OSError as exc:
            shutil.rmtree(workdir, ignore_errors=True)
            msg = "the document parser could not start"
            raise DocumentParserUnavailableError(msg, context={"reason": "start_failed"}) from exc
        worker = _Worker(process=process, workdir=workdir)
        ready, failure = self._read_with_deadline(worker, self._start_timeout_seconds)
        if ready is None or ready.get("kind") != "ready":
            _kill(worker)
            _log.warning("document parse worker did not start: {}", failure or "no ready message")
            msg = "the document parser could not start"
            raise DocumentParserUnavailableError(msg, context={"reason": "start_failed"})
        self._report_limits(ready.get("limits"))
        return worker

    def _report_limits(self, limits: object) -> None:
        if self._limits_reported or not isinstance(limits, dict):
            return
        self._limits_reported = True
        if limits.get("memory") and limits.get("cpu"):
            _log.info("document parse worker limits: memory, cpu and wall clock")
            return
        _log.warning(
            "document parse worker limits on this platform: memory={} cpu={}; the "
            "wall-clock kill and crash isolation still apply",
            bool(limits.get("memory")),
            bool(limits.get("cpu")),
        )

    def _exchange(
        self, worker: _Worker, job: dict[str, Any]
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Send one job and read its reply; ``(None, why)`` if the worker failed."""
        stdin = worker.process.stdin
        if stdin is None:  # pragma: no cover (always piped)
            _kill(worker)
            return None, "crashed"
        try:
            write_frame(stdin, job)
        except Exception:  # noqa: BLE001 (F8: any failure to write means a dead worker)
            return None, self._failure(worker, deadline_hit=False)
        reply, failure = self._read_with_deadline(worker, self._deadline_seconds)
        if reply is not None and reply.get("job_id") != job["job_id"]:
            # F5: a reply for another job (a stale second frame, or a forged one) is
            # never taken as this job's. The worker is killed and the job refused.
            _kill(worker)
            return None, "protocol"
        return reply, failure

    def _read_with_deadline(
        self, worker: _Worker, seconds: float
    ) -> tuple[dict[str, Any] | None, str | None]:
        """Read one frame, killing the worker if none arrives within ``seconds``.

        Killing the process closes its end of the pipe, so the blocked read returns:
        this works the same on every platform, with no select on a pipe.
        """
        fired = threading.Event()

        def deadline() -> None:
            fired.set()
            with contextlib.suppress(OSError):
                worker.process.kill()

        timer = threading.Timer(seconds, deadline)
        timer.daemon = True
        timer.start()
        broken = False
        try:
            stdout = worker.process.stdout
            reply = read_frame(stdout) if stdout is not None else None
        except Exception:  # noqa: BLE001 (F8: any failure to read means a dead worker)
            reply = None
            broken = True
        finally:
            timer.cancel()
        if reply is None:
            failure = self._failure(worker, deadline_hit=fired.is_set())
            if broken and not fired.is_set():
                failure = "reply"  # it answered, but not with a frame the api can read
            return None, failure
        return reply, None

    @staticmethod
    def _failure(worker: _Worker, *, deadline_hit: bool) -> str:
        code = _kill(worker)
        if deadline_hit:
            return "deadline"
        if _SIGXCPU is not None and code == -_SIGXCPU:
            return "cpu_limit"
        if code is not None and code < 0:
            return f"signal_{-code}"
        return "crashed"

    @staticmethod
    def _retire(worker: _Worker) -> None:
        """End a healthy worker: close its stdin so it exits, then make sure."""
        stdin = worker.process.stdin
        if stdin is not None:
            with contextlib.suppress(OSError, ValueError):
                stdin.close()
        try:
            worker.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _kill(worker)
        else:
            if worker.process.stdout is not None:
                with contextlib.suppress(OSError, ValueError):
                    worker.process.stdout.close()
            _remove_workdir(worker)

    # ----- turning a reply into a result (event loop) ----------------------------------

    def _parsed(self, outcome: dict[str, Any], *, path: Path) -> ParsedDocument:
        kind = outcome.get("kind")
        if kind == "ok":
            parsed: ParsedDocument = outcome["parsed"]
            return parsed
        document_format = path.suffix.lower().lstrip(".") or "unknown"
        if kind == "error":
            error = outcome.get("error")
            message = str(outcome.get("message") or "")
            context = {str(k): str(v) for k, v in dict(outcome.get("context") or {}).items()}
            if error == "unsupported":
                raise UnsupportedFormatError(message, context=context)
            if error == "corrupt":
                raise CorruptDocumentError(message, context=context)
            if error == "missing_dependency":
                raise MissingDependencyError(message, context=context)
            failure = error if error in ("memory_limit", "reply_too_large") else "worker_error"
            _log.warning(
                "document parse failed in the worker: {} ({})", failure, outcome.get("exception")
            )
            raise self._limit_refusal(failure, document_format)
        failure = str(outcome.get("failure") or "crashed")
        _log.warning("document parse worker ended: {} pid={}", failure, outcome.get("pid"))
        raise self._limit_refusal(failure, document_format)

    @staticmethod
    def _limit_refusal(failure: str, document_format: str) -> CorruptDocumentError:
        return CorruptDocumentError(
            "this document could not be read within the limits for documents",
            context={"format": document_format, "reason": "parse_limit", "limit": failure},
        )

    @staticmethod
    def _page_images(paths: list[Any], render_dir: Path | None) -> tuple[Path, ...]:
        """The worker's page images, each checked to be ``page-NNNN.png`` in ``render_dir``."""
        if not paths:
            return ()
        if render_dir is None:
            msg = "the parse returned page images it was not asked for"
            raise CorruptDocumentError(msg, context={"reason": "parse_limit", "limit": "reply"})
        checked: list[Path] = []
        for raw in paths:
            image = Path(str(raw))
            if image.parent != render_dir or not _PAGE_IMAGE_NAME.match(image.name):
                msg = "the parse returned a page image outside its render directory"
                raise CorruptDocumentError(msg, context={"reason": "parse_limit", "limit": "reply"})
            checked.append(image)
        return tuple(checked)
