"""Spec F5 T04 — producer-touch tests for F3 upload sidecar writes.

Validates the image_service.upload + routes/uploads.py extension that writes
F5 ``WorkspaceArtifactMetadata`` sidecars at write-time per D-F5-X-artifact-
metadata-convention. Document_service.upload sidecar coverage is deferred
to v0.2 (Spec 14 uses a sibling-but-different workspace tree —
``persona_<id>/conversations/.../`` rather than ``<owner>/<persona>/``;
F5 artifact-view at v0.1 walks only the owner-scoped tree).
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from persona_api.services import image_service
from persona_api.services.artifact_metadata import (
    SIDECAR_SUFFIX,
    read_artifact_sidecar,
)
from persona_api.storage import LocalFileStorage


def _make_png() -> bytes:
    """Synthesize a minimal valid PNG via Pillow (mirrors integration fixtures)."""
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("Pillow not installed; required for image_service.upload")
    img = Image.new("RGB", (16, 16), (255, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def png_bytes() -> bytes:
    return _make_png()


@pytest.fixture
def workspace_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


def test_image_upload_writes_f5_sidecar_with_correct_metadata(
    workspace_root: Path,
    png_bytes: bytes,
) -> None:
    ref = image_service.upload(
        file_storage=LocalFileStorage(workspace_root),
        owner_id="u1",
        persona_id="astrid",
        file_bytes=png_bytes,
        declared_media_type="image/png",
        conversation_id="conv-1",
        original_name="cat.png",
    )

    bytes_path = workspace_root / "u1" / "astrid" / ref.workspace_path
    assert bytes_path.is_file()

    sidecar = bytes_path.parent / f"{bytes_path.name}{SIDECAR_SUFFIX}"
    assert sidecar.is_file()

    meta = read_artifact_sidecar(bytes_path)
    assert meta is not None
    assert meta.source == "upload"
    assert meta.type == "image"
    assert meta.producing_spec == "13"
    assert meta.conversation_id == "conv-1"
    assert meta.original_name == "cat.png"


def test_image_upload_sidecar_handles_none_conversation_id_and_filename(
    workspace_root: Path,
    png_bytes: bytes,
) -> None:
    """The route may not have conversation_id or filename (persona-scoped uploads)."""
    ref = image_service.upload(
        file_storage=LocalFileStorage(workspace_root),
        owner_id="u1",
        persona_id="astrid",
        file_bytes=png_bytes,
        declared_media_type="image/png",
    )
    bytes_path = workspace_root / "u1" / "astrid" / ref.workspace_path
    meta = read_artifact_sidecar(bytes_path)
    assert meta is not None
    assert meta.conversation_id is None
    assert meta.original_name is None


class _SidecarFailingStorage:
    """Wraps LocalFileStorage but raises on the ``.f5.json`` sidecar put — proves
    the sidecar write is best-effort (R5-D-4: ``write_sidecar`` swallows it)."""

    def __init__(self, root: Path) -> None:
        self._inner = LocalFileStorage(root)

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        if key.endswith(SIDECAR_SUFFIX):
            raise OSError("simulated disk failure")
        return self._inner.put(key, data, content_type=content_type)

    def get(self, key: str) -> bytes:
        return self._inner.get(key)

    def open_stream(self, key: str) -> object:
        return self._inner.open_stream(key)

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)

    def delete(self, key: str) -> bool:
        return self._inner.delete(key)

    def list(self, prefix: str) -> object:
        return self._inner.list(prefix)


def test_image_upload_sidecar_write_failure_does_not_abort_upload(
    workspace_root: Path,
    png_bytes: bytes,
) -> None:
    """Sidecar write is best-effort — a failure must NOT abort the upload."""
    ref = image_service.upload(
        file_storage=_SidecarFailingStorage(workspace_root),  # type: ignore[arg-type]
        owner_id="u1",
        persona_id="astrid",
        file_bytes=png_bytes,
        declared_media_type="image/png",
    )
    bytes_path = workspace_root / "u1" / "astrid" / ref.workspace_path
    assert bytes_path.is_file()  # bytes still landed
    sidecar = bytes_path.parent / f"{bytes_path.name}{SIDECAR_SUFFIX}"
    assert not sidecar.is_file()  # no sidecar — graceful degradation


def test_image_upload_sidecar_idempotent_on_re_upload(
    workspace_root: Path,
    png_bytes: bytes,
) -> None:
    """blake2b ref → re-uploading identical bytes overwrites the sidecar
    with the new metadata (last-writer-wins per D-F5-X-artifact-metadata-
    convention)."""
    ref1 = image_service.upload(
        file_storage=LocalFileStorage(workspace_root),
        owner_id="u1",
        persona_id="astrid",
        file_bytes=png_bytes,
        declared_media_type="image/png",
        original_name="first.png",
    )
    ref2 = image_service.upload(
        file_storage=LocalFileStorage(workspace_root),
        owner_id="u1",
        persona_id="astrid",
        file_bytes=png_bytes,
        declared_media_type="image/png",
        original_name="second.png",
    )
    # Content-addressed: same ref both times.
    assert ref1.workspace_path == ref2.workspace_path

    bytes_path = workspace_root / "u1" / "astrid" / ref2.workspace_path
    meta = read_artifact_sidecar(bytes_path)
    assert meta is not None
    assert meta.original_name == "second.png"  # last-writer-wins
