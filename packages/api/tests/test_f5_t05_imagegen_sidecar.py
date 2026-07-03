"""Spec F5 T05 — producer-touch test for imagegen service sidecar writes.

Validates that ``imagegen.service._persist_bytes`` writes a
``WorkspaceArtifactMetadata`` sidecar with source="generated",
type="image", producing_spec="15" per D-F5-X-artifact-metadata-convention.

Spec R5 (R5-D-4): ``_persist_bytes`` now writes through the ``FileStorage`` seam;
``LocalFileStorage(root)`` under key ``{owner}/{persona}/{relative}`` is
byte-identical to the pre-R5 inline write, so the on-disk assertions still hold.
"""

from __future__ import annotations

import io
from typing import TYPE_CHECKING

import pytest
from persona_api.imagegen.service import _persist_bytes
from persona_api.services.artifact_metadata import SIDECAR_SUFFIX, read_artifact_sidecar
from persona_api.storage import LocalFileStorage

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


def _make_png() -> bytes:
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("Pillow not installed; required for image bytes")
    img = Image.new("RGB", (16, 16), (0, 128, 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def png_bytes() -> bytes:
    return _make_png()


class _SidecarFailingStorage:
    """Wraps LocalFileStorage but raises on the ``.f5.json`` sidecar put — proves
    the sidecar write is best-effort (its failure is swallowed by ``write_sidecar``)."""

    def __init__(self, root: Path) -> None:
        self._inner = LocalFileStorage(root)

    def put(self, key: str, data: bytes, *, content_type: str | None = None) -> str:
        if key.endswith(SIDECAR_SUFFIX):
            raise OSError("simulated disk failure")
        return self._inner.put(key, data, content_type=content_type)

    def get(self, key: str) -> bytes:
        return self._inner.get(key)

    def open_stream(self, key: str) -> Iterator[bytes]:
        return self._inner.open_stream(key)

    def exists(self, key: str) -> bool:
        return self._inner.exists(key)

    def delete(self, key: str) -> bool:
        return self._inner.delete(key)

    def list(self, prefix: str) -> Iterator[object]:
        return self._inner.list(prefix)


def test_imagegen_persist_writes_f5_sidecar(tmp_path: Path, png_bytes: bytes) -> None:
    relative = _persist_bytes(
        file_storage=LocalFileStorage(tmp_path),
        owner_id="u1",
        persona_id="astrid",
        image_bytes=png_bytes,
        media_type="image/png",
    )

    bytes_path = tmp_path / "u1" / "astrid" / relative
    assert bytes_path.is_file()

    sidecar = bytes_path.parent / f"{bytes_path.name}{SIDECAR_SUFFIX}"
    assert sidecar.is_file()

    meta = read_artifact_sidecar(bytes_path)
    assert meta is not None
    assert meta.source == "generated"
    assert meta.type == "image"
    assert meta.producing_spec == "15"
    assert meta.conversation_id is None
    assert meta.original_name is None


def test_imagegen_persist_with_conversation_id(tmp_path: Path, png_bytes: bytes) -> None:
    relative = _persist_bytes(
        file_storage=LocalFileStorage(tmp_path),
        owner_id="u1",
        persona_id="astrid",
        image_bytes=png_bytes,
        media_type="image/png",
        conversation_id="conv-42",
    )

    bytes_path = tmp_path / "u1" / "astrid" / relative
    meta = read_artifact_sidecar(bytes_path)
    assert meta is not None
    assert meta.conversation_id == "conv-42"


def test_imagegen_sidecar_failure_does_not_abort_persist(tmp_path: Path, png_bytes: bytes) -> None:
    """Sidecar write failure must not break the persist (bytes are primary)."""
    relative = _persist_bytes(
        file_storage=_SidecarFailingStorage(tmp_path),  # type: ignore[arg-type]
        owner_id="u1",
        persona_id="astrid",
        image_bytes=png_bytes,
        media_type="image/png",
    )

    bytes_path = tmp_path / "u1" / "astrid" / relative
    assert bytes_path.is_file()  # bytes still landed
    sidecar = bytes_path.parent / f"{bytes_path.name}{SIDECAR_SUFFIX}"
    assert not sidecar.is_file()  # no sidecar — graceful degradation


__all__: list[str] = []
