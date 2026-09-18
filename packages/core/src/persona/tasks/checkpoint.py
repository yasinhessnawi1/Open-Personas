"""The task checkpoint — the durable working state that carries between legs (Spec A2, T1).

The checkpoint is the architectural lock (D-A2-1). A **task** spans days through many
bounded agentic **legs**; the only thing that carries between legs is this checkpoint —
*progress (conclusions), intent (plan + next step), pointers (workspace artifacts), open
questions* — **never transcripts**. Its shape decides the amnesia/ossification twin:

- The **accumulating core** (``progress_conclusions`` + ``decisions`` + ``lessons``, and
  from Spec W1 the ``queries_run`` + ``sources_seen`` ledgers) is the only part that grows,
  and the only part the size bound governs. It is bounded so a leg records *conclusions,
  not history* (anti-amnesia without bloat).
- The **regenerated** fields (``current_plan`` / ``next_step``) are rewritten every leg and
  recited last in reconstruction (anti-ossification); they are explicitly **outside** the cap.
- **Pointers** (``artifact_pointers`` / ``event_log_cursor``) hold bulk by reference, so the
  checkpoint stays a few KB while detail remains restorable.

The **task contract** is NOT here — it lives on the task entity (frozen, A4-authored) so a leg
structurally cannot edit it. This module is pure: a frozen-Pydantic shape + a size-bound gate.
No DB, no I/O, no clock (``updated_at`` is injected by the caller). The durable RLS store
(persona-api) and the leg executor (persona-runtime) compose these.

See ``docs/specs/phase3/spec_A2/decisions.md`` (D-A2-1) and ``docs/research/spec_A2.md`` §1.3.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from persona.errors import CheckpointTooLargeError
from persona.skills import count_tokens

__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "DEFAULT_CHECKPOINT_TOKEN_BUDGET",
    "ArtifactPointer",
    "Decision",
    "TaskCheckpoint",
    "checkpoint_token_count",
    "enforce_checkpoint_budget",
]

#: Checkpoint schema version. Bump on any breaking field-set change (mirrors the persona
#: schema-version discipline). Recorded on every checkpoint.
CHECKPOINT_SCHEMA_VERSION = "1.0"

#: Default token budget for the accumulating core (D-A2-1). Reuses the project's
#: "one turn's worth of high-signal content" number (the 2000-token skill budget). The
#: api layer may override it via ``PERSONA_TASK_CHECKPOINT_TOKEN_BUDGET``; core stays
#: env-free by taking the budget as an injected argument.
DEFAULT_CHECKPOINT_TOKEN_BUDGET = 2000


def _ensure_utc(value: datetime) -> datetime:
    """Reject naive datetimes; normalise tz-aware ones to UTC.

    Mirrors the schema-/schedule-layer rule (spec_01 §11.4): every stored timestamp is
    tz-aware UTC so checkpoints, fires, and audit times share one frame.
    """
    if value.tzinfo is None:
        msg = "naive datetime not allowed; use datetime.now(UTC) or attach a tzinfo"
        raise ValueError(msg)
    return value.astimezone(UTC)


class Decision(BaseModel):
    """One decision a leg made, with its rationale (D-A2-1).

    The rationale is mandatory: later legs must inherit *why*, not just *what*, or they
    collide on conflicting decisions (the long-horizon failure Cognition documents).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: str
    rationale: str
    leg_id: str


class ArtifactPointer(BaseModel):
    """A reference into the workspace (or elsewhere) — bulk held by pointer, not payload.

    ``kind`` is the reference class (e.g. ``"workspace"`` / ``"url"`` / ``"store"``);
    ``ref`` is the path/URL/id the leg dereferences just-in-time during reconstruction.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: str
    ref: str


class TaskCheckpoint(BaseModel):
    """The durable working state written at every leg end (D-A2-1).

    Frozen boundary type. ``content_hash`` is computed at construction (tamper-evident,
    audit) over the content fields — it deliberately ignores ``updated_at`` so a re-write
    at a new time with identical content keeps a stable hash. The size bound
    (:func:`enforce_checkpoint_budget`) governs the accumulating core only.

    Attributes:
        task_id: The owning task.
        leg_id: The leg that wrote this checkpoint.
        checkpoint_seq: Monotonic append-only sequence per task (the idempotency anchor
            the leg job's CAS keys on — A2-R-4; populated/used by the durable store).
        progress_conclusions: Distilled findings (what is established), not events. Capped.
        decisions: Decisions-with-rationale made so far. Capped.
        lessons: Bounded wrong-turns to avoid re-walking. Capped.
        queries_run: What earlier legs already asked the world (Spec W1, D-W1-16): the
            canonical arguments of their search calls. Capped; the writer folds the oldest
            into a count marker rather than truncating an entry. This is how leg N+1 knows
            not to run leg N's searches again.
        sources_seen: The URLs earlier legs already read (Spec W1, D-W1-16). Capped the same
            way. A source listed here has been looked at; its findings are in the
            conclusions, so fetching it again buys nothing.
        current_plan: The remaining steps, regenerated each leg. Not capped.
        next_step: The single concrete action this leg's successor runs first. Not capped.
        open_questions: What is still unresolved.
        blocked_on: The **obstacle**: what has to change OUTSIDE the task before another
            leg can make progress, in one human sentence. ``None`` whenever the work can
            carry on, which is every ordinary leg. It is deliberately narrower than "why
            this task is waiting": the task state already says a task waits, and a question
            the persona asked lives in ``open_questions`` and is answered by the person
            reading it, so a leg that did its work and ended on a question is NOT blocked.
            Written where a park learns the obstacle (an approval it cannot grant itself, a
            leg that died after its retries) and never carried forward, so a task that got
            moving again is not still described as stuck.
        idle_until: The instant before which there is **nothing to do**, when the leg that
            wrote this found one (a sale that opens Thursday, a reply due Monday). The
            executor turns it into the outcome's timed wait, so the next leg is scheduled
            for then instead of at once. Written only by a leg the box stopped (a finished
            leg has no successor to delay), validated by the writer against the current
            time and a ceiling, and never carried forward: a leg that ran has, by running,
            stopped idling.
        artifact_pointers: References to workspace artifacts / key sources.
        event_log_cursor: Offset/id into the durable run records for just-in-time recall.
        schema_version: The checkpoint schema version.
        updated_at: When this checkpoint was written (tz-aware UTC; injected by the caller).
        content_hash: SHA-256 over the content fields. Computed if empty; verified if given.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    leg_id: str
    checkpoint_seq: int = Field(ge=0)

    progress_conclusions: tuple[str, ...] = ()
    decisions: tuple[Decision, ...] = ()
    lessons: tuple[str, ...] = ()
    # Spec W1 (D-W1-16): defaults, so every checkpoint written before this field existed
    # validates unchanged through this frozen extra="forbid" model.
    queries_run: tuple[str, ...] = ()
    sources_seen: tuple[str, ...] = ()

    current_plan: tuple[str, ...] = ()
    next_step: str = ""

    open_questions: tuple[str, ...] = ()
    blocked_on: str | None = None
    idle_until: datetime | None = None

    artifact_pointers: tuple[ArtifactPointer, ...] = ()
    event_log_cursor: str | None = None

    schema_version: str = CHECKPOINT_SCHEMA_VERSION
    updated_at: datetime
    content_hash: str = ""

    @field_validator("updated_at", mode="after")
    @classmethod
    def _updated_at_must_be_tz_aware(cls, value: datetime) -> datetime:
        return _ensure_utc(value)

    @field_validator("idle_until", mode="after")
    @classmethod
    def _idle_until_must_be_tz_aware(cls, value: datetime | None) -> datetime | None:
        return _ensure_utc(value) if value is not None else None

    @model_validator(mode="after")
    def _populate_or_verify_content_hash(self) -> TaskCheckpoint:
        expected = self._compute_content_hash()
        if not self.content_hash:
            object.__setattr__(self, "content_hash", expected)
        elif self.content_hash != expected and not self._matches_pre_ledger_hash():
            msg = "content_hash does not match the checkpoint content (tamper check)"
            raise ValueError(msg)
        return self

    def _matches_pre_ledger_hash(self) -> bool:
        """Does this hash match the field set as it stood before the W1 ledgers existed?

        Every checkpoint already in a database was hashed WITHOUT ``queries_run`` and
        ``sources_seen``. Adding them to the model adds two keys to the hashed payload, so
        without this path every stored checkpoint would read back as tampered and every
        running task would break on its next leg.

        Deliberately narrow. It applies only when both ledgers are EMPTY, which is the only
        state a pre-W1 checkpoint can deserialize into, so it can never excuse a checkpoint
        whose ledgers were edited: give either one a value and the current hash is the only
        one that counts. A checkpoint written from here on carries the current hash.
        """
        if self.queries_run or self.sources_seen:
            return False
        data = self.model_dump(
            mode="json",
            exclude=self._unhashed() | {"queries_run", "sources_seen"},
        )
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return self.content_hash == hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _unhashed(self) -> set[str]:
        """The fields the hash leaves out: the hash itself, the write time, and an unset idle.

        ``idle_until`` joined the model after every checkpoint in production had been hashed
        without it. Excluding it while it is ``None`` keeps every stored row reading back
        with the hash it was written with, and including it once set keeps the instant a
        leg named tamper-evident. The ledgers took a separate legacy path because they are
        tuples, whose empty value still serialises into the payload.
        """
        unhashed = {"content_hash", "updated_at"}
        if self.idle_until is None:
            unhashed.add("idle_until")
        return unhashed

    def _compute_content_hash(self) -> str:
        """SHA-256 over the content fields (excludes ``updated_at`` + ``content_hash``).

        Deterministic: ``model_dump(mode="json")`` + ``sort_keys`` → same content, same hash,
        regardless of key order. Tuple order is preserved (it is semantically meaningful).
        """
        data = self.model_dump(mode="json", exclude=self._unhashed())
        payload = json.dumps(data, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def checkpoint_token_count(checkpoint: TaskCheckpoint) -> int:
    """Count the tokens in the checkpoint's *accumulating core* (D-A2-1, D-W1-16).

    Counts ``progress_conclusions`` + each decision (``decision`` + ``rationale``) +
    ``lessons`` + the ``queries_run`` / ``sources_seen`` ledgers: the fields the size bound
    governs. The ledgers are IN the core because they accumulate exactly like conclusions do
    and would otherwise grow without a gate. The regenerated plan/next-step and the pointers
    are deliberately excluded: the cap forces *conclusions, not history*, not a small plan.

    Args:
        checkpoint: The checkpoint to measure.

    Returns:
        The token count of the accumulating core (``tiktoken cl100k_base``).
    """
    parts: list[str] = list(checkpoint.progress_conclusions)
    for decision in checkpoint.decisions:
        parts.append(decision.decision)
        parts.append(decision.rationale)
    parts.extend(checkpoint.lessons)
    parts.extend(checkpoint.queries_run)
    parts.extend(checkpoint.sources_seen)
    return count_tokens(" ".join(parts))


def enforce_checkpoint_budget(
    checkpoint: TaskCheckpoint,
    *,
    token_budget: int = DEFAULT_CHECKPOINT_TOKEN_BUDGET,
) -> None:
    """Raise if the checkpoint's accumulating core exceeds ``token_budget`` (D-A2-1).

    The boundary gate behind the amnesia/ossification twin: a leg that overruns the budget
    must reflect-and-compact (merge conclusions, keep pointers) *before* persisting; a write
    that still exceeds it after compaction fails fast here rather than persisting an
    unbounded, ossifying checkpoint. A read-only check (CQS): it never mutates the checkpoint.

    Args:
        checkpoint: The checkpoint to gate.
        token_budget: The cap on the accumulating core (default
            :data:`DEFAULT_CHECKPOINT_TOKEN_BUDGET`; the api layer injects the configured value).

    Raises:
        CheckpointTooLargeError: If the accumulating core exceeds ``token_budget``.
    """
    count = checkpoint_token_count(checkpoint)
    if count > token_budget:
        raise CheckpointTooLargeError(
            "checkpoint accumulating core exceeds the token budget",
            context={
                "task_id": checkpoint.task_id,
                "leg_id": checkpoint.leg_id,
                "checkpoint_seq": str(checkpoint.checkpoint_seq),
                "token_count": str(count),
                "token_budget": str(token_budget),
            },
        )
