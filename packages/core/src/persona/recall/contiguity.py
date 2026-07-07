"""Temporal contiguity + the frozen K8↔K9 DrillStop contract (Spec K9, T4; K9-D-6/D-7).

An episodic hit pulls its **time-neighbours** — the cognitive temporal-contiguity effect
(TCM/CMR; EM-LLM 2407.09450, best-in-44%-of-tasks). Contiguity gets its **own reserved
budget** ``k_c ≤ k_s`` (EM-LLM's measured partition), **seeded from the top hit(s) only**,
walking the gist's ``member_ids`` in original order with a slight **forward asymmetry**
(next favoured over previous — the human effect most systems omit).

**The DrillStop contract (K9-D-7).** When expansion stops is a **single-sourced K8↔K9
contract** — written once here, consumed by both K9's contiguity (this module) and K9's
abstention gate (T6, via :func:`classify_question_type`), forked by neither (the A6
freeze discipline; see ``docs/specs/phase3/spec_K9/drill_satisfied_contract.md``). K8 owns
the drill *pointers* (``member_ids``); K9 owns this *stop policy*. Stop on the OR of four
deterministic guards: (1) the ``k_c`` budget (always-on); (2) similarity drop-off — a
neighbour with a relevance reading below ``β · seed`` stops the walk (fires only when a
reading exists; pure temporal neighbours have none and are bounded by 1/3/4); (3) the
**gist boundary** — expansion never crosses the owning gist's members (the pyramid's gist
boundary *is* the EM-LLM event boundary — enforced structurally by iterating only within
the member list); (4) the **question-type gate** — a point-fact lookup does not expand
(``k_c → 0``); a narrative query expands to budget. All guards are pure, no LLM call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

from persona.recall.models import RecallCandidate, RecallSource

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.recall.config import RecallSettings
    from persona.schema.chunks import PersonaChunk

__all__ = [
    "DRILL_CUE_VERSION",
    "DrillStop",
    "Episode",
    "EpisodeProvider",
    "QuestionType",
    "classify_question_type",
    "expand_contiguity",
]

#: Version of the question-type cue lexicon (bump on any wording change; the eval re-runs).
DRILL_CUE_VERSION = "v1"

#: Narrative/temporal-deixis cues → expand contiguity. Precision-biased: a query is
#: POINT_FACT unless it clearly invites the surrounding episode. EN-primary; a couple of
#: high-signal NO cues included, the rest a documented versioned extension (P8 calibrates).
_NARRATIVE_CUES: tuple[str, ...] = (
    r"\bwhat happened\b",
    r"\btell me about\b",
    r"\bthat (day|time|week|night|morning|evening|conversation)\b",
    r"\bback then\b",
    r"\baround (then|that time)\b",
    r"\bwhen we\b",
    r"\bthe last time\b",
    r"\bafter that\b",
    r"\bbefore that\b",
    r"\bwhat (were|was) we\b",
    r"\bden gangen\b",  # NO: "that time"
    r"\bhva skjedde\b",  # NO: "what happened"
)
_NARRATIVE_RE = re.compile("|".join(_NARRATIVE_CUES), re.IGNORECASE)


class QuestionType(StrEnum):
    """The turn's coarse shape — the DrillStop guard-4 signal (K9-D-7), shared with T6.

    Values:
        NARRATIVE: Invites the surrounding episode ("what happened around then") —
            contiguity expands; the abstention gate (T6) accepts diffuse evidence (mass).
        POINT_FACT: Asks for a specific fact ("my dentist's name") — contiguity does not
            expand (neighbours add noise); the abstention gate requires a standout hit
            (margin), because many mediocre matches mean ambiguity, not an answer.
    """

    NARRATIVE = "narrative"
    POINT_FACT = "point_fact"


def classify_question_type(query: str) -> QuestionType:
    """Classify the turn deterministically from surface cues (no LLM — K9-D-7/D-8).

    Precision-biased toward :attr:`QuestionType.POINT_FACT`: only an explicit
    narrative/temporal-deixis cue returns :attr:`QuestionType.NARRATIVE`. Shared by the
    contiguity budget (T4) and the abstention gate (T6) — the one classifier, never forked.
    """
    return QuestionType.NARRATIVE if _NARRATIVE_RE.search(query) else QuestionType.POINT_FACT


@dataclass(frozen=True)
class Episode:
    """A hit's surrounding episode — the ordered gist members + the seed's position.

    Attributes:
        members: The gist's raw member chunks, in original (temporal) order — the
            drill pointers K8 wrote. This tuple IS the gist boundary (guard 3): expansion
            never looks outside it.
        seed_index: The seed chunk's index in ``members``; ``None`` when the seed is a gist
            row itself (the whole episode is its neighbourhood — walk from the start).
    """

    members: tuple[PersonaChunk, ...]
    seed_index: int | None


class EpisodeProvider(Protocol):
    """Resolve a hit's surrounding episode from the pyramid (composition-bound, T8/T9).

    The seam over K8's ``pyramid`` (``covering_gists`` + ``drill``): given a candidate,
    return its :class:`Episode` or ``None`` (no covering gist yet — fail-soft, no
    expansion). Injected so contiguity is pure and testable; the composition binds the
    real pyramid.
    """

    def episode(self, candidate: RecallCandidate) -> Episode | None:
        """Return the candidate's surrounding episode, or ``None`` if it has none."""
        ...


@dataclass(frozen=True)
class DrillStop:
    """The frozen stop-condition evaluator (K9-D-7) — guards 1 & 2; 3 & 4 are structural.

    Guard 3 (gist boundary) is enforced by :func:`expand_contiguity` iterating only within
    the :class:`Episode` members; guard 4 (question-type) is applied up front (a point-fact
    turn builds a ``DrillStop`` with ``budget=0``). This object carries the always-on budget
    guard and the similarity-drop-off guard.

    Attributes:
        budget: ``k_c`` — the reserved contiguity count (0 disables expansion, e.g. point-fact).
        beta: The similarity-drop-off fraction; a neighbour whose relevance reading is below
            ``beta · seed_relevance`` stops the walk. Fires only when a reading exists.
    """

    budget: int
    beta: float

    def stopped(
        self, *, added: int, seed_relevance: float | None, neighbour_relevance: float | None
    ) -> bool:
        """Return ``True`` when expansion must stop — guard 1 (budget) or 2 (drop-off)."""
        return added >= self.budget or (
            # guard 2 — similarity drop-off, only when a reading exists
            self.beta > 0.0
            and seed_relevance is not None
            and neighbour_relevance is not None
            and neighbour_relevance < self.beta * seed_relevance
        )


def _neighbour_order(members_len: int, seed_index: int | None) -> list[int]:
    """Indices to visit, forward-biased (K9-D-6): a gist seed walks from the start; a chunk
    seed walks outward next-before-previous at each distance (the TCM forward asymmetry)."""
    if seed_index is None:
        return list(range(members_len))
    order: list[int] = []
    distance = 1
    while len(order) < members_len:
        nxt, prev = seed_index + distance, seed_index - distance
        if nxt < members_len:
            order.append(nxt)
        if prev >= 0:
            order.append(prev)
        if nxt >= members_len and prev < 0:
            break
        distance += 1
    return order


def expand_contiguity(
    scored: Sequence[RecallCandidate],
    *,
    provider: EpisodeProvider,
    settings: RecallSettings,
    question_type: QuestionType,
    persona_id: str | None = None,
) -> tuple[list[RecallCandidate], list[str]]:
    """Pull the temporal neighbours of the top hit(s) into a reserved budget (K9-D-6/D-7).

    For a NARRATIVE turn, seed from the top ``contiguity_seed_count`` episodic hits and walk
    each seed's gist members (forward-biased) until :class:`DrillStop` fires or the shared
    ``k_c`` budget is spent — whichever first. Neighbours enter as ``via_contiguity=True``
    episodic-raw candidates, deduped against the existing pool and across seeds. A POINT_FACT
    turn expands nothing (``k_c → 0`` — guard 4). Returns the new candidates **and** the raw
    member ids actually used, for the reinforcement-through-a-gist seam (handover §4.2 — K9
    reinforces the members it used, not just the FOUND hits).

    Args:
        scored: The composite-scored pool (the seeds are its top episodic hits).
        provider: The episode resolver over K8's pyramid (injected).
        settings: The recall tunables (``contiguity_budget`` = k_c, ``contiguity_seed_count``,
            ``contiguity_beta``).
        question_type: The DrillStop guard-4 signal (from :func:`classify_question_type`).
        persona_id: The episodic scope stamped on the new neighbour candidates.

    Returns:
        ``(new_candidates, used_member_ids)`` — the reserved-budget neighbours and the raw
        ids drilled (for reinforcement). Both empty when nothing expands.
    """
    budget = 0 if question_type is QuestionType.POINT_FACT else settings.contiguity_budget
    stop = DrillStop(budget=budget, beta=settings.contiguity_beta)
    if budget <= 0:
        return [], []

    present = {c.key for c in scored}
    new: list[RecallCandidate] = []
    used: list[str] = []
    seeds = [
        c for c in scored if c.source in (RecallSource.EPISODIC_RAW, RecallSource.EPISODIC_GIST)
    ]
    for seed in seeds[: settings.contiguity_seed_count]:
        episode = provider.episode(seed)
        if episode is None or not episode.members:
            continue
        for idx in _neighbour_order(len(episode.members), episode.seed_index):
            member = episode.members[idx]
            if member.id in present:
                continue
            neighbour_rel = None if member.distance is None else 1.0 - member.distance
            if stop.stopped(
                added=len(new), seed_relevance=seed.relevance, neighbour_relevance=neighbour_rel
            ):
                break
            present.add(member.id)
            used.append(member.id)
            new.append(
                RecallCandidate(
                    source=RecallSource.EPISODIC_RAW,
                    key=member.id,
                    chunk=member,
                    persona_id=persona_id,
                    relevance=neighbour_rel,
                    via_contiguity=True,
                )
            )
        if len(new) >= budget:
            break
    return new, used
