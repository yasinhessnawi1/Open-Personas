"""The Postgres-backed durable job queue — claim / lease / heartbeat (Spec A0, T3).

``SELECT … FOR UPDATE SKIP LOCKED`` claim, a lease + heartbeat crash-detection
model, and the lifecycle UPDATEs the worker drives. The one correctness
invariant, stated loudly (D-A0-X-claim-then-commit):

    **The claim transaction is SHORT — claim-then-commit, never claim-and-hold.**

``claim`` opens its own transaction, marks the rows ``claimed`` with a lease, and
**commits before returning**. The lease (``lease_expires_at`` + ``locked_by``)
is what *owns* the job while the handler runs — we never hold a ``FOR UPDATE``
row lock for the job's duration. Holding the claim transaction open while the
handler ran would pin Postgres's xmin horizon and stall autovacuum cluster-wide
(the canonical Postgres-as-queue death). A dead/draining worker is detected
purely by lease expiry; ``reclaim_expired`` returns its jobs to ``queued`` (the
crash-resume edge — one mechanism for both crash and graceful drain).

**Engine / RLS contract.** ``enqueue`` is a tenant-attributed write (the owner is
known) and runs inside the owner's RLS scope, so the jobs-table ``WITH CHECK``
holds. ``claim``/``heartbeat``/``mark_running``/``complete``/``reclaim_expired``
are platform-DISPATCH operations that span tenants (one worker serves everyone),
so they require an engine with cross-tenant visibility on the jobs table — the
worker injects the admin/system engine for these. Tenant data access *inside* a
handler is a separate concern: the worker re-scopes to ``persona_app`` + the
job's owner GUC at the claim→execute choke point (D-A0-X-rls-chokepoint, T4).
The jobs-table RLS still governs tenant-facing reads (A6) and the enqueue check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from persona.jobs import JobState
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from persona_api.db.engine import rls_connection
from persona_api.db.models import jobs, jobs_archive

if TYPE_CHECKING:
    from sqlalchemy import Engine, RowMapping

__all__ = ["JobQueue", "JobRecord"]


class JobRecord(BaseModel):
    """The raw persistence shape of one ``jobs`` row (payload as JSONB dict).

    Registry-free: the queue reads/writes rows with the payload as a plain dict;
    the worker (T4) reconstructs the concrete typed payload via the
    :class:`~persona.jobs.JobRegistry` before dispatching to a handler. The typed
    domain object (:class:`persona.jobs.Job`) is the enqueue-construction / test
    shape; this is the over-the-wire-from-Postgres shape.
    """

    model_config = ConfigDict(frozen=True)

    id: str
    type: str
    owner_id: str
    payload: dict[str, Any]
    idempotency_key: str
    state: JobState
    priority: int
    attempt: int
    max_attempts: int
    scheduled_at: datetime
    created_at: datetime
    lease_expires_at: datetime | None
    locked_by: str | None
    last_error: str | None


def _record(row: RowMapping) -> JobRecord:
    """Map a ``jobs`` row (by column name) to a :class:`JobRecord`."""
    return JobRecord(
        id=row["id"],
        type=row["type"],
        owner_id=row["owner_id"],
        payload=row["payload"],
        idempotency_key=row["idempotency_key"],
        state=JobState(row["state"]),
        priority=row["priority"],
        attempt=row["attempt"],
        max_attempts=row["max_attempts"],
        scheduled_at=row["scheduled_at"],
        created_at=row["created_at"],
        lease_expires_at=row["lease_expires_at"],
        locked_by=row["locked_by"],
        last_error=row["last_error"],
    )


# The claim is one atomic statement: a SKIP LOCKED select-and-lock CTE feeding an
# UPDATE, so concurrent workers each grab a DISTINCT row in one round trip and
# never double-claim. ``FOR UPDATE SKIP LOCKED`` skips rows another worker has
# locked rather than blocking on them. All values are bound parameters.
#
# Claim-time fairness (T7, D-A0-6): a candidate is skipped when its owner already
# has ``:max_per_user`` jobs in flight (correlated count), or when total in-flight
# reaches ``:max_global`` — so one user's flood cannot starve others. The counts
# read durable ``jobs.state`` (in-flight state, not an in-process semaphore), so
# the gate is multi-worker-correct. ``idx_jobs_inflight_by_owner`` serves both
# counts. NB: claim-then-commit means an xact advisory lock (the imagegen cap-1
# primitive) cannot span a job's out-of-transaction run — the durable count is the
# correct duration-spanning mechanism here. This is an anti-starvation gate; a
# brief over-cap under concurrent batch claims is acceptable (it never starves).
_CLAIM_SQL = text(
    """
    WITH claimed AS (
        SELECT j.id
        FROM   jobs j
        WHERE  j.state = 'queued' AND j.scheduled_at <= :now
          AND  (SELECT count(*) FROM jobs u
                WHERE u.owner_id = j.owner_id
                  AND u.state IN ('claimed', 'running')) < :max_per_user
          AND  (SELECT count(*) FROM jobs g
                WHERE g.state IN ('claimed', 'running')) < :max_global
        ORDER BY j.priority DESC, j.scheduled_at
        FOR UPDATE SKIP LOCKED
        LIMIT  :limit
    )
    UPDATE jobs jj
    SET    state            = 'claimed',
           locked_by        = :worker_id,
           lease_expires_at = :lease_expires_at,
           attempt          = jj.attempt + 1
    FROM   claimed
    WHERE  jj.id = claimed.id
    RETURNING jj.*
    """
)

# Sentinel for an "unlimited" cap — larger than any realistic in-flight count.
_UNLIMITED = 2_147_483_647

# The shared column list for archival (everything but the archive-only archived_at,
# which server-defaults to now()).
_ARCHIVE_COLS = (
    "id, type, owner_id, payload, idempotency_key, state, priority, attempt, "
    "max_attempts, scheduled_at, created_at, lease_expires_at, locked_by, last_error"
)

# Age terminal jobs out of the HOT table into the COLD archive (D-A0-4 hygiene
# against bloat). One atomic statement: a SKIP LOCKED candidate set → DELETE …
# RETURNING (data-modifying CTE) → INSERT, so concurrent maintenance sweeps move
# DISJOINT rows (a row deleted by one worker is invisible to another) — no
# double-archive, no lost row.
_ARCHIVE_TERMINAL_SQL = text(
    f"""
    WITH candidates AS (
        SELECT id
        FROM   jobs
        WHERE  state IN ('succeeded', 'failed', 'dead') AND created_at < :older_than
        ORDER BY created_at
        LIMIT  :limit
        FOR UPDATE SKIP LOCKED
    ),
    moved AS (
        DELETE FROM jobs WHERE id IN (SELECT id FROM candidates)
        RETURNING {_ARCHIVE_COLS}
    )
    INSERT INTO jobs_archive ({_ARCHIVE_COLS})
    SELECT {_ARCHIVE_COLS} FROM moved
    """
)

_PURGE_ARCHIVE_SQL = text(
    """
    DELETE FROM jobs_archive
    WHERE id IN (SELECT id FROM jobs_archive WHERE archived_at < :older_than LIMIT :limit)
    """
)


def _utcnow(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(UTC)


class JobQueue:
    """Durable queue operations over the ``jobs`` table.

    Construct with an engine appropriate to the operation's RLS scope — see the
    module docstring's engine contract. The queue performs no I/O beyond these
    short transactions and holds no in-process state (multi-worker-correct).
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def enqueue(
        self,
        *,
        type: str,  # noqa: A002 — mirrors the durable ``jobs.type`` column name.
        owner_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
        priority: int = 0,
        scheduled_at: datetime | None = None,
        max_attempts: int = 5,
    ) -> JobRecord | None:
        """Enqueue a job; a duplicate ``idempotency_key`` is a no-op.

        ``INSERT … ON CONFLICT (idempotency_key) DO NOTHING RETURNING`` — returns
        the inserted :class:`JobRecord`, or ``None`` if the key already exists
        (the duplicate enqueue is silently absorbed). Runs in the owner's RLS
        scope so the jobs-table ``WITH CHECK`` holds.
        """
        values: dict[str, Any] = {
            "type": type,
            "owner_id": owner_id,
            "payload": payload,
            "idempotency_key": idempotency_key,
            "priority": priority,
            "max_attempts": max_attempts,
        }
        if scheduled_at is not None:
            values["scheduled_at"] = scheduled_at
        stmt = (
            pg_insert(jobs)
            .values(**values)
            .on_conflict_do_nothing(index_elements=["owner_id", "idempotency_key"])
            .returning(jobs)
        )
        with rls_connection(self._engine, owner_id) as conn:
            row = conn.execute(stmt).mappings().first()
        return _record(row) if row is not None else None

    def claim(
        self,
        *,
        worker_id: str,
        lease_seconds: int,
        limit: int = 1,
        max_per_user: int = 0,
        max_global: int = 0,
        now: datetime | None = None,
    ) -> list[JobRecord]:
        """Claim up to ``limit`` due jobs via SKIP LOCKED; commit; return them.

        Short transaction: the rows are marked ``claimed`` with a lease and the
        transaction COMMITS before this returns. The caller does the work
        afterwards, outside any transaction — the lease, not a row lock, owns the
        job. ``attempt`` is incremented on claim (1-based for backoff).

        Fairness (D-A0-6): ``max_per_user`` skips a candidate whose owner already
        has that many jobs in flight; ``max_global`` caps total in-flight. ``0``
        means unlimited for either.
        """
        now = _utcnow(now)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        with self._engine.begin() as conn:
            rows = (
                conn.execute(
                    _CLAIM_SQL,
                    {
                        "now": now,
                        "limit": limit,
                        "worker_id": worker_id,
                        "lease_expires_at": lease_expires_at,
                        "max_per_user": max_per_user or _UNLIMITED,
                        "max_global": max_global or _UNLIMITED,
                    },
                )
                .mappings()
                .all()
            )
        return [_record(r) for r in rows]

    def heartbeat(
        self,
        *,
        job_id: str,
        worker_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> bool:
        """Extend a job's lease, only if this worker still owns it.

        Returns ``True`` if the lease was renewed, ``False`` if the worker no
        longer holds it (reclaimed / completed / never owned) — the signal to
        stop working on a job another worker has taken over.
        """
        now = _utcnow(now)
        lease_expires_at = now + timedelta(seconds=lease_seconds)
        stmt = (
            update(jobs)
            .where(
                jobs.c.id == job_id,
                jobs.c.locked_by == worker_id,
                jobs.c.state.in_([JobState.CLAIMED.value, JobState.RUNNING.value]),
            )
            .values(lease_expires_at=lease_expires_at)
        )
        with self._engine.begin() as conn:
            return conn.execute(stmt).rowcount == 1

    def mark_running(self, *, job_id: str, worker_id: str) -> bool:
        """Transition a claimed job to ``running`` (handler start). Owner-checked."""
        stmt = (
            update(jobs)
            .where(
                jobs.c.id == job_id,
                jobs.c.locked_by == worker_id,
                jobs.c.state == JobState.CLAIMED.value,
            )
            .values(state=JobState.RUNNING.value)
        )
        with self._engine.begin() as conn:
            return conn.execute(stmt).rowcount == 1

    def complete(self, *, job_id: str, worker_id: str) -> bool:
        """Mark a running job ``succeeded`` and release its lease. Owner-checked."""
        stmt = (
            update(jobs)
            .where(
                jobs.c.id == job_id,
                jobs.c.locked_by == worker_id,
                jobs.c.state == JobState.RUNNING.value,
            )
            .values(state=JobState.SUCCEEDED.value, lease_expires_at=None, locked_by=None)
        )
        with self._engine.begin() as conn:
            return conn.execute(stmt).rowcount == 1

    def reclaim_expired(self, *, now: datetime | None = None) -> int:
        """Return jobs whose lease lapsed to ``queued`` (the crash-resume sweep).

        A ``claimed``/``running`` job whose ``lease_expires_at`` is in the past
        was held by a worker that died or drained without finishing; it becomes
        claimable again. Returns the number reclaimed. This is the SAME mechanism
        for a hard crash and a graceful-drain hand-off (D-A0-X-one-mechanism).
        """
        now = _utcnow(now)
        stmt = (
            update(jobs)
            .where(
                jobs.c.state.in_([JobState.CLAIMED.value, JobState.RUNNING.value]),
                jobs.c.lease_expires_at < now,
            )
            .values(
                state=JobState.QUEUED.value,
                locked_by=None,
                lease_expires_at=None,
            )
        )
        with self._engine.begin() as conn:
            return conn.execute(stmt).rowcount

    def _terminate(self, *, job_id: str, worker_id: str, new_state: JobState, error: str) -> bool:
        """RUNNING → ``new_state`` with ``last_error``, releasing the lease. Owner-checked."""
        stmt = (
            update(jobs)
            .where(
                jobs.c.id == job_id,
                jobs.c.locked_by == worker_id,
                jobs.c.state == JobState.RUNNING.value,
            )
            .values(
                state=new_state.value,
                last_error=error,
                lease_expires_at=None,
                locked_by=None,
            )
        )
        with self._engine.begin() as conn:
            return conn.execute(stmt).rowcount == 1

    def retry(self, *, job_id: str, worker_id: str, error: str, scheduled_at: datetime) -> bool:
        """Return a failed-but-retryable job to ``queued``, due at ``scheduled_at``.

        Records the cause and releases the lease; the job is re-claimable once
        ``scheduled_at`` arrives (the backoff delay). Owner-checked.
        """
        stmt = (
            update(jobs)
            .where(
                jobs.c.id == job_id,
                jobs.c.locked_by == worker_id,
                jobs.c.state == JobState.RUNNING.value,
            )
            .values(
                state=JobState.QUEUED.value,
                last_error=error,
                scheduled_at=scheduled_at,
                lease_expires_at=None,
                locked_by=None,
            )
        )
        with self._engine.begin() as conn:
            return conn.execute(stmt).rowcount == 1

    def mark_dead(self, *, job_id: str, worker_id: str, error: str) -> bool:
        """Dead-letter a job (retries exhausted) — terminal ``dead`` with cause."""
        return self._terminate(
            job_id=job_id, worker_id=worker_id, new_state=JobState.DEAD, error=error
        )

    def mark_failed(self, *, job_id: str, worker_id: str, error: str) -> bool:
        """Terminally fail a job (permanent, non-retryable) — ``failed`` with cause."""
        return self._terminate(
            job_id=job_id, worker_id=worker_id, new_state=JobState.FAILED, error=error
        )

    def count_spent_attempts(self, *, owner_id: str, idempotency_key: str) -> int:
        """How many jobs at ``idempotency_key`` (or its ``:retry:N`` successors) are SPENT.

        Spec W1 (D-W1-20, amended D-W1-29): the suffix a resume appends. A spent row at the
        CURRENT head is one that ended without advancing the head: dead-lettered, permanently
        failed, or **succeeded without running** (the claim-side skip of a paused task and the
        over-budget park both CONSUME the job). Any of them would absorb a later enqueue at
        that head through the duplicate guard, so all of them count. A genuinely completed leg
        advanced the head, so its succeeded row sits at an older key and never matters.

        Counts the hot table AND the archive (the sweep ages terminal rows out after a day; the
        count must not reset when it does). Owner-scoped (RLS + the explicit predicate).
        Read-only (CQS).
        """
        terminal = ("succeeded", "dead", "failed")
        successors = f"{idempotency_key}:retry:%"
        hot = (
            select(func.count())
            .select_from(jobs)
            .where(
                jobs.c.owner_id == owner_id,
                jobs.c.state.in_(terminal),
                (jobs.c.idempotency_key == idempotency_key)
                | jobs.c.idempotency_key.like(successors),
            )
        )
        cold = (
            select(func.count())
            .select_from(jobs_archive)
            .where(
                jobs_archive.c.owner_id == owner_id,
                jobs_archive.c.state.in_(terminal),
                (jobs_archive.c.idempotency_key == idempotency_key)
                | jobs_archive.c.idempotency_key.like(successors),
            )
        )
        with rls_connection(self._engine, owner_id) as conn:
            return int(conn.execute(hot).scalar_one()) + int(conn.execute(cold).scalar_one())

    def dead_letters(self, *, limit: int = 50, offset: int = 0) -> list[JobRecord]:
        """List dead-lettered jobs (newest first) — the A3/A6 observability seam.

        Read-only (CQS). Cross-tenant on the dispatch engine; tenant-facing
        history reads go through an owner-scoped query elsewhere (RLS).
        """
        stmt = (
            jobs.select()
            .where(jobs.c.state == JobState.DEAD.value)
            .order_by(jobs.c.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        with self._engine.begin() as conn:
            return [_record(r) for r in conn.execute(stmt).mappings().all()]

    def archive_terminal(self, *, older_than: datetime, limit: int = 1000) -> int:
        """Move terminal jobs older than ``older_than`` from hot ``jobs`` to cold.

        The cleaner sweep (D-A0-4): keeps the hot table's working set — and so the
        claim path and its indexes — small while A3/A6 still read history from
        ``jobs_archive``. ``older_than`` (caller computes ``now - archive_after``)
        leaves a window so recently-terminal jobs (incl. dead-letters) stay hot and
        queryable for a while. Race-safe under concurrent workers. Returns the count
        archived.
        """
        with self._engine.begin() as conn:
            return conn.execute(
                _ARCHIVE_TERMINAL_SQL, {"older_than": older_than, "limit": limit}
            ).rowcount

    def purge_archive(self, *, older_than: datetime, limit: int = 1000) -> int:
        """Delete archived jobs older than ``older_than`` (retention). Returns count."""
        with self._engine.begin() as conn:
            return conn.execute(
                _PURGE_ARCHIVE_SQL, {"older_than": older_than, "limit": limit}
            ).rowcount
