"""The `Run` state and the `CancelToken` control object (spec §7).

A :class:`Run` is the serialisable record of one agentic execution: its task,
status, the ordered :class:`~persona_runtime.agentic.step.Step` list, the final
output, and timing. It is **frozen Pydantic v2** (D-06-1) because it crosses the
spec-08 DB/JSON boundary (acceptance #10 serialises it) — the loop holds mutable
working state (a step list, a status var) and constructs the frozen ``Run`` at
the end and at each snapshot point spec 08 persists.

:class:`CancelToken` is the deliberate exception (D-06-1): it is mutable control
state shared between the caller (who calls :meth:`CancelToken.cancel`) and the
loop (who reads :attr:`CancelToken.is_cancelled` at each step boundary). It never
crosses a serialisation boundary — the *fact* of cancellation is recorded in
``Run.status``, which does. So it is a plain class, not Pydantic, not frozen.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from persona_runtime.agentic.step import Step  # noqa: TC001 — Pydantic needs runtime ref

__all__ = ["CancelToken", "Run", "RunStatus", "StepUsage"]


class RunStatus(StrEnum):
    """The terminal (or in-flight) status of a run (spec §7).

    ``RUNNING`` is the only non-terminal value. The four terminal values are
    distinct so a caller (or an automated pipeline) never mistakes a
    ``MAX_STEPS_REACHED`` or ``ERROR`` outcome for ``COMPLETED`` (D-06-2): a
    best-effort max-steps summary is an *output*, not a success signal.

    Values:
        RUNNING: The loop is executing.
        COMPLETED: The model produced a final answer (``[FINAL]``).
        CANCELLED: The caller cancelled via the :class:`CancelToken`.
        MAX_STEPS_REACHED: The step budget was exhausted; ``output`` holds a
            best-effort summary.
        ERROR: An unrecoverable error terminated the run; ``error`` holds the
            description.
    """

    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    MAX_STEPS_REACHED = "max_steps_reached"
    ERROR = "error"


class Run(BaseModel):
    """The serialisable record of one agentic run (spec §7).

    Frozen + ``extra="forbid"``. ``id`` defaults to a fresh UUID; the persona
    and task are required; ``steps`` accumulates the cycle history; ``output``
    is the deliverable (or best-effort summary); ``error`` is set only on
    ``ERROR``. Datetimes are tz-aware UTC (naive raises at construction).

    Attributes:
        id: Stable run identifier (UUID4 by default).
        persona_id: The persona this run belongs to.
        task: The original task string the run was created to execute.
        status: The run's :class:`RunStatus`.
        steps: The ordered plan-act-reflect cycle history.
        output: The final deliverable, or the best-effort summary at
            ``MAX_STEPS_REACHED`` / ``CANCELLED`` (R9-109: a cancelled leg carries
            its work forward too, since the checkpoint accumulates progress from
            this field alone). ``None`` while running, on early error, when a run
            is cancelled before its first step, or when the salvage summary fails.
        error: The error description; set only when ``status == ERROR``.
        started_at: tz-aware UTC start time.
        finished_at: tz-aware UTC end time; ``None`` while running.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(default_factory=lambda: str(uuid4()))
    persona_id: str
    task: str
    status: RunStatus
    steps: list[Step] = Field(default_factory=list)
    output: str | None = None
    error: str | None = None
    started_at: datetime
    finished_at: datetime | None = None

    @field_validator("started_at", "finished_at", mode="after")
    @classmethod
    def _must_be_tz_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            msg = "naive datetime not allowed on Run timestamps; use datetime.now(UTC)"
            raise ValueError(msg)
        return value.astimezone(UTC) if value is not None else None


class StepUsage(BaseModel):
    """One step's model-call usage, surfaced for INCREMENTAL billing (Spec M3, T4a).

    NOT persisted on ``Run``/``Step`` (billing is the api's concern, not the loop's)
    and NOT an SSE :class:`~persona_runtime.agentic.events.RunEvent` (billing data
    stays off the client stream + out of the persisted event log). The loop passes
    it to the optional ``on_step_usage`` callback right after each step's model call
    so the caller can meter the step's real cost and cut the run off at the NEXT
    step boundary on exhaustion. Frozen boundary type.

    Attributes:
        step: The zero-based step index this usage belongs to.
        provider: The served backend's provider name (for pricing).
        model: The served backend's model name (for pricing).
        prompt_tokens: Prompt tokens the step's model call consumed.
        completion_tokens: Completion tokens the step's model call emitted.
        cost_usd: Response-side actual cost in USD (OpenRouter usage.cost), or
            ``None`` — the caller then prices from the token counts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    step: int = Field(ge=0)
    provider: str
    model: str
    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    cost_usd: float | None = Field(default=None, ge=0.0)


class CancelToken:
    """Caller-held cancellation control for a run (spec §7; D-06-1).

    A plain mutable class — NOT Pydantic, NOT frozen. The caller constructs one,
    hands it to :meth:`AgenticLoop.run`, and calls :meth:`cancel` to request
    termination. The loop checks :attr:`is_cancelled` at each step boundary
    (never mid-step — an in-flight tool dispatch completes; the next step does
    not start, acceptance #6).
    """

    def __init__(self) -> None:
        self._cancelled = False

    @property
    def is_cancelled(self) -> bool:
        """True once :meth:`cancel` has been called."""
        return self._cancelled

    def cancel(self) -> None:
        """Request cancellation. Idempotent."""
        self._cancelled = True
