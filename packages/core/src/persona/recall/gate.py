"""Confidence/abstention gate, composed with the K4 safety gate (Spec K9, T6; K9-D-9).

At year-scale, confabulating a memory is worse than admitting absence: below-threshold
recall must yield an honest "I don't have that," not a fabricated fact. The gate is
**deterministic — no turn-path LLM** (K9-D-9): abstain unless the top reranked relevance
clears ``abstain_floor`` **and** the set is unambiguous — a clear standout (``margin``) or,
for a narrative turn, enough diffuse evidence (``mass``). Point-fact turns require the
standout (many mediocre matches = ambiguity, not an answer); the question-type signal is the
**shared DrillStop classifier** (:func:`~persona.recall.contiguity.classify_question_type`),
consumed here, never re-defined (K9-D-7).

**The load-bearing K4 composition (K9-D-9).** Safety and abstention are orthogonal — K4
answers *"may this node surface?"* (a per-node subtraction), abstention answers *"is the
recalled set strong enough to say anything?"* (a set-level predicate) — but they must render
as **one honest seam**, never double-gate. The ordering is decisive:

1. **K4 safety subtracts FIRST** (the wellbeing allowlist, per-node, graph candidates only).
2. **Abstention evaluates the POST-POLICY remainder** — confidence is computed on *exactly*
   the set that could surface, so it never "lies about a set that got stripped."
3. **One rendered seam, two internal reason codes** — ``LOW_CONFIDENCE`` vs
   ``WITHHELD_BY_POLICY`` are kept apart for logging/telemetry, but the surface is a single
   honest absence; the code that fired is **never leaked** (revealing "withheld for safety"
   would itself disclose that a sensitive node exists). The reason is attributed by asking
   whether the pre-policy set *would* have passed — using it only to label the decision,
   never to surface a subtracted node.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from persona.recall.contiguity import QuestionType
from persona.recall.models import RecallCandidate, RecallSource

if TYPE_CHECKING:
    from collections.abc import Sequence

    from persona.recall.config import RecallSettings

__all__ = ["AbstentionOutcome", "AbstentionReason", "compose_gate", "passes_confidence"]


class AbstentionReason(StrEnum):
    """Why the gate decided as it did — INTERNAL (logging/telemetry), never surfaced.

    Values:
        SURFACED: The post-policy set cleared the confidence gate — memory surfaces.
        LOW_CONFIDENCE: Abstained — recall was too weak/ambiguous (nothing worth surfacing).
        WITHHELD_BY_POLICY: Abstained — the K4 subtraction is the but-for cause (the recall
            *would* have surfaced but the surviving set no longer clears the gate). Kept
            distinct for telemetry; the user still sees one honest absence, never this reason.
    """

    SURFACED = "surfaced"
    LOW_CONFIDENCE = "abstain_low_confidence"
    WITHHELD_BY_POLICY = "withheld_by_policy"


@dataclass(frozen=True)
class AbstentionOutcome:
    """The gate's decision — the surfaced set (post-policy) + the internal reason.

    Attributes:
        surfaced: The candidates that may reach the prompt (empty when abstaining). Always
            the **post-policy** set — a K4-subtracted node is never here.
        abstained: ``True`` when the honest-absence seam should render (no memory surfaces).
        reason: The internal reason code (never surfaced to the user).
    """

    surfaced: tuple[RecallCandidate, ...]
    abstained: bool
    reason: AbstentionReason


def _relevances(candidates: Sequence[RecallCandidate]) -> list[float]:
    """The relevance readings, best-first; a missing reading counts as 0 (sparse/contiguity)."""
    return sorted(
        (c.relevance if c.relevance is not None else 0.0 for c in candidates), reverse=True
    )


def passes_confidence(
    candidates: Sequence[RecallCandidate],
    *,
    settings: RecallSettings,
    question_type: QuestionType,
) -> bool:
    """The deterministic confidence predicate (K9-D-9) — does anything surface?

    Passes iff the top relevance clears ``abstain_floor`` AND the set is unambiguous: a
    standout (``top1 − top2 ≥ abstain_margin_floor``) or — for a NARRATIVE turn only —
    enough diffuse mass (sum of the top ``abstain_mass_count`` readings ≥ ``abstain_mass_floor``).
    A POINT_FACT turn requires the standout: many mediocre matches mean ambiguity, not an answer.
    An empty set never passes.
    """
    rels = _relevances(candidates)
    if not rels or rels[0] < settings.abstain_floor:
        return False
    margin = rels[0] - (rels[1] if len(rels) > 1 else 0.0)
    if margin >= settings.abstain_margin_floor:
        return True
    if question_type is QuestionType.NARRATIVE:
        mass = sum(rels[: settings.abstain_mass_count])
        return mass >= settings.abstain_mass_floor
    return False


def _subtract_policy(
    candidates: Sequence[RecallCandidate], allowlist: set[str] | None
) -> list[RecallCandidate]:
    """K4 safety subtraction — graph candidates outside the allowlist are removed (K9-D-9).

    Applies to GRAPH candidates only (K4's allowlist is the owner's graph-node set minus the
    gated wellbeing nodes — K4-D-2); episodic chunks are not graph-gated and always pass.
    ``None`` allowlist = no subtraction (the common, un-gated turn).
    """
    if allowlist is None:
        return list(candidates)
    return [c for c in candidates if c.source is not RecallSource.GRAPH or c.key in allowlist]


def compose_gate(
    candidates: Sequence[RecallCandidate],
    *,
    settings: RecallSettings,
    question_type: QuestionType,
    allowlist: set[str] | None = None,
    result_budget: int | None = None,
) -> AbstentionOutcome:
    """Compose K4 safety-subtraction then abstention over the remainder (K9-D-9 — the seam).

    The load-bearing ordering: **subtract first, evaluate the remainder**. Confidence is
    measured on the post-policy set (never on nodes that get stripped); the surfaced set is
    always post-policy. When abstaining, the reason distinguishes a policy-caused absence
    from weak recall — for telemetry only; the user always sees one honest absence.

    Args:
        candidates: The scored + diffused pool (best-first).
        settings: The recall tunables (abstention thresholds + result budget).
        question_type: The shared DrillStop classifier's verdict (mass allowed only for
            NARRATIVE).
        allowlist: The K4 permitted graph-node-id set (``owner_nodes − gated``); ``None`` ⇒
            no subtraction.
        result_budget: Max surfaced candidates; ``None`` ⇒ ``settings.result_budget``.

    Returns:
        The :class:`AbstentionOutcome` — post-policy surfaced set (or empty) + internal reason.
    """
    budget = settings.result_budget if result_budget is None else result_budget
    post = _subtract_policy(candidates, allowlist)
    if passes_confidence(post, settings=settings, question_type=question_type):
        return AbstentionOutcome(
            surfaced=tuple(post[:budget]), abstained=False, reason=AbstentionReason.SURFACED
        )
    # Abstaining. Attribute the reason WITHOUT surfacing any subtracted node: if the
    # pre-policy set WOULD have passed and policy removed candidates, policy is the cause.
    policy_removed = len(candidates) - len(post)
    pre_would_pass = policy_removed > 0 and passes_confidence(
        candidates, settings=settings, question_type=question_type
    )
    reason = (
        AbstentionReason.WITHHELD_BY_POLICY if pre_would_pass else AbstentionReason.LOW_CONFIDENCE
    )
    return AbstentionOutcome(surfaced=(), abstained=True, reason=reason)
