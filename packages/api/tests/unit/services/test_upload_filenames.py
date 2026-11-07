"""An uploaded file's name is a name, never a path.

A document is staged for the file tools at ``uploads/<filename>`` (chat_service), and its
name is recorded on the DocumentRef at upload (document_service). The name came from the
multipart upload with only control characters stripped, so ``../conversations/c2/documents/
x.md`` was staged at a path the resolver accepts, because it stays inside the persona's
folder, and it could overwrite another conversation's file there. Only the last component of
the name is kept now, and a name that is nothing (empty, ``.``, ``..``) is refused.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from persona.documents.ingest import IngestStrategy
from persona.stores.document_store import DocumentStore
from persona_api.services import document_service
from persona_api.services.chat_service import _resolve_turn_documents
from persona_api.services.document_service import DOCUMENT_DIR_NAME, DocumentRef
from persona_api.storage import LocalFileStorage

_OWNER = "owner_1"
_PERSONA = "persona_f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1f1"
_CONV = "c1"


class _Backend:
    """Just enough of a memory transport for a DocumentStore write."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str], list[Any]] = {}

    def upsert(self, *, persona_id: str, store_kind: str, chunks: list[Any]) -> None:
        self.rows.setdefault((persona_id, store_kind), []).extend(chunks)

    def get_all(self, *, persona_id: str, store_kind: str) -> list[Any]:
        return list(self.rows.get((persona_id, store_kind), []))


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("memo.md", "memo.md"),
        ("../../conversations/c2/documents/evil.md", "evil.md"),
        ("..\\..\\evil.md", "evil.md"),
        ("/etc/evil.md", "evil.md"),
        ("a/b/c/report final.md", "report final.md"),
        ("bad\x00name.md", "badname.md"),
    ],
)
def test_only_the_last_part_of_a_name_is_kept(given: str, kept: str) -> None:
    assert document_service.upload_name(given) == kept


@pytest.mark.parametrize("given", ["", ".", "..", "a/..", "a/.", "/", "\\"])
def test_a_name_that_is_no_name_is_refused(given: str) -> None:
    assert document_service.upload_name(given) is None


def test_an_upload_records_its_name_without_the_path(tmp_path: Path) -> None:
    ref = document_service.upload(
        file_storage=LocalFileStorage(tmp_path),
        owner_id=_OWNER,
        persona_id=_PERSONA,
        conversation_id=_CONV,
        file_bytes=b"hello",
        filename="../../conversations/c2/documents/evil.md",
        document_store=DocumentStore(backend=_Backend()),  # type: ignore[arg-type]
    )
    assert ref.filename == "evil.md"


def _legacy_document(workspace: Path, filename: str) -> None:
    """A document recorded before this fix, its stored name still carrying a path."""
    base = workspace / _OWNER / _PERSONA / "conversations" / _CONV / DOCUMENT_DIR_NAME
    base.mkdir(parents=True, exist_ok=True)
    (base / "doc1.md").write_bytes(b"# body\n")
    ref = DocumentRef(
        doc_ref="doc1",
        filename=filename,
        title="t",
        format="md",
        workspace_path=f"{_OWNER}/{_PERSONA}/conversations/{_CONV}/{DOCUMENT_DIR_NAME}/doc1.md",
        strategy=IngestStrategy.WHOLE_INJECT,
        token_count=1,
        size_bytes=7,
    )
    (base / "doc1.md.meta.json").write_text(ref.model_dump_json())


def test_a_stored_name_with_a_path_is_staged_by_its_last_part(tmp_path: Path) -> None:
    _legacy_document(tmp_path, "../../conversations/c2/documents/x.md")
    staged = _resolve_turn_documents(
        file_storage=LocalFileStorage(tmp_path),
        owner_id=_OWNER,
        persona_id=_PERSONA,
        conversation_id=_CONV,
    )
    assert [s.path for s in staged] == ["uploads/x.md"]


@pytest.mark.parametrize("filename", ["..", "."])
def test_a_stored_name_that_is_no_name_is_not_staged(tmp_path: Path, filename: str) -> None:
    _legacy_document(tmp_path, filename)
    staged = _resolve_turn_documents(
        file_storage=LocalFileStorage(tmp_path),
        owner_id=_OWNER,
        persona_id=_PERSONA,
        conversation_id=_CONV,
    )
    assert staged == []
