"""The avatar-generation job handler — A0's first durable tenant (Spec A0, T9).

Replaces the create-path ``BackgroundTasks`` avatar hook with a durable job: an
avatar survives an api restart between create and generation, and the handler
proves the **at-least-once contract** (criterion 2) — a re-delivery (handler
retry OR lease-expiry reclaim) leaves EXACTLY ONE valid avatar, never a duplicate.

**Path selection is automatic (no cutover flag).** The create and regenerate
routes ask :func:`avatar_queue_available`: a job is enqueued iff THIS process's
worker has the ``avatar_generation`` handler registered, which is the strongest
form of "enqueue implies handler" (R9-013) because it reads the registry that
will actually run the job rather than re-deriving its preconditions. Without a
consuming worker (community without one, keyless boots) the routes keep the
in-request path. ``PERSONA_API_AVATAR_INLINE_ONLY`` is the explicit opt-out.

**Idempotency mechanism — SKIP-IF-ALREADY-SET (declared at registration).**
``personas.avatar_url`` is the durable marker for a create-time job: the handler
no-ops if it is already set, so the common re-delivery (the side effect completed
before the crash) re-runs as a true no-op — no wasteful regeneration, no orphan
bytes. The rarer kill-between-generate-and-set case re-generates; the generator
persists at a deterministic per-persona path so the re-gen OVERWRITES rather than
orphaning, and the ``WHERE avatar_url IS NULL`` compare-and-set sets exactly one
url even under concurrent re-delivery. A **regeneration** job carries its own
idempotency key (``avatar:{persona_id}:regen:{request_id}``) and replaces the
current avatar unconditionally: the marker would otherwise dedupe it into a
silent no-op, which is exactly what the queued regenerate did before this.
Avatar generation is non-deterministic, so the gate is "one valid avatar_url, no
corruption," not "byte-identical output."

The concrete provider wiring (an :class:`AvatarGenerator` over the imagegen
service) is composed at the worker root; this module is mechanism-agnostic over
that seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

from persona.jobs import SHORT_LEASE, JobPayload, JobState, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update

from persona_api.db.models import personas
from persona_api.services import persona_service

_log = get_logger("api.jobs.avatar")

if TYPE_CHECKING:
    from persona.jobs import JobContext, JobRegistry

    from persona_api.jobs.queue import JobQueue, JobRecord

__all__ = [
    "AVATAR_JOB_TYPE",
    "AvatarGenerationHandler",
    "AvatarGenerationPayload",
    "AvatarGenerator",
    "AvatarResult",
    "AvatarStatus",
    "JobTypeCarrier",
    "avatar_billing_key",
    "avatar_idempotency_key",
    "avatar_queue_available",
    "avatar_queue_ready",
    "avatar_status_from_job",
    "enqueue_avatar_generation",
    "register_avatar_handler",
]

AVATAR_JOB_TYPE = "avatar_generation"

#: What the read side tells the web about the latest avatar job for a persona.
#: ``pending`` = queued or running (keep polling); ``failed`` = dead-lettered or
#: permanently failed (stop polling, say so); ``None`` = nothing in flight.
AvatarStatus = Literal["pending", "failed"]

_PENDING_STATES = frozenset({JobState.QUEUED, JobState.CLAIMED, JobState.RUNNING})
_FAILED_STATES = frozenset({JobState.FAILED, JobState.DEAD})


@runtime_checkable
class JobTypeCarrier(Protocol):
    """Anything that can say which job types it will consume (the in-process worker handle)."""

    @property
    def job_types(self) -> frozenset[str]: ...


def avatar_queue_ready(*, image_backend: object | None, file_storage: object | None) -> bool:
    """The registration-side gate: can a worker in this process generate avatars at all?

    The ``avatar_generation`` tenant registers iff the image backend and the file
    storage its generator needs are composed. There is no flag in this predicate
    any more: the worker registers the handler whenever it CAN run it, and the
    producer decides by asking the worker what it registered
    (:func:`avatar_queue_available`).
    """
    return image_backend is not None and file_storage is not None


def avatar_queue_available(
    *, job_queue: object | None, in_process_worker: object | None, inline_only: bool
) -> bool:
    """The producer-side gate: may the route enqueue an ``avatar_generation`` job?

    True iff a queue exists, a worker runs in this process, that worker carries
    the avatar handler, and the operator has not opted out
    (``PERSONA_API_AVATAR_INLINE_ONLY``). Reading the worker's registered types is
    what makes "enqueue implies handler" structural: a job is never enqueued into
    a process that cannot consume it (the R9-013 poison loop), and a queue object
    without a consumer (a community boot that never started the worker) is not
    mistaken for a durable path.
    """
    if inline_only or job_queue is None or not isinstance(in_process_worker, JobTypeCarrier):
        return False
    return AVATAR_JOB_TYPE in in_process_worker.job_types


def avatar_idempotency_key(persona_id: str, *, regen_token: str | None = None) -> str:
    """The avatar job's dedup key (D-A0-X-...).

    The create-time key fires exactly once per persona. A regeneration carries a
    per-request ``regen_token`` so it is never deduped against the create job or
    an earlier regeneration.
    """
    if regen_token is None:
        return f"avatar:{persona_id}:create"
    return f"avatar:{persona_id}:regen:{regen_token}"


def avatar_billing_key(persona_id: str, *, regen_token: str | None = None) -> str:
    """The idempotent ledger key for one avatar charge (Spec M3, D-M3-R5).

    The create-time key is ``avatar:{persona_id}`` (unchanged from the request
    path, so existing ledger rows keep deduping a redelivered create). A
    regeneration is a new generation the owner asked for, so it carries its own
    key and is charged in its own right.
    """
    if regen_token is None:
        return f"avatar:{persona_id}"
    return f"avatar:{persona_id}:regen:{regen_token}"


class AvatarGenerationPayload(JobPayload):
    """The avatar job payload.

    ``regenerate`` + ``request_id`` distinguish an owner-requested regeneration
    from the create-time job; both default so rows enqueued before they existed
    still parse.
    """

    persona_id: str
    regenerate: bool = False
    request_id: str | None = None

    @property
    def regen_token(self) -> str | None:
        """The per-request token a regeneration keys on; ``None`` for a create job."""
        return self.request_id if self.regenerate else None


class AvatarResult(BaseModel):
    """The outcome of one avatar generation (the generator's return)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    avatar_url: str
    cost_micros: int = 0
    provider: str = ""


@runtime_checkable
class AvatarGenerator(Protocol):
    """Generates, persists and bills an avatar; returns its url, or ``None`` if declined.

    ``None`` is a no-op outcome (no backend configured, content rejected) — NOT a
    failure: the persona simply keeps no avatar. Implementations MUST persist at a
    deterministic per-persona path so a re-delivery's regeneration overwrites
    rather than orphaning bytes, and MUST charge the owner under ``billing_key``
    so a redelivery does not double-charge.
    """

    async def generate(
        self, *, persona_id: str, owner_id: str, yaml_str: str, billing_key: str
    ) -> AvatarResult | None: ...


class AvatarGenerationHandler:
    """Idempotent avatar generation. Skip-if-set + compare-and-set on create; replace on regen."""

    def __init__(self, *, generator: AvatarGenerator) -> None:
        self._generator = generator

    async def handle(self, payload: AvatarGenerationPayload, context: JobContext) -> None:
        persona_id = payload.persona_id
        # Skip-if-already-set: the durable idempotency no-op. Owner-scoped read.
        with context.connection() as conn:
            row = conn.execute(
                select(personas.c.avatar_url, personas.c.yaml).where(personas.c.id == persona_id)
            ).one_or_none()
        if row is None:
            return  # persona deleted between enqueue and run — nothing to do.
        if row.avatar_url is not None and not payload.regenerate:
            return  # already generated — true no-op (no re-gen, no orphan).

        result = await self._generator.generate(
            persona_id=persona_id,
            owner_id=context.owner_id,
            yaml_str=row.yaml,
            billing_key=avatar_billing_key(persona_id, regen_token=payload.regen_token),
        )
        if result is None:
            return  # generation declined (no backend / rejected) — no avatar, no error.

        context.meter(
            amount_micros=result.cost_micros,
            kind="model",
            detail={"provider": result.provider, "surface": "avatar"},
        )
        # Create: compare-and-set, only if STILL null, so a concurrent re-delivery
        # that also generated cannot overwrite — exactly one avatar_url wins.
        # Regenerate: the owner asked for a new portrait, so the current one is
        # replaced whatever it was. ``avatar_source`` is co-written ``'generated'``
        # in the SAME ``.values(...)`` (Spec R3, R3-D-3) so the "exactly one
        # avatar_url wins" invariant extends to provenance — no window where the
        # url is set but provenance is NULL. The Art. 50 disclosure derives from it.
        stmt = update(personas).where(personas.c.id == persona_id)
        if not payload.regenerate:
            stmt = stmt.where(personas.c.avatar_url.is_(None))
        with context.connection() as conn:
            updated = conn.execute(
                stmt.values(avatar_url=result.avatar_url, avatar_source="generated")
            ).rowcount
        # Spec P6 (D4-d): announce "persona is ready" only when THIS delivery set
        # the avatar (a compare-and-set loser doesn't double-signal; the (owner,
        # kind, ref_id) idempotency key dedups regardless). Best-effort in its OWN
        # owner-scoped transaction (context.connection()), separate from the avatar
        # write above — a feed-write failure can never fail the job (D-P6-12).
        if updated:
            try:
                with context.connection() as conn:
                    persona_service.write_persona_ready(conn, persona_id)
            except Exception as exc:  # noqa: BLE001 — advisory; never fail the job
                _log.warning(
                    "persona-ready notification write failed persona={pid}: {err}",
                    pid=persona_id,
                    err=str(exc),
                )


def register_avatar_handler(registry: JobRegistry, generator: AvatarGenerator) -> None:
    """Register the avatar handler on ``registry`` with its declared idempotency.

    Idempotency mechanism = skip-if-already-set (the ``avatar_url`` marker) for a
    create job, a per-request key for a regeneration; the proof is the
    forced-redelivery test (criterion 2). Short lease (seconds-scale), a small
    retry budget.
    """
    registry.register(
        JobTypeSpec(
            type=AVATAR_JOB_TYPE,
            payload_model=AvatarGenerationPayload,
            handler=AvatarGenerationHandler(generator=generator),
            idempotency_key=lambda p: avatar_idempotency_key(
                p.persona_id, regen_token=p.regen_token
            ),
            retry=RetryPolicy(max_attempts=3),
            lease=SHORT_LEASE,
        )
    )


def enqueue_avatar_generation(
    queue: JobQueue, *, persona_id: str, owner_id: str, regen_token: str | None = None
) -> None:
    """Enqueue an avatar job. ``owner_id`` is the authenticated owner.

    Without ``regen_token`` this is the create-time job: a duplicate enqueue (same
    persona) is a no-op via the idempotency key, safe to call on every create.
    With one it is an owner-requested regeneration under its own key. The worker
    runs it; the avatar appears on a later GET.
    """
    payload = AvatarGenerationPayload(
        persona_id=persona_id, regenerate=regen_token is not None, request_id=regen_token
    )
    queue.enqueue(
        type=AVATAR_JOB_TYPE,
        owner_id=owner_id,
        payload=payload.model_dump(exclude_defaults=True),
        idempotency_key=avatar_idempotency_key(persona_id, regen_token=regen_token),
    )


def avatar_status_from_job(record: JobRecord | None) -> AvatarStatus | None:
    """Map the latest avatar job for a persona onto what the web should do about it.

    Queued, claimed or running → ``"pending"`` (keep polling). Dead-lettered or
    permanently failed → ``"failed"`` (stop polling; the persona keeps the default
    avatar and the page can say so). Succeeded, or no job at all → ``None``.
    """
    if record is None:
        return None
    if record.state in _PENDING_STATES:
        return "pending"
    if record.state in _FAILED_STATES:
        return "failed"
    return None
