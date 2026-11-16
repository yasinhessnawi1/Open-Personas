"""The node CAS's trail comparison holds for every timestamp shape (R9-284 round 11 LOW-3).

``trail_is`` compares the stored provenance with the caller's read re-dumped
(``model_dump(mode="json", exclude_none=True)``) under ``jsonb_strip_nulls``. That only
works while a trail written, stored as JSON, read back through ``NodeProvenance`` and dumped
again comes out byte for byte the same. This pins it for UTC, sub-second and offset inputs,
so a serialiser change that would make every CAS fail (or pass) shows up here.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from persona.graph.models import NodeProvenance
from persona.graph.postgres import trail_matches
from persona.schema.chunks import WriteSource

_INPUTS = {
    "utc": datetime(2026, 11, 15, 12, 0, tzinfo=UTC),
    "utc sub-second": datetime(2026, 11, 15, 12, 0, 0, 123456, tzinfo=UTC),
    "utc millisecond": datetime(2026, 11, 15, 12, 0, 0, 500000, tzinfo=UTC),
    "positive offset": datetime(2026, 11, 15, 14, 0, 0, 7, tzinfo=timezone(timedelta(hours=2))),
    "negative offset": datetime(
        2026, 11, 15, 6, 30, 0, 250, tzinfo=timezone(timedelta(hours=-5, minutes=-30))
    ),
}


def _canonical(value: object) -> str:
    """The bytes jsonb would compare: keys sorted, nulls stripped (``jsonb_strip_nulls``)."""
    if isinstance(value, list):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    if isinstance(value, dict):
        kept = {k: v for k, v in value.items() if v is not None}
        return json.dumps(kept, sort_keys=True, separators=(",", ":"))
    return json.dumps(value)


def _stored(trail: list[NodeProvenance]) -> list[dict[str, object]]:
    """As the insert writes it (``model_dump(mode="json")``) and the driver hands it back."""
    return json.loads(json.dumps([p.model_dump(mode="json") for p in trail]))


@pytest.mark.parametrize("written_at", list(_INPUTS.values()), ids=list(_INPUTS))
def test_a_stored_trail_read_back_and_dumped_again_is_byte_identical(
    written_at: datetime,
) -> None:
    trail = [
        NodeProvenance(source=WriteSource.PERSONA_SELF, persona_id="p1", written_at=written_at),
        NodeProvenance(
            source=WriteSource.USER,
            written_at=written_at + timedelta(microseconds=1),
            reason="user edit",
            superseded_content="before",
        ),
    ]
    stored = _stored(trail)

    read = [NodeProvenance.model_validate(p) for p in stored]  # the row reader's path
    again = [p.model_dump(mode="json") for p in read]
    expected = [p.model_dump(mode="json", exclude_none=True) for p in read]  # the CAS's side

    assert json.dumps(again, sort_keys=True) == json.dumps(stored, sort_keys=True)
    assert _canonical(expected) == _canonical(stored)
    assert trail_matches(stored, read)
    assert read[0].written_at == written_at, "the same instant, whatever the offset"


def test_a_trail_that_differs_by_a_microsecond_does_not_match() -> None:
    """The comparison is exact: the pin above is not passing because everything matches."""
    at = _INPUTS["utc sub-second"]
    stored = _stored([NodeProvenance(source=WriteSource.SYSTEM, written_at=at)])
    other = [NodeProvenance(source=WriteSource.SYSTEM, written_at=at + timedelta(microseconds=1))]

    assert not trail_matches(stored, other)
    assert _canonical([p.model_dump(mode="json", exclude_none=True) for p in other]) != (
        _canonical(stored)
    )


_STORED_TODAY = {
    "utc": "2026-11-15T12:00:00Z",
    "utc sub-second": "2026-11-15T12:00:00.123456Z",
    "utc millisecond": "2026-11-15T12:00:00.500000Z",
    "positive offset": "2026-11-15T12:00:00.000007Z",
    "negative offset": "2026-11-15T12:00:00.000250Z",
}


@pytest.mark.parametrize("name", list(_INPUTS))
def test_trails_already_stored_still_compare_equal_after_a_read(name: str) -> None:
    """Rows written by earlier code hold these exact strings; a serialiser change that
    re-dumps them differently would make every CAS on an existing node fail."""
    stored = [
        {
            "source": "persona_self",
            "persona_id": "p1",
            "interaction_id": None,
            "interaction_kind": None,
            "written_at": _STORED_TODAY[name],
            "grounding": None,
            "reason": None,
            "superseded_content": None,
            "channel": None,
        }
    ]

    read = [NodeProvenance.model_validate(p) for p in stored]

    assert [p.model_dump(mode="json") for p in read] == stored
    assert _canonical([p.model_dump(mode="json", exclude_none=True) for p in read]) == (
        _canonical(stored)
    )
    assert read[0].written_at == _INPUTS[name]
    assert _stored(read) == stored
