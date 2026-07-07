"""The T4–T6 eval slice — the composed path against the acceptance criteria (Spec K9).

A deterministic, local eval that drives the full ``compose_recall`` path (fuse → rerank →
score → contiguity → diffusion → gate) and asserts the spec's acceptance shape:

- acceptance-2 (rerank lift): a reranker that knows the answer lifts it above the fused order;
- acceptance-4 (abstention, both directions): genuinely-relevant memory surfaces (no
  over-abstention); a query with none yields honest absence (no under-abstention, nothing
  fabricated);
- acceptance-5 (contiguity): an episodic hit expands its temporal neighbours, reinforced;
- acceptance-6 (diffusion discipline): a single-hop turn does not invoke diffusion;
- K9-D-12 end-to-end: the whole path runs with a STUB reranker — degraded ordering, never a
  crash. The full P8-style corpus eval is T10; this slice pins the mechanisms.
"""

from __future__ import annotations

from persona.recall.config import RecallSettings
from persona.recall.contiguity import Episode
from persona.recall.models import RecallCandidate, RecallLeg
from persona.recall.pipeline import RecallResult, compose_recall
from persona.recall.rerank import IdentityReranker, build_reranker
from persona.stores.lifecycle import EpisodicSettings

from tests.unit.recall._fixtures import NOW, chunk, node

SETTINGS = RecallSettings()
EPISODIC = EpisodicSettings()


class _KeywordScorer:
    """A scorer that ranks candidates containing a keyword first (a stand-in for P7)."""

    def __init__(self, keyword: str) -> None:
        self._keyword = keyword

    def score(self, query: str, texts: list[str]) -> list[float]:  # noqa: ARG002
        return [1.0 if self._keyword in t else 0.0 for t in texts]


class _FakeEpisodes:
    def __init__(self, mapping: dict[str, Episode]) -> None:
        self._mapping = mapping

    def episode(self, candidate: RecallCandidate) -> Episode | None:
        return self._mapping.get(candidate.key)


def _legs(chunks: list, nodes: list) -> dict:  # type: ignore[type-arg]
    legs: dict = {}
    if chunks:
        legs[RecallLeg.EPISODIC_RAW] = chunks
    if nodes:
        legs[RecallLeg.GRAPH_DENSE] = nodes
    return legs


def _compose(**kw: object) -> RecallResult:
    base: dict[str, object] = {
        "settings": SETTINGS,
        "episodic_settings": EPISODIC,
        "now": NOW,
        "persona_id": "p",
        "owner_id": "o",
    }
    base.update(kw)
    return compose_recall(**base)  # type: ignore[arg-type]


# ---- acceptance-2: rerank lift --------------------------------------------


def test_reranker_lifts_the_relevant_answer_above_the_fused_order() -> None:
    # Fused order puts the distractor first (better cosine); the reranker knows "dentist".
    legs = _legs(
        [
            chunk("distractor", distance=0.05, text="the weather was nice"),
            chunk("answer", distance=0.30, text="my dentist is Dr Ade"),
        ],
        [],
    )
    stub = _compose(legs=legs, query="dentist", reranker=IdentityReranker())
    reranked = _compose(
        legs=legs,
        query="dentist",
        reranker=build_reranker(
            scorer=_KeywordScorer("dentist"), settings=RecallSettings(rerank_enabled=True)
        ),
    )
    assert stub.surfaced[0].key == "distractor"  # un-reranked: cosine wins
    assert reranked.surfaced[0].key == "answer"  # reranked: the answer is lifted (the lift)


# ---- acceptance-4: abstention, both directions ----------------------------


def test_relevant_memory_surfaces_no_over_abstention() -> None:
    legs = _legs([chunk("hit", distance=0.05, text="my address is 5 Oak St")], [])
    result = _compose(legs=legs, query="what is my address", reranker=IdentityReranker())
    assert not result.abstained
    assert result.surfaced[0].key == "hit"


def test_no_relevant_memory_yields_honest_absence_no_fabrication() -> None:
    legs = _legs([chunk("weak", distance=0.95, text="unrelated chatter")], [])
    result = _compose(legs=legs, query="what is my passport number", reranker=IdentityReranker())
    assert result.abstained  # honest absence
    assert result.surfaced == ()  # nothing fabricated reaches the prompt


def test_empty_recall_abstains() -> None:
    result = _compose(legs={}, query="anything", reranker=IdentityReranker())
    assert result.abstained
    assert result.surfaced == ()


# ---- acceptance-5: contiguity ---------------------------------------------


def test_a_narrative_hit_expands_and_reinforces_its_neighbours() -> None:
    members = (chunk("m0"), chunk("hit", distance=0.05, text="the day we moved"), chunk("m2"))
    provider = _FakeEpisodes({"hit": Episode(members=members, seed_index=1)})
    legs = _legs([chunk("hit", distance=0.05, text="the day we moved")], [])
    result = _compose(
        legs=legs,
        query="what happened that day",
        reranker=IdentityReranker(),
        episode_provider=provider,
    )
    surfaced_keys = {c.key for c in result.surfaced}
    assert "hit" in surfaced_keys
    assert {"m0", "m2"} & surfaced_keys  # neighbours pulled in
    assert set(result.reinforce_ids) >= {"hit"}  # the hit + used members reinforce


# ---- acceptance-6: diffusion discipline -----------------------------------


class _CountingNeighbours:
    def __init__(self) -> None:
        self.calls = 0

    def neighbours(self, node_id: str) -> list[str]:  # noqa: ARG002
        self.calls += 1
        return []


def test_a_strong_single_hop_turn_does_not_invoke_diffusion() -> None:
    legs = _legs([], [node("g1", distance=0.02, name="Address", content="5 Oak St")])
    provider = _CountingNeighbours()
    _compose(
        legs=legs,
        query="my address",  # single entity, high confidence → gate OFF
        reranker=IdentityReranker(),
        neighbour_provider=provider,
    )
    assert provider.calls == 0  # diffusion default-off on single-hop (structural discipline)


# ---- K9-D-12 end-to-end: the full path runs stub-safe ---------------------


def test_full_path_runs_with_a_stub_reranker_and_all_providers() -> None:
    members = (chunk("m0"), chunk("hit", distance=0.05), chunk("m2"))
    result = _compose(
        legs=_legs(
            [chunk("hit", distance=0.05, text="what happened")],
            [node("g1", distance=0.1)],
        ),
        query="what happened that day with Alice and Bob",  # narrative + multi-entity
        reranker=IdentityReranker(),
        episode_provider=_FakeEpisodes({"hit": Episode(members=members, seed_index=1)}),
        neighbour_provider=_CountingNeighbours(),
    )
    # Degraded ordering under the stub, but a real result — never a crash, no P7 dependency.
    assert isinstance(result.surfaced, tuple)
    assert result.question_type is not None
