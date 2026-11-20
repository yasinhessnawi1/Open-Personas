"""The checkpoint kinds round-trip through one helper (R9-220).

The leg cap counts the ``leg`` kind only, so the four writers and the counter must agree on
the spelling. Anything no writer mints reads as no kind at all, never as a leg.
"""

from __future__ import annotations

import pytest
from persona.tasks import CheckpointKind, checkpoint_id, checkpoint_kind


@pytest.mark.parametrize("kind", list(CheckpointKind))
def test_every_kind_reads_back_as_itself(kind: CheckpointKind) -> None:
    assert checkpoint_kind("t1", checkpoint_id("t1", kind, 7)) is kind


def test_the_leg_spelling_is_the_one_shipped_checkpoints_carry() -> None:
    """Checkpoints written before the helper existed say ``{task}:leg:{seq}``."""
    assert checkpoint_id("t1", CheckpointKind.LEG, 3) == "t1:leg:3"


@pytest.mark.parametrize(
    "leg_id", ["t1:leg:", "t1:leg:x", "t2:leg:3", "t1:legs:3", "t1:retry:3", "leg:3"]
)
def test_a_shape_no_writer_mints_is_no_kind(leg_id: str) -> None:
    assert checkpoint_kind("t1", leg_id) is None
