"""Tests for ``persona.stores.versioning``."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.errors import BrokenVersionChainError
from persona.schema.chunks import ChunkProvenance, PersonaChunk, WriteSource
from persona.stores.versioning import (
    compute_next_version,
    current_version,
    link_supersedes,
    validate_chain,
)

UTC_NOW = datetime(2026, 5, 27, 12, 0, 0, tzinfo=UTC)


def _chunk(
    *,
    chunk_id: str,
    logical_id: str,
    version: int,
    superseded_by: str | None = None,
    text: str = "t",
) -> PersonaChunk:
    return PersonaChunk(
        id=chunk_id,
        text=text,
        created_at=UTC_NOW,
        provenance=ChunkProvenance(
            source=WriteSource.USER,
            logical_id=logical_id,
            version=version,
            superseded_by=superseded_by,
            written_at=UTC_NOW,
        ),
    )


class TestComputeNextVersion:
    def test_empty_chain_returns_one(self) -> None:
        assert compute_next_version([], "lid") == 1

    def test_no_matching_logical_id_returns_one(self) -> None:
        existing = [_chunk(chunk_id="a", logical_id="other", version=1)]
        assert compute_next_version(existing, "lid") == 1

    def test_returns_max_plus_one_for_matching_chain(self) -> None:
        existing = [
            _chunk(chunk_id="v1", logical_id="lid", version=1, superseded_by="v2"),
            _chunk(chunk_id="v2", logical_id="lid", version=2),
            _chunk(chunk_id="a", logical_id="other", version=99),
        ]
        assert compute_next_version(existing, "lid") == 3

    def test_ignores_chunks_without_provenance(self) -> None:
        no_prov = PersonaChunk(id="x", text="t", created_at=UTC_NOW)
        existing = [
            no_prov,
            _chunk(chunk_id="v1", logical_id="lid", version=1),
        ]
        assert compute_next_version(existing, "lid") == 2


class TestCurrentVersion:
    def test_returns_none_for_unknown_chain(self) -> None:
        assert current_version([], "lid") is None

    def test_returns_head_of_chain(self) -> None:
        existing = [
            _chunk(chunk_id="v1", logical_id="lid", version=1, superseded_by="v2"),
            _chunk(chunk_id="v2", logical_id="lid", version=2),
        ]
        head = current_version(existing, "lid")
        assert head is not None
        assert head.id == "v2"

    def test_raises_when_multiple_heads_exist(self) -> None:
        # Broken chain: two non-superseded versions for the same logical_id.
        existing = [
            _chunk(chunk_id="v1", logical_id="lid", version=1),
            _chunk(chunk_id="v2", logical_id="lid", version=2),
        ]
        with pytest.raises(BrokenVersionChainError, match="multiple head"):
            current_version(existing, "lid")


class TestLinkSupersedes:
    def test_returns_copy_with_superseded_by_set(self) -> None:
        prev = _chunk(chunk_id="v1", logical_id="lid", version=1)
        new = link_supersedes(prev, "v2")
        assert new.provenance is not None
        assert new.provenance.superseded_by == "v2"
        # Original is unchanged (frozen).
        assert prev.provenance is not None
        assert prev.provenance.superseded_by is None

    def test_rejects_chunk_without_provenance(self) -> None:
        no_prov = PersonaChunk(id="x", text="t", created_at=UTC_NOW)
        with pytest.raises(BrokenVersionChainError):
            link_supersedes(no_prov, "v2")


class TestValidateChain:
    def test_empty_chain_is_valid(self) -> None:
        validate_chain([])

    def test_single_version_with_no_supersedes_is_valid(self) -> None:
        validate_chain([_chunk(chunk_id="v1", logical_id="lid", version=1)])

    def test_well_formed_three_version_chain(self) -> None:
        chain = [
            _chunk(chunk_id="v1", logical_id="lid", version=1, superseded_by="v2"),
            _chunk(chunk_id="v2", logical_id="lid", version=2, superseded_by="v3"),
            _chunk(chunk_id="v3", logical_id="lid", version=3),
        ]
        validate_chain(chain)

    def test_missing_provenance_raises(self) -> None:
        chain = [PersonaChunk(id="x", text="t", created_at=UTC_NOW)]
        with pytest.raises(BrokenVersionChainError, match="missing provenance"):
            validate_chain(chain)

    def test_multiple_logical_ids_raises(self) -> None:
        chain = [
            _chunk(chunk_id="v1", logical_id="a", version=1),
            _chunk(chunk_id="v2", logical_id="b", version=2),
        ]
        with pytest.raises(BrokenVersionChainError, match="multiple logical_ids"):
            validate_chain(chain)

    def test_non_contiguous_versions_raises(self) -> None:
        """Spec K13 changed the WORDS this raises, not the fact that it raises.

        The message a data defect carries is now product copy for the person whose memory
        stopped working, so a test that matched the old developer phrasing would pin prose
        that is meant to be improved. These match the structured ``defect`` instead, which is
        the part that is a contract (``ChainDefect``), and the copy stays free to change.
        """
        chain = [
            _chunk(chunk_id="v1", logical_id="lid", version=1, superseded_by="v3"),
            _chunk(chunk_id="v3", logical_id="lid", version=3),
        ]
        with pytest.raises(BrokenVersionChainError, match="defect=missing_version"):
            validate_chain(chain)

    def test_duplicate_versions_raises(self) -> None:
        chain = [
            _chunk(chunk_id="v1", logical_id="lid", version=1),
            _chunk(chunk_id="v1b", logical_id="lid", version=1),
        ]
        with pytest.raises(BrokenVersionChainError):
            validate_chain(chain)

    def test_wrong_supersedes_pointer_raises(self) -> None:
        chain = [
            _chunk(chunk_id="v1", logical_id="lid", version=1, superseded_by="not_v2"),
            _chunk(chunk_id="v2", logical_id="lid", version=2),
        ]
        with pytest.raises(BrokenVersionChainError, match="defect=dangling_link"):
            validate_chain(chain)

    def test_tail_with_supersedes_raises(self) -> None:
        chain = [_chunk(chunk_id="v1", logical_id="lid", version=1, superseded_by="ghost")]
        with pytest.raises(BrokenVersionChainError, match="defect=dangling_link"):
            validate_chain(chain)


class TestMemoryStoreProtocolImport:
    """Smoke test that the protocol type and helpers are exported."""

    def test_protocol_re_exported(self) -> None:
        from persona.stores import MemoryStore  # noqa: PLC0415 — runtime check

        # `MemoryStore` is a Protocol; we only verify it imports and is
        # marked runtime_checkable.
        assert hasattr(MemoryStore, "__protocol_attrs__") or hasattr(MemoryStore, "_is_protocol")
