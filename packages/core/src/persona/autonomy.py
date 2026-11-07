"""Autonomy primitives — the proactive-autonomy preference axis (spec 21).

The persona's *autonomy preference* (``"cautious" | "balanced" | "decisive"``)
tunes two coupled behaviours: how readily it asks proactive clarifying
questions, and how aggressively it auto-dispatches tasks. This module owns the
core-layer primitives that both the runtime loops (spec 05/06) and the
auto-dispatcher (spec 21 §2.2) consume:

- :data:`AutonomyLevel` — the type alias (single source of the three values,
  reconciled against :class:`persona.schema.persona.Persona.autonomy` by a
  test, not a runtime import — schema must not depend on this module).
- :class:`AmbiguityClass` — the four ambiguity-signal classes. Lives here (not
  in persona-runtime's ``ambiguity.py``) because the gating policy below
  references it and persona-runtime depends on persona-core, never the reverse
  (layering, ENGINEERING_STANDARDS §1.2 hexagonal).
- :class:`AutonomyPolicy` — the per-level behaviour knobs (question caps per
  D-21-5, which ambiguity classes trigger an ask).
- :func:`resolve_autonomy` — the load-time resolver: YAML default overlaid by
  any ``persona_self`` head version under ``logical_id="autonomy"`` (D-21-8 /
  D-21-11). Pure: it takes already-fetched chunks and never touches a store.

The learner that *writes* autonomy updates (``record_autonomy_update``) is
spec 21 T03, built on top of these primitives.
"""

from __future__ import annotations

from enum import StrEnum
from typing import TYPE_CHECKING, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from persona.errors import (
    AutonomyCooldownError,
    ChainConflictError,
    InvalidAutonomyLevelError,
)
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource, mint_chunk_id
from persona.stores.self_facts import SelfFactsStore
from persona.stores.versioning import current_version

if TYPE_CHECKING:
    from collections.abc import Iterable
    from datetime import datetime

    from persona.schema.persona import Persona
    from persona.stores.protocol import MemoryStore

__all__ = [
    "AUTONOMY_LEVELS",
    "AUTONOMY_LOGICAL_ID",
    "AUTONOMY_METADATA_KEY",
    "AUTONOMY_POLICIES",
    "AUTONOMY_SESSION_METADATA_KEY",
    "AmbiguityClass",
    "AutonomyLevel",
    "AutonomyPolicy",
    "policy_for",
    "record_autonomy_update",
    "resolve_autonomy",
]

# The autonomy preference. Kept in sync with Persona.autonomy's inline Literal
# by ``test_autonomy.py::test_autonomy_levels_match_schema_field`` (schema is the
# lower layer and must not import this module, so the values are duplicated and
# the test guards against drift — D-21-11).
AutonomyLevel = Literal["cautious", "balanced", "decisive"]

#: The three valid levels as a set, for membership checks at the boundary.
AUTONOMY_LEVELS: frozenset[str] = frozenset({"cautious", "balanced", "decisive"})

#: The logical-chain id under which the persona_self learner versions autonomy
#: updates in the self_facts store (D-21-8). One chain per persona.
AUTONOMY_LOGICAL_ID: str = "autonomy"

#: The chunk-metadata key carrying the canonical machine-readable level. The
#: chunk ``text`` is a human-readable sentence (for history/audit display); this
#: metadata field is the authoritative value :func:`resolve_autonomy` reads.
AUTONOMY_METADATA_KEY: str = "autonomy_level"

#: The chunk-metadata key recording which session wrote an autonomy update, so
#: the per-session cooldown (D-21-4) is enforceable statelessly from the chain
#: itself (no separate session-state store).
AUTONOMY_SESSION_METADATA_KEY: str = "session_id"


class AmbiguityClass(StrEnum):
    """The four classes of ambiguity a proactive question can address (D-21-1).

    Values:
        MISSING_PARAMETER: A required slot for an impending action is absent
            (recipient, time, amount, target). Class A — precision-biased.
        VAGUE_SCOPE: The deliverable or referent is under-specified ("draft a
            complaint", "fix this"). Class B — precision-biased.
        CONFLICTING_CONSTRAINTS: Mutually tense requirements co-occur (cheap +
            premium, brief + comprehensive). Class C — **detect-and-log only in
            v0.1, never triggers a direct question** (D-21-19); excluded from
            every level's :attr:`AutonomyPolicy.asks_on`.
        SAFETY_CRITICAL_GAP: A destructive / irreversible / sensitive action is
            under-specified. Class D — recall-biased and **bypasses the autonomy
            gate at every level** (present in every level's ``asks_on``).
    """

    MISSING_PARAMETER = "missing_parameter"
    VAGUE_SCOPE = "vague_scope"
    CONFLICTING_CONSTRAINTS = "conflicting_constraints"
    SAFETY_CRITICAL_GAP = "safety_critical_gap"


class AutonomyPolicy(BaseModel):
    """The behaviour knobs for one autonomy level (spec 21 §2.1, D-21-5).

    Frozen + ``extra="forbid"``: a policy is a value object, looked up by level
    and never mutated. The runtime loops read :attr:`asks_on` to gate the
    ambiguity detector and the cap fields to bound question frequency.

    Attributes:
        level: The level this policy describes.
        questions_per_turn: Max proactive questions admitted in one chat turn.
            One for every level (no batching — D-21-5); a breach converts to a
            stated assumption (D-21-18), it does not raise.
        questions_per_run: Max proactive questions admitted in one agentic run
            (D-21-5: cautious 5, balanced 3, decisive 1). A question consumes a
            ``max_steps`` slot regardless (D-21-15).
        asks_on: The ambiguity classes that trigger a question at this level.
            ``SAFETY_CRITICAL_GAP`` is always present (gate bypass);
            ``CONFLICTING_CONSTRAINTS`` is never present (detect-only, D-21-19).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    level: AutonomyLevel
    questions_per_turn: int = Field(ge=0)
    questions_per_run: int = Field(ge=0)
    asks_on: frozenset[AmbiguityClass]


#: The canonical per-level policies. The single source of the D-21-5 caps and
#: the spec §2.1 gating table; ``policy_for`` is the lookup accessor.
AUTONOMY_POLICIES: dict[AutonomyLevel, AutonomyPolicy] = {
    "cautious": AutonomyPolicy(
        level="cautious",
        questions_per_turn=1,
        questions_per_run=5,
        asks_on=frozenset(
            {
                AmbiguityClass.MISSING_PARAMETER,
                AmbiguityClass.VAGUE_SCOPE,
                AmbiguityClass.SAFETY_CRITICAL_GAP,
            }
        ),
    ),
    "balanced": AutonomyPolicy(
        level="balanced",
        questions_per_turn=1,
        questions_per_run=3,
        asks_on=frozenset(
            {
                AmbiguityClass.MISSING_PARAMETER,
                AmbiguityClass.SAFETY_CRITICAL_GAP,
            }
        ),
    ),
    "decisive": AutonomyPolicy(
        level="decisive",
        questions_per_turn=1,
        questions_per_run=1,
        asks_on=frozenset({AmbiguityClass.SAFETY_CRITICAL_GAP}),
    ),
}


def policy_for(level: AutonomyLevel) -> AutonomyPolicy:
    """Return the :class:`AutonomyPolicy` for ``level``.

    Args:
        level: One of the three autonomy levels.

    Returns:
        The frozen policy value object for that level.

    Raises:
        InvalidAutonomyLevelError: ``level`` is not a known level. This guards
            the boundary where an unvalidated string reaches the lookup (the
            type system already rejects it for typed callers).
    """
    policy = AUTONOMY_POLICIES.get(level)
    if policy is None:
        raise InvalidAutonomyLevelError(
            "unknown autonomy level",
            context={"level": str(level), "supported": ",".join(sorted(AUTONOMY_LEVELS))},
        )
    return policy


def resolve_autonomy(
    persona: Persona,
    self_facts_chunks: Iterable[PersonaChunk],
) -> AutonomyLevel:
    """Resolve the runtime-effective autonomy level for ``persona``.

    Effective autonomy = the persona's YAML default
    (:attr:`Persona.autonomy`) overlaid by the current (non-superseded) head of
    the ``persona_self`` learning chain under ``logical_id="autonomy"`` in the
    self_facts store, if one exists (D-21-8 / D-21-11). The persona model stays
    frozen; this is a pure read over already-fetched chunks — no store access,
    no mutation (CQS).

    Args:
        persona: The loaded persona; supplies the author-time default.
        self_facts_chunks: The persona's self_facts chunks (the caller fetches
            them, typically via ``SelfFactsStore.get_all`` including superseded
            so the chain is complete; :func:`current_version` selects the head).

    Returns:
        The resolved autonomy level.

    Raises:
        InvalidAutonomyLevelError: The learning chain's head stores a value
            outside the three supported levels (corrupted / hand-edited chain).
            Fail-loud rather than silently fall back to the YAML default.
    """
    head = current_version(self_facts_chunks, AUTONOMY_LOGICAL_ID)
    if head is None:
        return persona.autonomy
    raw = head.metadata.get(AUTONOMY_METADATA_KEY)
    if raw is None or raw not in AUTONOMY_LEVELS:
        raise InvalidAutonomyLevelError(
            "autonomy chain head stores an invalid level",
            context={
                "value": str(raw),
                "logical_id": AUTONOMY_LOGICAL_ID,
                "persona_id": str(persona.persona_id),
                "chunk_id": head.id,
            },
        )
    return cast("AutonomyLevel", raw)


def record_autonomy_update(
    store: MemoryStore,
    persona_id: str,
    new_level: AutonomyLevel,
    *,
    now: datetime,
    written_by: str,
    reason: str,
    confidence: float = 1.0,
    session_id: str | None = None,
) -> None:
    """Persist a ``persona_self`` autonomy update (spec 21 T03, D-21-4/8/11).

    Appends a new version to the autonomy learning chain
    (``logical_id="autonomy"``) in the self_facts store via the force-write
    path. The store enforces the persona_self policy (``force=True`` +
    ``confidence >= 0.8`` + non-empty reason, architecture §4.3) and emits the
    single ``AuditEvent`` for the mutation — this function does not audit
    separately (CQS write; one event per mutation).

    Before writing, the **cooldown** (D-21-4) is enforced statelessly from the
    chain itself: an update is rejected if the current head was written on the
    same UTC day *or* within the same session as ``now``/``session_id``. A
    rejected update raises and writes nothing (rejections do not audit).

    The persona model stays frozen (D-21-11) — the effective level is recovered
    at read time by :func:`resolve_autonomy`, never by mutating ``Persona``.

    Args:
        store: The self_facts store (any :class:`MemoryStore`).
        persona_id: The persona whose autonomy is being revised.
        new_level: The new autonomy level to record.
        now: The current tz-aware UTC time (injected for testability and to
            keep the function pure of clock access).
        written_by: Provenance ``written_by`` — the model+tier id making the
            self-revision (e.g., ``"frontier:claude-sonnet-4-6"``).
        reason: Non-empty rationale (the store rejects an empty reason).
        confidence: Confidence in ``[0, 1]``; the store rejects ``< 0.8``.
        session_id: The current session/conversation id. When provided it is
            recorded on the chunk and consulted for the per-session cooldown;
            when ``None`` only the per-day cooldown applies.

    Raises:
        InvalidAutonomyLevelError: ``new_level`` is not a supported level.
        AutonomyCooldownError: An update was already recorded today or in this
            session (D-21-4).
        PersonaSelfWriteForbiddenError: The store rejected the write (e.g.,
            confidence below threshold or empty reason).
    """
    if new_level not in AUTONOMY_LEVELS:
        raise InvalidAutonomyLevelError(
            "cannot record an invalid autonomy level",
            context={
                "value": str(new_level),
                "persona_id": persona_id,
                "supported": ",".join(sorted(AUTONOMY_LEVELS)),
            },
        )

    # The cooldown is a decision made from the chain, so a store level retry would walk past it:
    # the loser of a race would land a second update on a day that allows one (D-21-4). So this
    # takes the conflict itself and goes back through the gate, where it normally stops, because
    # what beat it is now the head. A second conflict means the chain is genuinely busy, and
    # that reaches the caller.
    try:
        _gate_and_append(
            store, persona_id, new_level, now, written_by, reason, confidence, session_id
        )
    except ChainConflictError:
        _gate_and_append(
            store, persona_id, new_level, now, written_by, reason, confidence, session_id
        )


def _gate_and_append(
    store: MemoryStore,
    persona_id: str,
    new_level: AutonomyLevel,
    now: datetime,
    written_by: str,
    reason: str,
    confidence: float,
    session_id: str | None,
) -> None:
    """Read the chain, enforce the cooldown against its head, and append the new version."""
    chain = store.history(persona_id, AUTONOMY_LOGICAL_ID)
    if chain:
        head = chain[-1]
        _enforce_cooldown(head, persona_id=persona_id, now=now, session_id=session_id)

    metadata = {
        AUTONOMY_METADATA_KEY: new_level,
        "confidence": f"{confidence}",
    }
    if session_id is not None:
        metadata[AUTONOMY_SESSION_METADATA_KEY] = session_id

    chunk = PersonaChunk(
        # Minted, never numbered from the chain: two writers that read the same chain would
        # build the same id and the second would overwrite the first in place, which nothing
        # can see (R9-205).
        id=mint_chunk_id(persona_id, SelfFactsStore.STORE_KIND),
        text=f"Autonomy preference set to {new_level}.",
        metadata=metadata,
        created_at=now,
        # provenance.logical_id pins the chunk to the autonomy chain even on the
        # first write (the store overwrites the other provenance fields).
        provenance=ChunkProvenance(
            source=WriteSource.PERSONA_SELF,
            logical_id=AUTONOMY_LOGICAL_ID,
            written_at=now,
            written_by=written_by,
            reason=reason,
        ),
    )
    store.write(
        persona_id,
        [chunk],
        source=WriteSource.PERSONA_SELF,
        written_by=written_by,
        reason=reason,
        force=True,
        retry_on_conflict=False,
    )


def _enforce_cooldown(
    head: PersonaChunk,
    *,
    persona_id: str,
    now: datetime,
    session_id: str | None,
) -> None:
    """Raise :class:`AutonomyCooldownError` if ``head`` is within cooldown of ``now``.

    Day window is checked first (the dominant constraint); the session window
    catches the rare cross-midnight same-session case the day check misses.
    """
    if head.provenance is None:  # pragma: no cover - chain heads always carry provenance
        return
    written_at = head.provenance.written_at
    if written_at.date() == now.date():
        raise AutonomyCooldownError(
            "autonomy already updated today",
            context={
                "persona_id": persona_id,
                "window": "day",
                "head_written_at": written_at.isoformat(),
            },
        )
    if session_id is not None and head.metadata.get(AUTONOMY_SESSION_METADATA_KEY) == session_id:
        raise AutonomyCooldownError(
            "autonomy already updated in this session",
            context={
                "persona_id": persona_id,
                "window": "session",
                "session_id": session_id,
                "head_written_at": written_at.isoformat(),
            },
        )
