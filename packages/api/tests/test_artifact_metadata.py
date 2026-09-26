"""Tests for Spec F5 T03 — artifact metadata sidecar (D-F5-2).

Validates the WorkspaceArtifactMetadata Pydantic shape + the sidecar
read/write/delete helpers per D-F5-X-artifact-metadata-convention.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from persona_api.services.artifact_metadata import (
    SIDECAR_SUFFIX,
    SPEC_14_SIDECAR_SUFFIX,
    WorkspaceArtifactMetadata,
    delete_artifact_sidecar,
    is_any_sidecar,
    read_artifact_sidecar,
    sidecar_path_for,
    utcnow,
    write_artifact_sidecar,
)
from pydantic import ValidationError

# -- shape validation --------------------------------------------------------


def test_workspace_artifact_metadata_is_frozen() -> None:
    meta = WorkspaceArtifactMetadata(
        source="upload",
        type="image",
        producing_spec="13",
        conversation_id="conv-1",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        original_name="photo.png",
    )
    with pytest.raises(ValidationError):
        meta.source = "generated"  # type: ignore[misc]


def test_workspace_artifact_metadata_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError) as exc:
        WorkspaceArtifactMetadata(
            source="upload",
            type="image",
            producing_spec="13",
            conversation_id=None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            original_name=None,
            stray_field="nope",  # type: ignore[call-arg]
        )
    assert "stray_field" in str(exc.value)


@pytest.mark.parametrize("invalid_source", ["other", "user", "", "UPLOAD"])
def test_workspace_artifact_metadata_rejects_invalid_source(invalid_source: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceArtifactMetadata(
            source=invalid_source,  # type: ignore[arg-type]
            type="image",
            producing_spec="13",
            conversation_id=None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            original_name=None,
        )


@pytest.mark.parametrize("invalid_type", ["video", "audio", "binary"])
def test_workspace_artifact_metadata_rejects_invalid_type(invalid_type: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceArtifactMetadata(
            source="generated",
            type=invalid_type,  # type: ignore[arg-type]
            producing_spec="15",
            conversation_id=None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            original_name=None,
        )


@pytest.mark.parametrize("invalid_spec", ["1", "11", "18", "F5"])
def test_workspace_artifact_metadata_rejects_invalid_producing_spec(invalid_spec: str) -> None:
    with pytest.raises(ValidationError):
        WorkspaceArtifactMetadata(
            source="generated",
            type="chart",
            producing_spec=invalid_spec,  # type: ignore[arg-type]
            conversation_id=None,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            original_name=None,
        )


def test_workspace_artifact_metadata_requires_tz_aware_datetime() -> None:
    with pytest.raises(ValidationError):
        WorkspaceArtifactMetadata(
            source="upload",
            type="image",
            producing_spec="13",
            conversation_id=None,
            created_at=datetime(2026, 1, 1),  # naive — should reject
            original_name=None,
        )


# -- sidecar path helper -----------------------------------------------------


def test_sidecar_path_appends_suffix(tmp_path: Path) -> None:
    bytes_path = tmp_path / "uploads" / "abc.png"
    expected = tmp_path / "uploads" / f"abc.png{SIDECAR_SUFFIX}"
    assert sidecar_path_for(bytes_path) == expected


def test_sidecar_suffix_is_f5_json() -> None:
    """F5 uses ``.f5.json`` to avoid collision with Spec 14's ``.meta.json``
    document sidecar shape (Phase 5 discovery — see artifact_metadata
    module docstring)."""
    assert SIDECAR_SUFFIX == ".f5.json"


def test_spec_14_sidecar_suffix_is_recorded() -> None:
    """Regression: Spec 14's existing sidecar suffix must stay enumerable
    so F5's artifact-list endpoint can skip it during the workspace walk."""
    assert SPEC_14_SIDECAR_SUFFIX == ".meta.json"


def test_is_any_sidecar_recognises_both_suffixes(tmp_path: Path) -> None:
    """is_any_sidecar must return True for both F5 sidecars and Spec 14's
    pre-existing document sidecars so the enumeration skips both."""
    f5 = tmp_path / "abc.png.f5.json"
    spec14 = tmp_path / "report.pdf.meta.json"
    bytes_path = tmp_path / "abc.png"
    f5.write_text("{}")
    spec14.write_text("{}")
    bytes_path.write_bytes(b"X")
    assert is_any_sidecar(f5) is True
    assert is_any_sidecar(spec14) is True
    assert is_any_sidecar(bytes_path) is False


# -- write + read round-trip -------------------------------------------------


def _make_meta(
    source: str = "upload",
    type_: str = "image",
    producing_spec: str = "13",
    conversation_id: str | None = "conv-1",
    original_name: str | None = "photo.png",
) -> WorkspaceArtifactMetadata:
    return WorkspaceArtifactMetadata(
        source=source,  # type: ignore[arg-type]
        type=type_,  # type: ignore[arg-type]
        producing_spec=producing_spec,  # type: ignore[arg-type]
        conversation_id=conversation_id,
        created_at=datetime(2026, 6, 7, 12, 0, tzinfo=UTC),
        original_name=original_name,
    )


def test_write_then_read_round_trip(tmp_path: Path) -> None:
    bytes_path = tmp_path / "uploads" / "abc.png"
    bytes_path.parent.mkdir(parents=True)
    bytes_path.write_bytes(b"FAKE PNG")

    meta = _make_meta()
    write_artifact_sidecar(bytes_path, meta, root=tmp_path)

    sidecar = sidecar_path_for(bytes_path)
    assert sidecar.is_file()

    loaded = read_artifact_sidecar(bytes_path, root=tmp_path)
    assert loaded == meta


def test_read_returns_none_when_sidecar_missing(tmp_path: Path) -> None:
    bytes_path = tmp_path / "uploads" / "abc.png"
    bytes_path.parent.mkdir(parents=True)
    bytes_path.write_bytes(b"FAKE PNG")
    # No sidecar written.
    assert read_artifact_sidecar(bytes_path, root=tmp_path) is None


def test_write_is_idempotent_overwrite(tmp_path: Path) -> None:
    """Re-writing the same path overwrites the sidecar (last-writer-wins)."""
    bytes_path = tmp_path / "uploads" / "abc.png"
    bytes_path.parent.mkdir(parents=True)
    bytes_path.write_bytes(b"FAKE PNG")

    meta_v1 = _make_meta(original_name="version1.png")
    meta_v2 = _make_meta(original_name="version2.png")

    write_artifact_sidecar(bytes_path, meta_v1, root=tmp_path)
    assert read_artifact_sidecar(bytes_path, root=tmp_path) == meta_v1

    write_artifact_sidecar(bytes_path, meta_v2, root=tmp_path)
    assert read_artifact_sidecar(bytes_path, root=tmp_path) == meta_v2


def test_read_raises_validation_error_on_malformed_sidecar(tmp_path: Path) -> None:
    """Malformed sidecars surface the bug rather than silently dropping metadata."""
    bytes_path = tmp_path / "uploads" / "abc.png"
    bytes_path.parent.mkdir(parents=True)
    bytes_path.write_bytes(b"FAKE PNG")

    sidecar = sidecar_path_for(bytes_path)
    sidecar.write_text('{"not_a_valid": "shape"}', encoding="utf-8")

    with pytest.raises(ValidationError):
        read_artifact_sidecar(bytes_path, root=tmp_path)


def test_sidecar_json_is_valid_pydantic_serialisation(tmp_path: Path) -> None:
    bytes_path = tmp_path / "uploads" / "abc.png"
    bytes_path.parent.mkdir(parents=True)
    bytes_path.write_bytes(b"FAKE PNG")

    meta = _make_meta()
    write_artifact_sidecar(bytes_path, meta, root=tmp_path)

    sidecar = sidecar_path_for(bytes_path)
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["source"] == "upload"
    assert payload["type"] == "image"
    assert payload["producing_spec"] == "13"
    assert payload["conversation_id"] == "conv-1"
    assert payload["original_name"] == "photo.png"
    # created_at is ISO-8601 string
    assert "2026-06-07" in payload["created_at"]


# -- delete consistency ------------------------------------------------------


def test_delete_removes_sidecar_and_returns_true(tmp_path: Path) -> None:
    bytes_path = tmp_path / "uploads" / "abc.png"
    bytes_path.parent.mkdir(parents=True)
    bytes_path.write_bytes(b"FAKE PNG")
    write_artifact_sidecar(bytes_path, _make_meta(), root=tmp_path)

    assert sidecar_path_for(bytes_path).is_file()
    assert delete_artifact_sidecar(bytes_path, root=tmp_path) is True
    assert not sidecar_path_for(bytes_path).is_file()


def test_delete_returns_false_when_sidecar_missing(tmp_path: Path) -> None:
    bytes_path = tmp_path / "uploads" / "abc.png"
    bytes_path.parent.mkdir(parents=True)
    bytes_path.write_bytes(b"FAKE PNG")
    # No sidecar.
    assert delete_artifact_sidecar(bytes_path, root=tmp_path) is False


def test_delete_consistency_then_read_returns_none(tmp_path: Path) -> None:
    """After deleting bytes + sidecar, a subsequent read returns None."""
    bytes_path = tmp_path / "uploads" / "abc.png"
    bytes_path.parent.mkdir(parents=True)
    bytes_path.write_bytes(b"FAKE PNG")
    write_artifact_sidecar(bytes_path, _make_meta(), root=tmp_path)

    bytes_path.unlink()  # delete bytes first per D-F5-X-artifact-delete-shape
    delete_artifact_sidecar(bytes_path, root=tmp_path)  # then sidecar

    assert read_artifact_sidecar(bytes_path, root=tmp_path) is None


# -- producer matrix (table coverage of valid combinations) ------------------


@pytest.mark.parametrize(
    ("source", "artifact_type", "producing_spec"),
    [
        ("upload", "image", "13"),
        ("upload", "doc", "14"),
        ("generated", "image", "15"),
        ("generated", "doc", "16"),
        ("generated", "chart", "17"),
        ("generated", "data", "17"),
        ("generated", "doc", "12"),
    ],
)
def test_valid_producer_combinations(
    tmp_path: Path,
    source: str,
    artifact_type: str,
    producing_spec: str,
) -> None:
    """The valid (source, type, producing_spec) tuples per D-F5-X-artifact-metadata-convention."""
    bytes_path = tmp_path / "f.bin"
    bytes_path.write_bytes(b"X")

    meta = _make_meta(source=source, type_=artifact_type, producing_spec=producing_spec)
    write_artifact_sidecar(bytes_path, meta, root=tmp_path)
    assert read_artifact_sidecar(bytes_path, root=tmp_path) == meta


def test_conversation_id_can_be_none(tmp_path: Path) -> None:
    bytes_path = tmp_path / "f.bin"
    bytes_path.write_bytes(b"X")
    meta = _make_meta(conversation_id=None)
    write_artifact_sidecar(bytes_path, meta, root=tmp_path)
    loaded = read_artifact_sidecar(bytes_path, root=tmp_path)
    assert loaded is not None
    assert loaded.conversation_id is None


def test_original_name_can_be_none(tmp_path: Path) -> None:
    bytes_path = tmp_path / "f.bin"
    bytes_path.write_bytes(b"X")
    meta = _make_meta(original_name=None)
    write_artifact_sidecar(bytes_path, meta, root=tmp_path)
    loaded = read_artifact_sidecar(bytes_path, root=tmp_path)
    assert loaded is not None
    assert loaded.original_name is None


# -- utcnow helper -----------------------------------------------------------


def test_utcnow_returns_tz_aware_datetime() -> None:
    now = utcnow()
    assert now.tzinfo is not None
    assert now.tzinfo.utcoffset(now) == datetime.now(tz=UTC).utcoffset()


def test_the_sidecar_functions_require_the_workspace_root() -> None:
    """T1.6 L-C: root is keyword-only with no default, so no caller can fall back to a
    weaker final-component-only write by forgetting it."""
    import inspect

    from persona_api.services.artifact_metadata import (
        delete_artifact_sidecar,
        read_artifact_sidecar,
        write_artifact_sidecar,
    )

    for function in (write_artifact_sidecar, read_artifact_sidecar, delete_artifact_sidecar):
        root = inspect.signature(function).parameters["root"]
        assert root.kind is inspect.Parameter.KEYWORD_ONLY, function.__name__
        assert root.default is inspect.Parameter.empty, function.__name__
