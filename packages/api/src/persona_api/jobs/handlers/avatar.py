"""The avatar-generation job handler — A0's first durable tenant (Spec A0, T9).

Replaces the create-path ``BackgroundTasks`` avatar hook with a durable job: an
avatar survives an api restart between create and generation, and the handler
proves the **at-least-once contract** (criterion 2) — a re-delivery (handler
retry OR lease-expiry reclaim) leaves EXACTLY ONE valid avatar, never a duplicate.

**Idempotency mechanism — SKIP-IF-ALREADY-SET (declared at registration).**
``personas.avatar_url`` is the durable marker: the handler no-ops if it is already
set, so the common re-delivery (the side effect completed before the crash)
re-runs as a true no-op — no wasteful regeneration, no orphan bytes. The rarer
kill-between-generate-and-set case re-generates; the generator persists at a
deterministic per-persona path so the re-gen OVERWRITES rather than orphaning, and
the ``WHERE avatar_url IS NULL`` compare-and-set sets exactly one url even under
concurrent re-delivery. Avatar generation is non-deterministic, so the gate is
"one valid avatar_url, no corruption," not "byte-identical output."

The concrete provider wiring (an :class:`AvatarGenerator` over the imagegen
service) is composed at the worker root by the orchestrator at the enqueue→worker
cutover; this module is mechanism-agnostic over that seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

from persona.jobs import SHORT_LEASE, JobPayload, JobTypeSpec, RetryPolicy
from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, update

from persona_api.db.models import personas
from persona_api.services import persona_service

_log = get_logger("api.jobs.avatar")

if TYPE_CHECKING:
    from persona.jobs import JobContext, JobRegistry

    from persona_api.jobs.queue import JobQueue

__all__ = [
    "AVATAR_JOB_TYPE",
    "AvatarGenerationHandler",
    "AvatarGenerationPayload",
    "AvatarGenerator",
    "AvatarResult",
    "avatar_idempotency_key",
    "avatar_queue_ready",
    "enqueue_avatar_generation",
    "register_avatar_handler",
]

AVATAR_JOB_TYPE = "avatar_generation"


def avatar_queue_ready(
    *, avatar_via_queue: bool, image_backend: object | None, file_storage: object | None
) -> bool:
    """THE one queue-cutover gate, shared by producer and registration (R9-013).

    An ``avatar_generation`` job may be enqueued **iff the worker will have a
    handler for it**: the cutover flag is on AND the image backend + file storage
    the handler's generator needs are composed. The create route (the producer)
    and :func:`~persona_api.background.worker_root.build_worker_registry` (the
    registration) MUST both consult this predicate — a producer-only read of the
    flag is exactly the half-shipped cutover that enqueued unknown-type jobs into
    a handler-less worker (the R9-013 poison loop).
    """
    return bool(avatar_via_queue) and image_backend is not None and file_storage is not None


def avatar_idempotency_key(persona_id: str) -> str:
    """The create-time avatar key — fires exactly once per persona (D-A0-X-...).

    A later *regeneration* must use a DISTINCT key (e.g.
    ``avatar:{persona_id}:regen:{request_id}``) so it is not deduped as a no-op.
    """
    return f"avatar:{persona_id}:create"


class AvatarGenerationPayload(JobPayload):
    """The avatar job payload — just the persona to generate for."""

    persona_id: str


class AvatarResult(BaseModel):
    """The outcome of one avatar generation (the generator's return)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    avatar_url: str
    cost_micros: int = 0
    provider: str = ""


@runtime_checkable
class AvatarGenerator(Protocol):
    """Generates + persists an avatar; returns its url, or ``None`` if declined.

    ``None`` is a no-op outcome (no backend configured, content rejected) — NOT a
    failure: the persona simply keeps no avatar. Implementations MUST persist at a
    deterministic per-persona path so a re-delivery's regeneration overwrites
    rather than orphaning bytes.
    """

    async def generate(
        self, *, persona_id: str, owner_id: str, yaml_str: str
    ) -> AvatarResult | None: ...


class AvatarGenerationHandler:
    """Idempotent avatar generation. Skip-if-set + compare-and-set upsert."""

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
        if row.avatar_url is not None:
            return  # already generated — true no-op (no re-gen, no orphan).

        result = await self._generator.generate(
            persona_id=persona_id, owner_id=context.owner_id, yaml_str=row.yaml
        )
        if result is None:
            return  # generation declined (no backend / rejected) — no avatar, no error.

        context.meter(
            amount_micros=result.cost_micros,
            kind="model",
            detail={"provider": result.provider, "surface": "avatar"},
        )
        # Compare-and-set: only set if STILL null, so a concurrent re-delivery that
        # also generated cannot overwrite — exactly one avatar_url wins. ``avatar_source``
        # is co-written ``'generated'`` in the SAME ``.values(...)`` (Spec R3, R3-D-3) so the
        # "exactly one avatar_url wins" invariant extends to provenance — no window where the
        # url is set but provenance is NULL. The Art. 50 disclosure derives from this signal.
        with context.connection() as conn:
            updated = conn.execute(
                update(personas)
                .where(personas.c.id == persona_id, personas.c.avatar_url.is_(None))
                .values(avatar_url=result.avatar_url, avatar_source="generated")
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

    Idempotency mechanism = skip-if-already-set (the ``avatar_url`` marker); the
    proof is the forced-redelivery test (criterion 2). Short lease (seconds-scale),
    a small retry budget.
    """
    registry.register(
        JobTypeSpec(
            type=AVATAR_JOB_TYPE,
            payload_model=AvatarGenerationPayload,
            handler=AvatarGenerationHandler(generator=generator),
            idempotency_key=lambda p: avatar_idempotency_key(p.persona_id),
            retry=RetryPolicy(max_attempts=3),
            lease=SHORT_LEASE,
        )
    )


def enqueue_avatar_generation(queue: JobQueue, *, persona_id: str, owner_id: str) -> None:
    """Enqueue a create-time avatar job. ``owner_id`` is the authenticated owner.

    A duplicate enqueue (same persona) is a no-op via the idempotency key — safe
    to call on every create. The worker runs it; the avatar appears on a later GET.
    """
    queue.enqueue(
        type=AVATAR_JOB_TYPE,
        owner_id=owner_id,
        payload={"persona_id": persona_id},
        idempotency_key=avatar_idempotency_key(persona_id),
    )
