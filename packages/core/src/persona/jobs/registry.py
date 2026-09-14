"""The typed job-type registry — Toolbox-style, duplicate-rejecting (Spec A0, T1).

Maps a job ``type`` to its :class:`JobTypeSpec`: the frozen-Pydantic payload
model, the idempotent handler, the idempotency-key recipe (declared AT
registration — D-A0-X-idempotency-key-convention), and the retry + lease
policies (D-A0-1/D-A0-2). Mirrors the persona ``Toolbox`` explicit-registry
pattern (no dynamic dispatch); registration rejects a duplicate type so a
second runtime can never silently shadow a handler.

The registry is the one place that knows, per type, how to dedup an enqueue and
how to reconstruct the concrete payload from stored JSONB (the T2/T3 seam).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Generic, Protocol, TypeVar, runtime_checkable

from persona.errors import DuplicateJobTypeError, UnknownJobTypeError
from persona.jobs.models import MEDIUM_LEASE, JobPayload, LeasePolicy, RetryPolicy

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable, Iterable, Mapping
    from contextlib import AbstractContextManager

    from sqlalchemy import Connection

__all__ = ["JobContext", "JobHandler", "JobRegistry", "JobTypeSpec"]

P = TypeVar("P", bound=JobPayload)
# A handler only consumes its payload (parameter position), so the Protocol's
# type var is contravariant — a handler for the base payload can stand in for a
# handler of a subtype.
P_contra = TypeVar("P_contra", bound=JobPayload, contravariant=True)


@runtime_checkable
class JobContext(Protocol):
    """The execution context a worker passes to a handler — owner-scoped, only.

    The job's owner (the RLS scope and idempotency-key material) plus the ONLY
    database access a handler is given: an owner-scoped connection (RLS-bound to
    ``owner_id``). A handler never receives the worker's cross-tenant dispatch
    engine, so it has no in-band path to another tenant's data — the RLS choke
    point is structural, not disciplinary (D-A0-X-rls-chokepoint). The worker's
    concrete context (persona-api, T4) implements this against the ``persona_app``
    RLS engine; handlers depend on this abstraction, never on the worker.
    """

    @property
    def owner_id(self) -> str: ...

    @property
    def job_id(self) -> str:
        """The durable job id — the attribution key for metered spend."""
        ...

    def connection(self) -> AbstractContextManager[Connection]:
        """An owner-scoped DB connection (a transaction with the owner GUC set).

        The sole DB affordance a handler gets. Every query through it runs under
        the job owner's RLS scope; a read of another tenant's rows returns zero.
        """
        ...

    def meter(
        self, *, amount_micros: int, kind: str, detail: Mapping[str, str] | None = None
    ) -> None:
        """Record a spend event attributable to THIS job (criterion 9, D-A0-X-metering-bar).

        ``kind`` is the spend class (``"model"`` / ``"sandbox"`` / ``"external"``);
        ``amount_micros`` is the spend in ledger micros (a hundredth of a US cent, so
        10 000 to the dollar — ``persona.tasks.MICROS_PER_DOLLAR``; NOT micro-dollars),
        an integer to avoid float drift. Recorded into the EXISTING observability
        ledger keyed by ``job_id`` — A0 *meters* (records), A2 accounts, A3
        enforces. This never deducts credits; a charged handler does that
        separately via the credits service (the deduct path, proven later).
        Best-effort: a metering failure never breaks the job.
        """
        ...


@runtime_checkable
class JobHandler(Protocol[P_contra]):
    """An idempotent unit of background work for one payload type.

    At-least-once delivery (D-A0-X-claim-then-commit) means a handler MUST be
    safe to re-run: a worker can die after the side effect but before the
    success write, so the job runs again. Each registered handler proves its
    re-delivery safety with a forced-kill / lease-expiry test (criterion 2, the
    hard gate) — the property is tested per handler, not assumed. CQS: a handler
    performs work and returns ``None``; outcomes are durable job state, not a
    return value.
    """

    async def handle(self, payload: P_contra, context: JobContext) -> None: ...


@dataclass(frozen=True)
class JobTypeSpec(Generic[P]):
    """One registered job type.

    A composition-root wiring object (it holds a handler, a type, and callables
    and is never serialised) — hence a frozen dataclass, not a Pydantic boundary
    model. The ``idempotency_key`` recipe is declared here, at registration, so
    every type's dedup identity is explicit and reviewable in one place.

    Attributes:
        type: The registry key (and the durable ``Job.type``).
        payload_model: The concrete frozen :class:`JobPayload` subclass.
        handler: The idempotent handler for this type.
        idempotency_key: Builds the operation+intent-scoped dedup key from a
            payload — e.g. ``lambda p: f"avatar:{p.persona_id}:create"`` for the
            avatar-create handler (a regen uses a distinct recipe).
        retry: The retry/backoff/dead-letter policy.
        lease: The lease/heartbeat class (defaults to MEDIUM).
    """

    type: str
    # ``builtins.type`` because the ``type`` field above shadows the builtin
    # name inside this class body.
    payload_model: builtins.type[P]
    handler: JobHandler[P]
    idempotency_key: Callable[[P], str]
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    lease: LeasePolicy = MEDIUM_LEASE


class JobRegistry:
    """Explicit, duplicate-rejecting registry of job types (Toolbox-style).

    Construct with an iterable of specs (each duplicate-checked) and/or
    :meth:`register` incrementally from the worker composition root. The
    container is heterogeneous over payload types by design; per-entry type
    safety is preserved by the generic :class:`JobTypeSpec` at registration.
    """

    def __init__(self, specs: Iterable[JobTypeSpec[Any]] = ()) -> None:
        # ``JobTypeSpec[Any]``: the registry spans many payload types, so the
        # container is necessarily existentially typed. Each entry was created
        # as a concrete ``JobTypeSpec[ConcretePayload]`` (type-checked there);
        # the ``Any`` is only the erasure needed to hold them together.
        self._specs: dict[str, JobTypeSpec[Any]] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: JobTypeSpec[Any]) -> None:
        """Register a job type. Raises :class:`DuplicateJobTypeError` on a repeat."""
        if spec.type in self._specs:
            raise DuplicateJobTypeError(
                "duplicate job type registration",
                context={"type": spec.type},
            )
        self._specs[spec.type] = spec

    def get(self, job_type: str) -> JobTypeSpec[Any]:
        """Resolve a job type. Raises :class:`UnknownJobTypeError` if unregistered."""
        spec = self._specs.get(job_type)
        if spec is None:
            raise UnknownJobTypeError(
                "unknown job type",
                context={"type": job_type, "known": ", ".join(sorted(self._specs))},
            )
        return spec

    def types(self) -> list[str]:
        """Sorted registered job-type names."""
        return sorted(self._specs)

    def idempotency_key_for(self, job_type: str, payload: JobPayload) -> str:
        """Build the idempotency key for ``payload`` via its registered recipe."""
        return self.get(job_type).idempotency_key(payload)

    def parse_payload(self, job_type: str, data: dict[str, Any]) -> JobPayload:
        """Reconstruct a concrete payload from stored JSONB (the T2/T3 seam).

        Validates ``data`` against the type's registered payload model, so a
        malformed or schema-drifted payload fails loud at claim time rather than
        three layers into the handler.
        """
        payload: JobPayload = self.get(job_type).payload_model.model_validate(data)
        return payload
