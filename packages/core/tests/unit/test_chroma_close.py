"""Closing the Chroma backend releases its folder (Spec WIN T2).

Windows refuses to rename a folder whose files are open; the community auto-import
renames the legacy Chroma folder after a complete import, so it closes the backend
first. On Linux the rename works either way; on Windows this test fails without
the close.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.stores.chroma import ChromaBackend
from persona.stores.embedder import HashEmbedder

if TYPE_CHECKING:
    from pathlib import Path


def test_a_closed_backend_leaves_its_folder_free_to_rename(tmp_path: Path) -> None:
    pytest.importorskip("chromadb")
    folder = tmp_path / ".persona_chroma"
    backend = ChromaBackend(persist_path=folder, embedder=HashEmbedder())
    assert backend.get_all(persona_id="p1", store_kind="identity") == []
    backend.close()
    moved = folder.rename(folder.with_name(".persona_chroma.migrated"))
    assert moved.is_dir()
