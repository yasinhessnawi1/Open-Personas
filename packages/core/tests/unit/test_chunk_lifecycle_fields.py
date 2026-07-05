"""Spec K8 T2 — the hash-EXCLUDED lifecycle fields on PersonaChunk (K8-D-2/3).

The content hash is a tamper check over text + metadata. Lifecycle state
(strength / last_recalled_at / band / pinned / member_ids) mutates during
normal operation — reinforcement, tiering, pinning — so it MUST live outside
the hash: toggling any of it never changes chunk identity, never trips the
tamper error, never forces a re-embed. This file is the model-level half of
that guard; the transport halves live in the Chroma/Postgres round-trip suites.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from persona.schema.chunks import PersonaChunk
from pydantic import ValidationError

_NOW = datetime(2026, 7, 4, 12, 0, tzinfo=UTC)


def _chunk(**overrides: object) -> PersonaChunk:
    base: dict[str, object] = {
        "id": "p1::episodic::0001",
        "text": "USER: hei\nASSISTANT: hei!",
        "metadata": {"importance": "0.5"},
        "created_at": _NOW,
    }
    base.update(overrides)
    return PersonaChunk(**base)  # type: ignore[arg-type]


def test_lifecycle_defaults_are_fresh_full_band() -> None:
    c = _chunk()
    assert c.strength == 1
    assert c.last_recalled_at is None
    assert c.band == 0  # FULL
    assert c.pinned is False
    assert c.member_ids == ()


def test_lifecycle_state_never_participates_in_the_content_hash() -> None:
    plain = _chunk()
    lively = _chunk(
        strength=9,
        last_recalled_at=_NOW,
        band=1,
        pinned=True,
        member_ids=("a", "b"),
    )
    assert plain.content_hash == lively.content_hash  # identity is text+metadata only


def test_toggling_lifecycle_fields_never_trips_the_tamper_check() -> None:
    stored = _chunk()
    # A reinforce / demote / pin as the store performs it: copy with new
    # lifecycle values, carrying the ORIGINAL hash — revalidation must accept.
    updated = stored.model_copy(
        update={
            "strength": stored.strength + 1,
            "last_recalled_at": _NOW,
            "band": 1,
            "pinned": True,
        }
    )
    revalidated = PersonaChunk.model_validate(updated.model_dump())
    assert revalidated.content_hash == stored.content_hash
    assert revalidated.strength == 2  # noqa: PLR2004
    assert revalidated.pinned is True


def test_metadata_mutation_still_trips_the_tamper_check() -> None:
    # The guard must not have weakened: metadata IS hashed.
    stored = _chunk()
    tampered = stored.model_dump()
    tampered["metadata"] = {"importance": "0.9"}
    with pytest.raises(ValidationError, match="content_hash mismatch"):
        PersonaChunk.model_validate(tampered)


def test_strength_and_band_reject_invalid_values() -> None:
    with pytest.raises(ValidationError):
        _chunk(strength=0)
    with pytest.raises(ValidationError):
        _chunk(band=-1)
