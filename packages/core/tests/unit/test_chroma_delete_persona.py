"""Dropping a Chroma collection: a missing one is success, any other failure is not (R9-284).

``delete_persona`` caught every exception and returned, because Chroma raises when the
collection is not there and a delete is idempotent. So a locked file on Windows, or a broken
store, read as a successful delete: the memory stayed on disk and the caller was told it went.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 - used at runtime in fixtures
from unittest.mock import patch

import pytest
from persona.stores.chroma import ChromaBackend
from persona.stores.embedder import HashEmbedder


@pytest.fixture
def backend(tmp_path: Path) -> ChromaBackend:
    pytest.importorskip("chromadb")
    return ChromaBackend(persist_path=tmp_path / "chroma", embedder=HashEmbedder())


def test_a_collection_that_is_not_there_is_success(backend: ChromaBackend) -> None:
    backend.delete_persona("never_written", "episodic")
    backend.delete_persona("never_written", "episodic")


def test_any_other_failure_reaches_the_caller(backend: ChromaBackend) -> None:
    with (
        patch.object(backend._client, "delete_collection", side_effect=PermissionError("locked")),  # noqa: SLF001
        pytest.raises(PermissionError),
    ):
        backend.delete_persona("p1", "episodic")
