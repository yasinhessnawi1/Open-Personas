"""Tests for the Spec 28 ``WorkspaceDirPersister`` adapter (A6)."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from persona.schema.tools import PersistedArtifact
from persona.tools.workspace_persister import WorkspacePersister
from persona_api.sandbox import (
    SandboxRequestContext,
    reset_sandbox_request_context,
    set_sandbox_request_context,
)
from persona_api.services.workspace_persister import WorkspaceDirPersister
from persona_api.storage import LocalFileStorage

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


@pytest.fixture
def _ctx() -> Iterator[None]:
    token = set_sandbox_request_context(
        SandboxRequestContext(owner_id="user-1", conversation_id="conv-A")
    )
    try:
        yield
    finally:
        reset_sandbox_request_context(token)


class TestWorkspaceDirPersister:
    def test_satisfies_protocol(self, tmp_path: Path) -> None:
        p = WorkspaceDirPersister(file_storage=LocalFileStorage(tmp_path), persona_id="astrid")
        assert isinstance(p, WorkspacePersister)

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_ctx")
    async def test_persist_writes_bytes_under_owner_persona_uploads(self, tmp_path: Path) -> None:
        p = WorkspaceDirPersister(file_storage=LocalFileStorage(tmp_path), persona_id="astrid")
        art = await p.persist(b"hello", mime_type="text/markdown", suggested_filename="report.md")

        assert isinstance(art, PersistedArtifact)
        assert art.workspace_path.startswith("uploads/")
        assert art.workspace_path.endswith(".md")
        assert art.mime_type == "text/markdown"
        assert art.size_bytes == 5
        # bytes land under <root>/<owner>/<persona>/uploads/
        on_disk = tmp_path / "user-1" / "astrid" / art.workspace_path
        assert on_disk.read_bytes() == b"hello"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_ctx")
    async def test_content_addressed_idempotent(self, tmp_path: Path) -> None:
        p = WorkspaceDirPersister(file_storage=LocalFileStorage(tmp_path), persona_id="astrid")
        a1 = await p.persist(b"same", mime_type="image/png", suggested_filename="x.png")
        a2 = await p.persist(b"same", mime_type="image/png", suggested_filename="y.png")
        assert a1.workspace_path == a2.workspace_path  # same bytes → same path

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_ctx")
    async def test_writes_f5_sidecar_producing_spec_28(self, tmp_path: Path) -> None:
        p = WorkspaceDirPersister(file_storage=LocalFileStorage(tmp_path), persona_id="astrid")
        art = await p.persist(
            b"graph TD; A-->B", mime_type="text/vnd.mermaid", suggested_filename="d.mmd"
        )
        sidecar = tmp_path / "user-1" / "astrid" / (art.workspace_path + ".f5.json")
        meta = json.loads(sidecar.read_text())
        assert meta["producing_spec"] == "28"
        assert meta["type"] == "diagram"
        assert meta["source"] == "generated"
        assert meta["conversation_id"] == "conv-A"

    @pytest.mark.asyncio
    @pytest.mark.usefixtures("_ctx")
    async def test_image_mime_tags_sidecar_image(self, tmp_path: Path) -> None:
        p = WorkspaceDirPersister(file_storage=LocalFileStorage(tmp_path), persona_id="astrid")
        art = await p.persist(b"\x89PNG", mime_type="image/png", suggested_filename="i.png")
        sidecar = tmp_path / "user-1" / "astrid" / (art.workspace_path + ".f5.json")
        assert json.loads(sidecar.read_text())["type"] == "image"

    @pytest.mark.asyncio
    async def test_no_context_raises(self, tmp_path: Path) -> None:
        # No sandbox request context bound → persist refuses (fail loud).
        p = WorkspaceDirPersister(file_storage=LocalFileStorage(tmp_path), persona_id="astrid")
        with pytest.raises(RuntimeError):
            await p.persist(b"x", mime_type="text/plain", suggested_filename="x.txt")
