"""Who may use the document parse worker, and for how long (second security review F2).

One owner could hold the parse worker as long as they liked: each upload was bounded, but
nothing bounded the uploads. This service bounds the owner:

- **At most two upload bodies arriving per owner.** A :class:`BodySlot` is held while
  an upload's body arrives (third security review finding 5); a third answers 429
  ``uploads_arriving`` before a byte of it is read.
- **One upload in flight per owner.** An :class:`UploadLease` is held from the moment
  an upload is admitted until its parse has finished in the worker AND its store has
  finished in the api, cancellation included. A second upload meanwhile answers 429
  ``upload_in_progress``.
- **A worker-time budget.** Every job charges its owner the CPU seconds it used in the
  worker (wall seconds where the platform cannot say; see the pool).
  Past :data:`WORKER_SECONDS_BUDGET` within the last :data:`BUDGET_WINDOW_SECONDS`, the
  owner's parses (uploads and re-parses alike) answer 429 ``parse_budget_exhausted``
  until enough of that time has left the window.
- **A cooldown after a limit.** A job that ended at a worker limit (killed by the
  deadline, the CPU or memory limit, a crash, or a reply the api cannot read) bars its
  owner for :data:`COOLDOWN_SECONDS`: 429 ``parse_cooldown``.
- **Suspect owners go last.** For :data:`SUSPECT_SECONDS` after a limit, the owner is
  suspect (:meth:`DocumentAdmission.is_suspect`): the pool hands them a worker only when
  no other owner is waiting for one (third security review finding 2).

The state lives on an instance the app builds and injects (``app.state``), never in
module globals, and every method is safe to call from any thread: leases are released
from worker-waiter threads and store threads.
"""

from __future__ import annotations

import collections
import math
import threading
import time
from typing import TYPE_CHECKING

from persona.errors import PersonaError

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "BUDGET_WINDOW_SECONDS",
    "COOLDOWN_SECONDS",
    "MAX_BODIES_PER_OWNER",
    "SUSPECT_SECONDS",
    "WORKER_SECONDS_BUDGET",
    "BodySlot",
    "DocumentAdmission",
    "DocumentParseBudgetError",
    "DocumentParseCooldownError",
    "DocumentUploadInProgressError",
    "DocumentUploadsArrivingError",
    "UploadLease",
]

#: Worker-seconds one owner may use within :data:`BUDGET_WINDOW_SECONDS`. Genuine large
#: files take up to about 15 s (D-FV0-31 measurements), so this is a dozen of the
#: largest documents in ten minutes, and at most 30 % of one worker's time for one owner.
WORKER_SECONDS_BUDGET: float = 180.0

#: The sliding window the budget is counted over.
BUDGET_WINDOW_SECONDS: float = 600.0

#: How long an owner waits after one of their files ended at a worker limit. A hostile
#: file costs its owner the job's seconds (up to the 60 s deadline) plus this.
COOLDOWN_SECONDS: float = 60.0

#: How long after a limit an owner's parses wait behind every other owner's.
SUSPECT_SECONDS: float = 600.0

#: Upload bodies one owner may have arriving at once.
MAX_BODIES_PER_OWNER: int = 2


class DocumentUploadInProgressError(PersonaError):
    """This owner already has an upload being parsed or stored."""


class DocumentParseBudgetError(PersonaError):
    """This owner has used their worker-time budget; ``retry_after_seconds`` in context."""


class DocumentParseCooldownError(PersonaError):
    """This owner's last file ended at a worker limit; ``retry_after_seconds`` in context."""


class DocumentUploadsArrivingError(PersonaError):
    """This owner already has :data:`MAX_BODIES_PER_OWNER` upload bodies arriving."""


class BodySlot:
    """One upload body arriving; :meth:`release` is safe to call more than once."""

    def __init__(self, admission: DocumentAdmission, owner_id: str) -> None:
        self._admission = admission
        self.owner_id = owner_id
        self._released = False

    def release(self) -> None:
        """The body has arrived, or the request ended: free the owner's place."""
        with self._admission.lock:
            if self._released:
                return
            self._released = True
            self._admission.forget_body(self.owner_id)


class UploadLease:
    """One owner's upload, held until every part of it has finished.

    Reference counted: the route holds one reference from admission until its store and
    ingest finish, and each worker job holds one until the worker is done with it. The
    owner's upload slot is free again when the count reaches zero, and then the
    callbacks given to :meth:`on_released` run (the route's scratch directory is removed
    that way, only once neither the worker nor the store can still use it).
    """

    def __init__(self, admission: DocumentAdmission, owner_id: str) -> None:
        self._admission = admission
        self.owner_id = owner_id
        self._holds = 1
        self._store_started = False
        self._abandoned = False
        self._on_released: list[Callable[[], None]] = []

    def on_released(self, callback: Callable[[], None]) -> None:
        """Run ``callback`` once the last reference is released (now, if it already is)."""
        with self._admission.lock:
            if self._holds > 0:
                self._on_released.append(callback)
                return
        callback()

    def hold(self) -> None:
        """Take one more reference (a worker job starting on this upload's behalf)."""
        with self._admission.lock:
            if self._holds <= 0:
                msg = "the upload lease was already released"
                raise RuntimeError(msg)
            self._holds += 1

    def release(self) -> None:
        """Drop one reference; the last one frees the owner's upload slot."""
        with self._admission.lock:
            if self._holds <= 0:
                return
            self._holds -= 1
            if self._holds > 0:
                return
            self._admission.forget_upload(self)
            callbacks, self._on_released = self._on_released, []
        for callback in callbacks:
            callback()

    @property
    def released(self) -> bool:
        """Whether every reference is gone."""
        with self._admission.lock:
            return self._holds <= 0

    def start_store(self) -> bool:
        """Called first thing in the store thread: False if the request gave up before it ran.

        The route abandons the lease when it is cancelled while the store is still
        queued for a thread; a store that then starts does nothing, so nothing is stored
        without its audit, and the lease is not released twice.
        """
        with self._admission.lock:
            if self._abandoned:
                return False
            self._store_started = True
            return True

    def abandon_store(self) -> None:
        """The request is gone: release the route's reference unless the store has started.

        A store that has started releases the reference itself when it finishes.
        """
        with self._admission.lock:
            if self._store_started or self._abandoned:
                return
            self._abandoned = True
        self.release()


class DocumentAdmission:
    """Per-owner admission for the document parse worker; see the module docstring."""

    def __init__(
        self,
        *,
        worker_seconds_budget: float = WORKER_SECONDS_BUDGET,
        window_seconds: float = BUDGET_WINDOW_SECONDS,
        cooldown_seconds: float = COOLDOWN_SECONDS,
        suspect_seconds: float = SUSPECT_SECONDS,
        max_bodies_per_owner: int = MAX_BODIES_PER_OWNER,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._budget = worker_seconds_budget
        self._window = window_seconds
        self._cooldown = cooldown_seconds
        self._clock = clock
        self.lock = threading.Lock()
        self._uploads: dict[str, UploadLease] = {}
        self._spent: dict[str, collections.deque[tuple[float, float]]] = {}
        self._cooldown_until: dict[str, float] = {}
        self._suspect_until: dict[str, float] = {}
        self._bodies: dict[str, int] = {}
        self._max_bodies = max_bodies_per_owner
        self._suspect_seconds = suspect_seconds

    # ----- checks ------------------------------------------------------------------

    def check(self, owner_id: str) -> None:
        """Refuse an owner who is cooling down or over their worker-time budget.

        Raises:
            DocumentParseCooldownError, DocumentParseBudgetError: With
                ``retry_after_seconds`` in ``context``.
        """
        with self.lock:
            now = self._clock()
            until = self._cooldown_until.get(owner_id)
            if until is not None and now < until:
                msg = "a file of yours could not be read within the limits; try again shortly"
                raise DocumentParseCooldownError(
                    msg,
                    context={
                        "reason": "parse_cooldown",
                        "retry_after_seconds": str(max(1, math.ceil(until - now))),
                    },
                )
            if until is not None:
                del self._cooldown_until[owner_id]
            spent = self._spent_locked(owner_id, now, create=False)
            if spent and sum(seconds for _, seconds in spent) >= self._budget:
                oldest_end = spent[0][0]
                retry = max(1, math.ceil(oldest_end + self._window - now))
                msg = "you have used your document reading time for now; try again later"
                raise DocumentParseBudgetError(
                    msg,
                    context={
                        "reason": "parse_budget_exhausted",
                        "retry_after_seconds": str(retry),
                    },
                )

    def begin_body(self, owner_id: str) -> BodySlot:
        """Admit one more upload body from this owner, before any of it is read.

        Raises:
            DocumentUploadsArrivingError: :data:`MAX_BODIES_PER_OWNER` are arriving.
        """
        with self.lock:
            arriving = self._bodies.get(owner_id, 0)
            if arriving >= self._max_bodies:
                msg = "other uploads of yours are still arriving; try again when they finish"
                raise DocumentUploadsArrivingError(
                    msg,
                    context={"reason": "uploads_arriving", "retry_after_seconds": "5"},
                )
            self._bodies[owner_id] = arriving + 1
            return BodySlot(self, owner_id)

    def forget_body(self, owner_id: str) -> None:
        """One of the owner's bodies is done. Called with :attr:`lock` held, by the slot."""
        arriving = self._bodies.get(owner_id, 0) - 1
        if arriving > 0:
            self._bodies[owner_id] = arriving
        else:
            self._bodies.pop(owner_id, None)

    def bodies_arriving(self, owner_id: str) -> int:
        """How many of this owner's upload bodies are arriving."""
        with self.lock:
            return self._bodies.get(owner_id, 0)

    def begin_upload(self, owner_id: str) -> UploadLease:
        """Admit an upload: checks the owner, then takes their one upload slot.

        Raises:
            DocumentUploadInProgressError: The owner has an upload in flight.
            DocumentParseCooldownError, DocumentParseBudgetError: As :meth:`check`.
        """
        self.check(owner_id)
        with self.lock:
            if owner_id in self._uploads:
                msg = "an upload of yours is still being read; try again when it finishes"
                raise DocumentUploadInProgressError(msg, context={"reason": "upload_in_progress"})
            lease = UploadLease(self, owner_id)
            self._uploads[owner_id] = lease
            return lease

    # ----- accounting --------------------------------------------------------------

    def charge(self, owner_id: str, seconds: float, *, hit_limit: bool) -> None:
        """Record a finished job: the worker seconds it cost, and whether it hit a limit.

        A limit starts the owner's cooldown and makes them suspect.
        """
        with self.lock:
            now = self._clock()
            self._spent_locked(owner_id, now).append((now, max(0.0, seconds)))
            if hit_limit:
                self._cooldown_until[owner_id] = now + self._cooldown
                self._suspect_until[owner_id] = now + self._suspect_seconds

    def is_suspect(self, owner_id: str) -> bool:
        """Whether one of this owner's files hit a worker limit within :data:`SUSPECT_SECONDS`."""
        with self.lock:
            until = self._suspect_until.get(owner_id)
            if until is None:
                return False
            if self._clock() < until:
                return True
            del self._suspect_until[owner_id]
            return False

    def worker_seconds_used(self, owner_id: str) -> float:
        """The worker-seconds this owner has used within the window."""
        with self.lock:
            spent = self._spent_locked(owner_id, self._clock(), create=False)
            return sum(seconds for _, seconds in spent)

    def upload_in_flight(self, owner_id: str) -> bool:
        """Whether this owner holds an upload lease."""
        with self.lock:
            return owner_id in self._uploads

    def forget_upload(self, lease: UploadLease) -> None:
        """Free the owner's upload slot. Called with :attr:`lock` held, by the lease."""
        if self._uploads.get(lease.owner_id) is lease:
            del self._uploads[lease.owner_id]

    def _spent_locked(
        self, owner_id: str, now: float, *, create: bool = True
    ) -> collections.deque[tuple[float, float]]:
        """The owner's charges still inside the window (older ones are dropped here)."""
        spent = self._spent.get(owner_id)
        if spent is None:
            spent = collections.deque()
            if create:
                self._spent[owner_id] = spent
            return spent
        while spent and spent[0][0] <= now - self._window:
            spent.popleft()
        if not spent and not create:
            del self._spent[owner_id]
        return spent
