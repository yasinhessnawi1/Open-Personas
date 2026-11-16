"""Entity resolution orchestration (Spec K2, T3; D-K0-9 seam, criterion 3).

The runtime orchestrator drives K0's deterministic ``EntityRegistry.resolve`` and
runs the LLM judge (``EntityRecognizer``) ONLY on the AMBIGUOUS band — never
re-judging what K0 resolved cleanly. It turns raw entity mentions into resolved
canonical ``entity_ids``, collapsing aliases ("my doctor… Dr. Hansen… she…") to
ONE entity (criterion 3) rather than fragments. Tested with a fake registry +
fake (mocked-LLM) judge.
"""

# ruff: noqa: ARG002 — registry/judge test doubles deliberately ignore some args.

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.graph.models import CanonicalEntity, EntityAlias
from persona.graph.protocol import (
    EntityCandidate,
    EntityRegistry,
    ResolutionDecision,
    ResolutionVerdict,
)
from persona_runtime.extraction.resolution import EntityResolver


class _FakeRegistry:
    """A scriptable EntityRegistry double — resolve verdicts per surface, ids on create."""

    def __init__(self, verdicts: dict[str, ResolutionVerdict]) -> None:
        self._verdicts = verdicts
        self._counter = 0
        self.created: list[str] = []
        self.aliases: list[tuple[str, str]] = []  # (entity_id, surface)
        self._entities: dict[str, CanonicalEntity] = {}

    def resolve(self, owner_id: str, mention: str) -> ResolutionVerdict:
        return self._verdicts[mention]

    def get_entity(self, owner_id: str, entity_id: str) -> CanonicalEntity | None:
        return self._entities.get(entity_id)

    def create_entity(
        self,
        owner_id: str,
        *,
        canonical_name: str,
        aliases: tuple[EntityAlias, ...] = (),
        provenance: object = None,
    ) -> CanonicalEntity:
        self._counter += 1
        entity_id = f"entity-{self._counter}"
        self.created.append(canonical_name)
        entity = CanonicalEntity(
            id=entity_id,
            canonical_name=canonical_name,
            aliases=aliases,
            created_at=datetime.now(UTC),
        )
        self._entities[entity_id] = entity
        return entity

    def add_alias(self, owner_id: str, entity_id: str, alias: EntityAlias) -> None:
        self.aliases.append((entity_id, alias.surface))


class _FakeJudge:
    """A fake AMBIGUOUS-band judge: returns a configured chosen id per mention."""

    def __init__(self, decisions: dict[str, str | None]) -> None:
        self._decisions = decisions
        self.calls: list[str] = []

    async def judge_ambiguous(
        self, *, owner_id: str, mention: str, candidates: tuple[EntityCandidate, ...]
    ) -> str | None:
        self.calls.append(mention)
        return self._decisions.get(mention)


def _merge(canonical_id: str) -> ResolutionVerdict:
    return ResolutionVerdict(decision=ResolutionDecision.MERGE, canonical_id=canonical_id)


def _separate() -> ResolutionVerdict:
    return ResolutionVerdict(decision=ResolutionDecision.SEPARATE)


def _ambiguous(*candidate_ids: str) -> ResolutionVerdict:
    return ResolutionVerdict(
        decision=ResolutionDecision.AMBIGUOUS,
        candidates=tuple(
            EntityCandidate(entity_id=cid, canonical_name=cid, score=0.85) for cid in candidate_ids
        ),
    )


@pytest.mark.asyncio
async def test_merge_verdict_uses_canonical_id_no_judge_no_create() -> None:
    reg = _FakeRegistry({"the GP": _merge("entity-existing")})
    judge = _FakeJudge({})
    out = await EntityResolver(registry=reg, judge=judge).resolve_mentions("u1", ["the GP"])
    assert out == {"the GP": "entity-existing"}
    assert judge.calls == []  # K0 resolved cleanly — never re-judged (D-K0-9)
    assert reg.created == []


@pytest.mark.asyncio
async def test_separate_verdict_creates_a_new_entity() -> None:
    reg = _FakeRegistry({"Pixel the dog": _separate()})
    out = await EntityResolver(registry=reg, judge=_FakeJudge({})).resolve_mentions(
        "u1", ["Pixel the dog"]
    )
    assert out == {"Pixel the dog": "entity-1"}
    assert reg.created == ["Pixel the dog"]


@pytest.mark.asyncio
async def test_ambiguous_confirmed_by_judge_adds_alias_and_uses_that_id() -> None:
    reg = _FakeRegistry({"my doctor": _ambiguous("entity-7")})
    judge = _FakeJudge({"my doctor": "entity-7"})
    out = await EntityResolver(registry=reg, judge=judge).resolve_mentions("u1", ["my doctor"])
    assert out == {"my doctor": "entity-7"}
    assert reg.aliases == [("entity-7", "my doctor")]
    assert reg.created == []


@pytest.mark.asyncio
async def test_ambiguous_rejected_by_judge_creates_a_new_entity() -> None:
    reg = _FakeRegistry({"Sam": _ambiguous("entity-3")})
    judge = _FakeJudge({"Sam": None})  # judge says "not the same"
    out = await EntityResolver(registry=reg, judge=judge).resolve_mentions("u1", ["Sam"])
    assert out == {"Sam": "entity-1"}
    assert reg.created == ["Sam"]
    assert reg.aliases == []


@pytest.mark.asyncio
async def test_identical_surfaces_resolve_once() -> None:
    reg = _FakeRegistry({"mum": _separate()})
    out = await EntityResolver(registry=reg, judge=_FakeJudge({})).resolve_mentions(
        "u1", ["mum", "mum", "mum"]
    )
    assert out == {"mum": "entity-1"}
    assert reg.created == ["mum"]  # created exactly once


@pytest.mark.asyncio
async def test_provenance_is_passed_through_to_create_entity() -> None:
    captured: dict[str, object] = {}

    class _RecordingRegistry(_FakeRegistry):
        def create_entity(
            self, owner_id: str, *, canonical_name: str, **kwargs: object
        ) -> CanonicalEntity:  # type: ignore[override]
            captured["provenance"] = kwargs.get("provenance")
            return super().create_entity(owner_id, canonical_name=canonical_name)

    from persona.graph.models import NodeProvenance
    from persona.schema.chunks import WriteSource

    prov = NodeProvenance(source=WriteSource.SYSTEM, written_at=datetime.now(UTC))
    reg = _RecordingRegistry({"x": _separate()})
    await EntityResolver(registry=reg, judge=_FakeJudge({})).resolve_mentions(
        "u1", ["x"], provenance=prov
    )
    assert captured["provenance"] is prov


@pytest.mark.asyncio
async def test_criterion_3_alias_heavy_collapses_to_one_canonical_entity() -> None:
    # "my doctor… Dr. Hansen… she…" → ONE canonical entity, not three fragments.
    # First surface is new (SEPARATE → create entity-1); later variants reach the
    # registry's review band (AMBIGUOUS with entity-1 as a candidate) and the judge
    # confirms they are the same person → add_alias, same id. Never re-judged on a
    # clean verdict; never fragmented.
    reg = _FakeRegistry(
        {
            "my doctor": _separate(),
            "Dr. Hansen": _ambiguous("entity-1"),
            "she": _ambiguous("entity-1"),
        }
    )
    judge = _FakeJudge({"Dr. Hansen": "entity-1", "she": "entity-1"})
    out = await EntityResolver(registry=reg, judge=judge).resolve_mentions(
        "u1", ["my doctor", "Dr. Hansen", "she"]
    )
    # all three surfaces map to the SAME canonical entity
    assert set(out.values()) == {"entity-1"}
    assert out == {"my doctor": "entity-1", "Dr. Hansen": "entity-1", "she": "entity-1"}
    # exactly one entity created; the two variants recorded as aliases
    assert reg.created == ["my doctor"]
    assert reg.aliases == [("entity-1", "Dr. Hansen"), ("entity-1", "she")]


def test_fake_registry_satisfies_the_k0_protocol() -> None:
    assert isinstance(_FakeRegistry({}), EntityRegistry)


class _RecordingRegistry(_FakeRegistry):
    def __init__(self, verdicts: dict[str, ResolutionVerdict]) -> None:
        super().__init__(verdicts)
        self.added: list[EntityAlias] = []

    def add_alias(self, owner_id: str, entity_id: str, alias: EntityAlias) -> None:
        self.added.append(alias)
        super().add_alias(owner_id, entity_id, alias)


@pytest.mark.asyncio
async def test_an_alias_records_the_persona_it_came_through() -> None:
    """R9-284 review H-1: erasing that persona must be able to find the surface."""
    from persona.graph.models import NodeProvenance  # noqa: PLC0415
    from persona.schema.chunks import WriteSource  # noqa: PLC0415

    reg = _RecordingRegistry({"my doctor": _ambiguous("entity-7")})
    judge = _FakeJudge({"my doctor": "entity-7"})
    provenance = NodeProvenance(
        source=WriteSource.PERSONA_SELF,
        persona_id="persona_a",
        written_at=datetime(2026, 11, 15, tzinfo=UTC),
    )

    await EntityResolver(registry=reg, judge=judge).resolve_mentions(
        "u1", ["my doctor"], provenance=provenance
    )

    assert [(a.surface, a.persona_id) for a in reg.added] == [("my doctor", "persona_a")]
