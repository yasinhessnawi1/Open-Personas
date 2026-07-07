"""K9 flip-ratification eval slice (Spec K9, T10) — the local P8-style measurement.

Not just pass/fail assertions: a small labelled eval set over the composed ``compose_recall``
path that MEASURES the load-bearing claims the flip rests on, and reports the numbers:

1. **Rerank lift** (acceptance-2): mean recall@1 with a reranker that knows the answer ≥ the
   un-reranked fused baseline — including the SYNAPSE-style low-similarity case (the true answer
   has a weak cosine but is recovered).
2. **Abstention, both directions + both reason codes** (acceptance-4 / K9-D-9): relevant memory
   surfaces; genuinely-absent memory ⇒ honest absence; a point-fact near-tie ⇒ abstain; the
   ``WITHHELD_BY_POLICY`` vs ``LOW_CONFIDENCE`` reason codes are both exercised internally and
   NEVER surfaced.
3. **Fail-soft mid-eval** (acceptance-3): a reranker that dies degrades the whole path to the
   fused order — never a crash, never a stall.

The full P8 corpus (LongMemEval / LoCoMo / the 1M-turn eval) is P8's; this slice pins the
mechanisms deterministically so the flip decision has measured evidence.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.recall.config import RecallSettings
from persona.recall.contiguity import QuestionType
from persona.recall.gate import AbstentionReason, compose_gate
from persona.recall.models import RecallCandidate, RecallLeg, RecallSource
from persona.recall.pipeline import compose_recall
from persona.recall.rerank import IdentityReranker, build_reranker
from persona.stores.lifecycle import EpisodicSettings

from tests.unit.recall._fixtures import NOW, chunk, node

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.graph.models import ConceptNode
    from persona.recall.rerank import Reranker

SETTINGS = RecallSettings()
EPISODIC = EpisodicSettings()


class _KeywordScorer:
    """A stand-in for P7: scores 1.0 for candidates whose text holds the keyword, else 0.1."""

    def __init__(self, keyword: str) -> None:
        self._kw = keyword

    def score(self, query: str, texts: list[str]) -> list[float]:  # noqa: ARG002
        return [1.0 if self._kw in t else 0.1 for t in texts]


#: The labelled eval set: each query has one relevant chunk whose COSINE is worse than a
#: distractor's (so the fused/cosine baseline ranks the distractor first), but whose text
#: carries the keyword a good reranker keys on. The last case is the SYNAPSE low-similarity
#: case: the answer's cosine is genuinely weak (distance 0.62) yet the reranker recovers it.
_EVAL = [
    (
        "dentist",
        "answer",
        [
            chunk("distractor", distance=0.05, text="the weather was pleasant"),
            chunk("answer", distance=0.35, text="my dentist is Dr Ade in Oslo"),
        ],
    ),
    (
        "passport",
        "answer",
        [
            chunk("d1", distance=0.08, text="we talked about lunch"),
            chunk("answer", distance=0.30, text="my passport expires in March"),
        ],
    ),
    (
        "allergy",
        "answer",
        [
            chunk("d2", distance=0.10, text="the movie was long"),
            chunk("answer", distance=0.40, text="I have a penicillin allergy"),
        ],
    ),
    # SYNAPSE low-similarity: the answer's cosine is weak; a similarity-gated baseline would
    # rank it low, the reranker recovers it (fuse+rerank degrades gracefully — acceptance-2).
    (
        "mortgage",
        "answer",
        [
            chunk("d3", distance=0.06, text="nice chat about coffee"),
            chunk("answer", distance=0.62, text="my mortgage renews next year"),
        ],
    ),
]


def _recall_at_1(reranker_factory: Callable[[str], Reranker]) -> float:
    hits = 0
    for query, relevant, corpus in _EVAL:
        result = compose_recall(
            legs={RecallLeg.EPISODIC_RAW: corpus},
            query=query,
            reranker=reranker_factory(query),
            settings=SETTINGS,
            episodic_settings=EPISODIC,
            now=NOW,
            persona_id="p",
        )
        if result.surfaced and result.surfaced[0].key == relevant:
            hits += 1
    return hits / len(_EVAL)


def test_rerank_lift_measured_over_the_eval_set() -> None:
    fused = _recall_at_1(lambda _q: IdentityReranker())  # the un-reranked baseline
    reranked = _recall_at_1(
        lambda q: build_reranker(
            scorer=_KeywordScorer(q), settings=RecallSettings(rerank_enabled=True)
        )
    )
    # Reported for the flip package: the baseline ranks distractors first (cosine), the reranker
    # lifts the true answer — reranked recall@1 ≥ fused, and strictly higher here.
    assert reranked >= fused
    assert reranked == 1.0  # the reranker recovers every case, incl. the low-similarity one
    assert fused < 1.0  # the fused/cosine baseline misses (distractors win on similarity)


def test_relevant_memory_surfaces_no_over_abstention() -> None:
    result = compose_recall(
        legs={RecallLeg.EPISODIC_RAW: [chunk("hit", distance=0.03, text="my address is 5 Oak")]},
        query="what is my address",
        reranker=IdentityReranker(),
        settings=SETTINGS,
        episodic_settings=EPISODIC,
        now=NOW,
        persona_id="p",
    )
    assert not result.abstained
    assert result.surfaced[0].key == "hit"


def test_no_relevant_memory_abstains_low_confidence_nothing_leaked() -> None:
    result = compose_recall(
        legs={RecallLeg.EPISODIC_RAW: [chunk("weak", distance=0.97, text="unrelated")]},
        query="what is my passport number",
        reranker=IdentityReranker(),
        settings=SETTINGS,
        episodic_settings=EPISODIC,
        now=NOW,
        persona_id="p",
    )
    assert result.abstained
    assert result.surfaced == ()  # nothing fabricated reaches the prompt
    assert result.reason is AbstentionReason.LOW_CONFIDENCE


def test_point_fact_near_tie_abstains_ambiguity_not_an_answer() -> None:
    # Two near-equal candidates for a point-fact = ambiguity → abstain (the gate working).
    result = compose_recall(
        legs={
            RecallLeg.EPISODIC_RAW: [
                chunk("a", distance=0.20, text="my bank is Nordea"),
                chunk("b", distance=0.22, text="my bank is DNB"),
            ]
        },
        query="what is my bank",
        reranker=IdentityReranker(),
        settings=SETTINGS,
        episodic_settings=EPISODIC,
        now=NOW,
        persona_id="p",
    )
    assert result.abstained
    assert result.reason is AbstentionReason.LOW_CONFIDENCE


def test_both_reason_codes_exercised_and_neither_leaked() -> None:
    # WITHHELD_BY_POLICY: a strong graph hit is K4-subtracted, the survivor is weak → abstain
    # *because of policy*; the subtracted node never surfaces, and the reason is internal only.
    strong = node("g_sensitive", distance=0.02)
    weak = node("g_weak", distance=0.9)
    withheld = compose_gate(
        [
            _as_candidate(strong, rank=1),
            _as_candidate(weak, rank=2),
        ],
        settings=SETTINGS,
        question_type=QuestionType.POINT_FACT,
        allowlist={"g_weak"},  # g_sensitive is gated out
    )
    assert withheld.abstained
    assert withheld.surfaced == ()  # the subtracted node is NEVER surfaced
    assert withheld.reason is AbstentionReason.WITHHELD_BY_POLICY

    low = compose_gate(
        [_as_candidate(weak, rank=1)],
        settings=SETTINGS,
        question_type=QuestionType.POINT_FACT,
    )
    assert low.reason is AbstentionReason.LOW_CONFIDENCE
    # The two reason codes are distinct internal telemetry; the surfaced set is identical
    # (empty) either way — the user sees ONE honest absence, the reason is never in the output.
    assert withheld.surfaced == low.surfaced == ()


def test_failsoft_mid_eval_reranker_dies_degrades_to_fused() -> None:
    from persona.recall.rerank import FailSoftReranker

    class _DyingReranker:
        def rerank(self, query: str, candidates: list, *, top_k: int) -> list:  # noqa: ARG002
            raise RuntimeError("reranker crashed mid-eval")

    corpus = [chunk("strong", distance=0.03, text="the answer"), chunk("weak", distance=0.6)]
    result = compose_recall(
        legs={RecallLeg.EPISODIC_RAW: corpus},
        query="the answer",
        # The composition ALWAYS wraps the reranker in the fail-soft shell (T3); a dying inner
        # degrades to the fused order.
        reranker=FailSoftReranker(_DyingReranker()),
        settings=SETTINGS,
        episodic_settings=EPISODIC,
        now=NOW,
        persona_id="p",
    )
    # The turn survives on the fused order — never a crash, never a stall.
    assert not result.abstained
    assert result.surfaced[0].key == "strong"


def _as_candidate(n: ConceptNode, *, rank: int) -> RecallCandidate:
    return RecallCandidate(
        source=RecallSource.GRAPH,
        key=n.id,
        node=n,
        relevance=None if n.distance is None else 1.0 - n.distance,
        rank=rank,
    )
