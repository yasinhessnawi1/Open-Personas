"""The pointer translation and its bound (R9-162)."""

from __future__ import annotations

from persona.schema.tools import PersistedArtifact
from persona.tasks import (
    MAX_ARTIFACT_POINTERS,
    WORKSPACE_POINTER_KIND,
    ArtifactPointer,
    merge_artifact_pointers,
    pointers_from_artifacts,
)


def _artifact(path: str) -> PersistedArtifact:
    return PersistedArtifact(workspace_path=path, mime_type="image/png", size_bytes=10)


def test_a_persisted_artifact_becomes_a_workspace_pointer() -> None:
    assert pointers_from_artifacts([_artifact("uploads/a.png")]) == (
        ArtifactPointer(kind=WORKSPACE_POINTER_KIND, ref="uploads/a.png"),
    )


def test_the_same_file_twice_is_one_pointer() -> None:
    pointers = pointers_from_artifacts([_artifact("a.png"), _artifact("b.png"), _artifact("a.png")])
    assert [p.ref for p in pointers] == ["a.png", "b.png"]


def test_a_blank_path_is_not_a_pointer() -> None:
    assert pointers_from_artifacts([_artifact("   ")]) == ()


def test_merge_keeps_the_earlier_position_of_a_rewritten_file() -> None:
    prior = (
        ArtifactPointer(kind="workspace", ref="a.md"),
        ArtifactPointer(kind="workspace", ref="b.md"),
    )
    fresh = (
        ArtifactPointer(kind="workspace", ref="a.md"),
        ArtifactPointer(kind="workspace", ref="c.md"),
    )
    assert [p.ref for p in merge_artifact_pointers(prior, fresh)] == ["a.md", "b.md", "c.md"]


def test_merge_distinguishes_kinds() -> None:
    merged = merge_artifact_pointers(
        (ArtifactPointer(kind="workspace", ref="r"),), (ArtifactPointer(kind="url", ref="r"),)
    )
    assert len(merged) == 2


def test_merge_bounds_the_list_keeping_the_newest() -> None:
    prior = tuple(
        ArtifactPointer(kind="workspace", ref=f"{i}.md") for i in range(MAX_ARTIFACT_POINTERS + 5)
    )
    merged = merge_artifact_pointers(prior, (ArtifactPointer(kind="workspace", ref="new.md"),))
    assert len(merged) == MAX_ARTIFACT_POINTERS
    assert merged[-1].ref == "new.md"
    assert merged[0].ref == "6.md"
