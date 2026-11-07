"""A chain two writers raced can be resolved, by an operator, and only that exact shape (R9-205).

Two writers that read one chain both wrote version N+1. Neither is wrong, both rows are on disk,
and the chain then refuses every later write. K13's repair refuses it, because in general two rows
claiming one version mean the version order is not the truth, and that refusal stands for every
other shape. This one shape is different: below the top the chain is sound, so the version order
is the truth up to the race, and what is missing is only which of two rows came first.

The chain itself mostly answers that. The prior head's ``superseded_by`` was overwritten by each
writer in turn, so it names the writer that committed LAST. Any others go by ``written_at``. No
version is hidden: the earlier one stays in the history and the later one is current, which is
what would have happened had the writes not raced.

It sits behind an explicit flag, dry run by default, audited. The near misses below are the point
of this file: a repair that accepts a shape it should refuse rewrites someone's history.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from persona.audit import AuditAction, MemoryAuditLogger
from persona.errors import BrokenVersionChainError
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.self_facts import SelfFactsStore
from persona.stores.versioning import (
    ChainDefect,
    diagnose_chain,
    plan_raced_resolution,
    settled_chains,
    validate_chain,
)

_NOW = datetime(2026, 11, 6, 9, 0, tzinfo=UTC)
_PERSONA = "p1"
_LOGICAL = "L"


def _row(
    chunk_id: str,
    version: int,
    superseded_by: str | None,
    *,
    seconds: int = 0,
) -> PersonaChunk:
    written = _NOW + timedelta(seconds=seconds)
    return PersonaChunk(
        id=chunk_id,
        text=f"text of {chunk_id}",
        metadata={"confidence": "0.9"},
        created_at=written,
        provenance=ChunkProvenance(
            source=WriteSource.USER,
            logical_id=_LOGICAL,
            version=version,
            superseded_by=superseded_by,
            written_at=written,
        ),
    )


def _raced() -> list[PersonaChunk]:
    """v1 then v2, then two writers at v3. The prior head names ``b``, which committed last."""
    return [
        _row("v1", 1, "v2"),
        _row("v2", 2, "b"),
        _row("a", 3, None, seconds=1),
        _row("b", 3, None, seconds=2),
    ]


class _Backend:
    def __init__(self, chain: list[PersonaChunk]) -> None:
        self.rows = {c.id: c for c in chain}
        self.upserts = 0

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        _ = (persona_id, store_kind)
        return list(self.rows.values())

    def get_by_logical_ids(
        self, *, persona_id: str, store_kind: str, logical_ids: list[str]
    ) -> list[PersonaChunk]:
        _ = (persona_id, store_kind, logical_ids)
        return list(self.rows.values())

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[PersonaChunk]) -> None:
        _ = (persona_id, store_kind)
        self.upserts += 1
        self.rows.update({c.id: c for c in chunks})

    def relink(
        self,
        *,
        persona_id: str,
        store_kind: str,
        links: dict[str, str | None],
        versions: dict[str, int] | None = None,
    ) -> None:
        _ = (persona_id, store_kind)
        for chunk_id, target in links.items():
            chunk = self.rows[chunk_id]
            assert chunk.provenance is not None
            update: dict[str, object] = {"superseded_by": target}
            if versions and chunk_id in versions:
                update["version"] = versions[chunk_id]
            self.rows[chunk_id] = chunk.model_copy(
                update={"provenance": chunk.provenance.model_copy(update=update)}
            )


def _store(chain: list[PersonaChunk]) -> tuple[SelfFactsStore, _Backend, MemoryAuditLogger]:
    backend = _Backend(chain)
    audit = MemoryAuditLogger()
    return SelfFactsStore(backend=backend, audit_logger=audit), backend, audit  # type: ignore[arg-type]


def test_the_raced_shape_has_its_own_name() -> None:
    diagnosis = diagnose_chain(_raced())
    assert diagnosis.defect is ChainDefect.RACED_HEADS
    assert diagnosis.resolvable
    assert not diagnosis.repairable, "a plain --apply must not touch it"
    assert set(diagnosis.offending_ids) == {"a", "b"}


def test_a_first_write_race_is_the_same_shape_with_nothing_below() -> None:
    chain = [_row("a", 1, None, seconds=1), _row("b", 1, None, seconds=2)]
    assert diagnose_chain(chain).defect is ChainDefect.RACED_HEADS


def test_three_writers_are_the_same_shape() -> None:
    chain = [_row("v1", 1, "c"), *[_row(i, 2, None) for i in ("a", "b", "c")]]
    assert diagnose_chain(chain).defect is ChainDefect.RACED_HEADS


@pytest.mark.parametrize(
    ("label", "chain"),
    [
        (
            "duplicates below the top",
            [_row("a", 1, "c"), _row("b", 1, "c"), _row("c", 2, None)],
        ),
        (
            "one of the top rows is superseded",
            [_row("v1", 1, "b"), _row("a", 2, "b"), _row("b", 2, None)],
        ),
        (
            "the prior head names a row that was never written",
            [_row("v1", 1, "gone"), _row("a", 2, None), _row("b", 2, None)],
        ),
        (
            "the prior head names neither top row",
            [_row("v1", 1, "v2"), _row("v2", 2, "v1"), _row("a", 3, None), _row("b", 3, None)],
        ),
        (
            "a link below the top is crossed",
            [_row("v1", 1, "b"), _row("v2", 2, "b"), _row("a", 3, None), _row("b", 3, None)],
        ),
        (
            "the prior head was never linked",
            [_row("v1", 1, None), _row("a", 2, None), _row("b", 2, None)],
        ),
    ],
    ids=lambda v: v if isinstance(v, str) else "",
)
def test_every_near_miss_stays_refused(label: str, chain: list[PersonaChunk]) -> None:
    diagnosis = diagnose_chain(chain)
    assert diagnosis.defect is ChainDefect.DUPLICATE_VERSION, label
    assert not diagnosis.repairable
    assert not diagnosis.resolvable


def test_a_version_missing_below_the_race_stays_refused() -> None:
    chain = [_row("v1", 1, "b"), _row("a", 3, None), _row("b", 3, None)]
    diagnosis = diagnose_chain(chain)
    assert diagnosis.defect is not ChainDefect.RACED_HEADS
    assert not diagnosis.resolvable


def test_a_healthy_chain_is_not_resolvable() -> None:
    chain = [_row("v1", 1, "v2"), _row("v2", 2, None)]
    assert not diagnose_chain(chain).resolvable


def test_the_row_the_prior_head_names_goes_last_whatever_its_clock_says() -> None:
    """Link order is what the database saw. Two machines' clocks are only what they claim."""
    chain = [_row("v1", 1, "b"), _row("a", 2, None, seconds=9), _row("b", 2, None, seconds=1)]
    plan = plan_raced_resolution(chain)
    assert plan.links == {"v1": "a", "a": "b", "b": None}
    assert plan.versions == {"a": 2, "b": 3}


def test_the_others_go_by_when_they_were_written() -> None:
    chain = [
        _row("v1", 1, "c"),
        _row("a", 2, None, seconds=5),
        _row("b", 2, None, seconds=1),
        _row("c", 2, None, seconds=3),
    ]
    plan = plan_raced_resolution(chain)
    assert plan.links == {"v1": "b", "b": "a", "a": "c", "c": None}
    assert plan.versions == {"b": 2, "a": 3, "c": 4}


def test_with_no_prior_head_the_clock_decides_and_the_id_breaks_a_tie() -> None:
    chain = [_row("b", 1, None), _row("a", 1, None)]
    plan = plan_raced_resolution(chain)
    assert plan.links == {"a": "b", "b": None}
    assert plan.versions == {"a": 1, "b": 2}


def test_the_plan_refuses_a_chain_that_is_not_the_raced_shape() -> None:
    with pytest.raises(BrokenVersionChainError):
        plan_raced_resolution([_row("v1", 1, "v2"), _row("v2", 2, None)])


def test_a_plain_repair_leaves_it_exactly_as_it_was() -> None:
    chain = _raced()
    store, backend, audit = _store(chain)
    assert store.repair(_PERSONA) == 0
    assert list(backend.rows.values()) == chain
    assert audit.events == []


def test_resolving_keeps_every_version_and_the_last_committer_is_current() -> None:
    store, backend, _ = _store(_raced())

    assert store.repair(_PERSONA, resolve_raced=True) == 1

    chain = store.history(_PERSONA, _LOGICAL)  # validates, and raised before this
    assert [c.id for c in chain] == ["v1", "v2", "a", "b"]
    assert [c.provenance.version for c in chain if c.provenance] == [1, 2, 3, 4]
    assert store.diagnose(_PERSONA).healthy
    assert backend.upserts == 0, "a resolution moves pointers: it must never rewrite or embed"


def test_the_chain_takes_writes_again_afterwards() -> None:
    store, _, _ = _store(_raced())
    store.repair(_PERSONA, resolve_raced=True)
    store.write(
        _PERSONA,
        [_row("next", 1, None)],
        source=WriteSource.USER,
    )
    assert store.history(_PERSONA, _LOGICAL)[-1].id == "next"


def test_the_resolution_is_audited_with_ids_and_counts_and_never_text() -> None:
    store, _, audit = _store(_raced())
    store.repair(_PERSONA, written_by="cli.repair", resolve_raced=True)

    event = audit.events[-1]
    assert event.action is AuditAction.REPAIR
    assert event.written_by == "cli.repair"
    assert event.logical_ids == [_LOGICAL]
    assert set(event.chunk_ids) == {"v2", "a", "b"}
    assert event.metadata == {"defect": "raced_heads", "renumbered": "1"}
    assert "text of" not in event.model_dump_json()


def test_a_second_run_finds_nothing_to_do() -> None:
    store, _, audit = _store(_raced())
    store.repair(_PERSONA, resolve_raced=True)
    before = len(audit.events)
    assert store.repair(_PERSONA, resolve_raced=True) == 0
    assert len(audit.events) == before


def test_the_error_a_person_reads_names_the_flag() -> None:
    with pytest.raises(BrokenVersionChainError, match="--resolve-raced"):
        validate_chain(_raced())


def test_settling_a_copy_fixes_the_raced_chain_and_leaves_the_rest_alone() -> None:
    """For the community import: the source stays as it was, and the copy has one head."""
    untouched = _row("other", 1, None).model_copy(
        update={
            "provenance": ChunkProvenance(
                source=WriteSource.USER, logical_id="other", written_at=_NOW
            )
        }
    )
    source = [*_raced(), untouched]

    copied = {c.id: c for c in settled_chains(source)}

    assert copied["other"] is untouched
    assert [c.id for c in sorted(copied.values(), key=lambda c: c.id) if _is_head(c)] == [
        "b",
        "other",
    ]
    assert copied["b"].provenance.version == 4  # type: ignore[union-attr]
    assert (
        diagnose_chain([c for c in copied.values() if c.id != "other"]).defect is ChainDefect.NONE
    )
    assert diagnose_chain(_raced()).defect is ChainDefect.RACED_HEADS, "the source is not touched"


def test_settling_a_copy_relinks_plain_multiple_heads_too() -> None:
    chain = [_row("v1", 1, None), _row("v2", 2, None)]
    assert diagnose_chain(settled_chains(chain)).defect is ChainDefect.NONE


def test_settling_a_copy_leaves_a_shape_nobody_can_settle() -> None:
    chain = [_row("v1", 1, "b"), _row("v2", 2, "b"), _row("a", 3, None), _row("b", 3, None)]
    assert settled_chains(chain) == chain


def _is_head(chunk: PersonaChunk) -> bool:
    return chunk.provenance is not None and chunk.provenance.superseded_by is None
