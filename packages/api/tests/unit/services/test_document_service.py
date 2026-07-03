"""Unit tests for ``persona_api.services.document_service`` (spec 14 T13).

Covers the workspace+sidecar layout, the upload → parse → ingest pipeline,
the vision-handoff-required 422 path (no orphans), per-document removal,
the cascade-helper for T19, and the criterion-#11 workspace-layout
assertion (Refinement #3: workspace path matches resolve_sandbox_path
semantics; Spec 12 runtime NOT required).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.documents.errors import (
    CorruptDocumentError,
    UnsupportedFormatError,
)
from persona.documents.ingest import IngestStrategy
from persona.stores.document_store import DocumentStore
from persona_api.services.document_service import (
    DOCUMENT_DIR_NAME,
    DocumentRef,
    get_document_text,
    list_for_conversation,
    remove_all_for_conversation,
    remove_document,
    upload,
)
from persona_api.storage import LocalFileStorage

if TYPE_CHECKING:
    from persona.schema.chunks import PersonaChunk

FIXTURE_DIR = (
    Path(__file__).resolve().parents[3] / ".." / "core" / "tests" / "fixtures" / "documents"
)
FIXTURE_DIR = FIXTURE_DIR.resolve()


class _InMemoryBackend:
    def __init__(self) -> None:
        self.store: dict[tuple[str, str], list[PersonaChunk]] = {}

    def upsert(
        self,
        *,
        persona_id: str,
        store_kind: str,
        chunks: list[PersonaChunk],
    ) -> None:
        key = (persona_id, store_kind)
        existing = self.store.setdefault(key, [])
        existing_ids = {c.id for c in chunks}
        kept = [c for c in existing if c.id not in existing_ids]
        kept.extend(chunks)
        self.store[key] = kept

    def query(
        self,
        *,
        persona_id: str,
        store_kind: str,
        text: str,  # noqa: ARG002
        top_k: int,
        where: dict[str, Any] | None = None,  # noqa: ARG002
    ) -> list[PersonaChunk]:
        return list(self.store.get((persona_id, store_kind), []))[:top_k]

    def get_all(self, *, persona_id: str, store_kind: str) -> list[PersonaChunk]:
        return list(self.store.get((persona_id, store_kind), []))

    def delete_persona(self, persona_id: str, store_kind: str) -> None:
        self.store.pop((persona_id, store_kind), None)

    def delete_documents(self, *, persona_id: str, store_kind: str, ids: list[str]) -> None:
        key = (persona_id, store_kind)
        self.store[key] = [c for c in self.store.get(key, []) if c.id not in set(ids)]


@pytest.fixture
def document_store() -> DocumentStore:
    return DocumentStore(backend=_InMemoryBackend())  # type: ignore[arg-type]


@pytest.fixture
def sandbox_root(tmp_path: Path) -> Path:
    root = tmp_path / "workspace"
    root.mkdir()
    return root


class TestUploadSmallDoc:
    def test_returns_whole_inject_ref(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        text = b"The lease is for twelve months."
        ref = upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=text,
            filename="memo.txt",
            document_store=document_store,
        )
        assert ref.strategy == IngestStrategy.WHOLE_INJECT
        assert ref.format == "txt"
        assert ref.token_count > 0

    def test_original_bytes_in_workspace_at_resolve_sandbox_path_location(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        # Refinement #3 — criterion #11 workspace-layout assertion. The
        # original bytes land at the path Spec 12's sandbox WOULD resolve
        # under resolve_sandbox_path semantics. Spec 12 runtime NOT
        # required to pass.
        ref = upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=b"hello",
            filename="memo.txt",
            document_store=document_store,
        )
        absolute = sandbox_root / ref.workspace_path
        assert absolute.exists()
        assert absolute.read_bytes() == b"hello"

    def test_workspace_path_is_relative_under_sandbox_root(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        ref = upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=b"hi",
            filename="memo.txt",
            document_store=document_store,
        )
        # The DocumentRef carries a RELATIVE path (relative to sandbox_root),
        # not an absolute one — the API never leaks workspace absolute paths.
        assert not Path(ref.workspace_path).is_absolute()
        # Spec 35: owner-scoped structure {owner}/{persona}/conversations/{conv}/documents/...
        assert ref.workspace_path.startswith("owner/astrid/")
        assert "conv1" in ref.workspace_path
        assert DOCUMENT_DIR_NAME in ref.workspace_path

    def test_sidecar_written_for_listing(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=b"hi",
            filename="memo.txt",
            document_store=document_store,
        )
        refs = list_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
        )
        assert len(refs) == 1
        assert refs[0].format == "txt"

    def test_upload_registers_conversation_artifact(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        # Spec 35: the upload ALSO writes an F5 artifact sidecar (source=
        # "upload", scoped to the conversation) so it surfaces in the unified
        # Files viewer alongside persona-generated artifacts.
        from persona_api.services.artifact_metadata import read_artifact_sidecar

        ref = upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=b"hi",
            filename="memo.txt",
            document_store=document_store,
        )
        meta = read_artifact_sidecar(sandbox_root / ref.workspace_path)
        assert meta is not None
        assert meta.source == "upload"
        assert meta.conversation_id == "conv1"
        assert meta.original_name == "memo.txt"
        assert meta.producing_spec == "14"

    def test_doc_ref_is_url_safe_and_no_delimiter(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        # ``::`` would break the chunk-ID format; spaces / unicode get
        # slug-stripped.
        ref = upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=b"hi",
            filename="My Tenancy Memo (Final).txt",
            document_store=document_store,
        )
        assert "::" not in ref.doc_ref
        assert " " not in ref.doc_ref


class TestUploadLargeDoc:
    def test_returns_retrieval_ref_and_writes_chunks(
        self, sandbox_root: Path, document_store: DocumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force a low threshold so a small fixture triggers retrieval.
        monkeypatch.setenv("PERSONA_DOC_INJECT_THRESHOLD", "100")
        text = ("Paragraph content. " * 200).encode("utf-8")
        ref = upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=text,
            filename="report.txt",
            document_store=document_store,
        )
        assert ref.strategy == IngestStrategy.RETRIEVAL
        # Chunks landed in the store under the conversation scope.
        chunks = document_store.get_all("conv1")
        assert len(chunks) >= 1


class TestUploadUnsupportedFormat:
    def test_unsupported_extension_raises_and_does_not_write(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        with pytest.raises(UnsupportedFormatError):
            upload(
                file_storage=LocalFileStorage(sandbox_root),
                owner_id="owner",
                persona_id="astrid",
                conversation_id="conv1",
                file_bytes=b"junk",
                filename="archive.rar",
                document_store=document_store,
            )
        # Nothing landed in the workspace.
        refs = list_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
        )
        assert refs == []


class TestUploadCorruptFile:
    def test_corrupt_file_raises_and_cleans_workspace(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        with pytest.raises(CorruptDocumentError):
            upload(
                file_storage=LocalFileStorage(sandbox_root),
                owner_id="owner",
                persona_id="astrid",
                conversation_id="conv1",
                file_bytes=b"   \n\n  \n",  # empty extraction
                filename="memo.txt",
                document_store=document_store,
            )
        # The orphan workspace file from the failed parse was cleaned up.
        refs = list_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
        )
        assert refs == []


class TestVisionHandoffPath:
    """T21 — scanned PDFs succeed via rasterisation + ImageContent.

    The T13 interim contract (raise VisionHandoffRequiredError → 422) is
    GONE. T21's wiring: rasterise each page via pypdfium2 + Pillow, persist
    page PNGs under the workspace, return a DocumentRef carrying
    ImageContent references for the runtime's vision-tier routing
    (Spec 13 D-13-X-pdf-contract).
    """

    def test_scanned_pdf_returns_vision_handoff_strategy(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        scanned = FIXTURE_DIR / "scanned-like.pdf"
        ref = upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=scanned.read_bytes(),
            filename="scan.pdf",
            document_store=document_store,
        )
        assert ref.strategy == IngestStrategy.VISION_HANDOFF
        assert ref.format == "pdf"

    def test_scanned_pdf_populates_image_content(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        scanned = FIXTURE_DIR / "scanned-like.pdf"
        ref = upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=scanned.read_bytes(),
            filename="scan.pdf",
            document_store=document_store,
        )
        # The fixture has 3 pages.
        assert len(ref.images) == 3
        for image in ref.images:
            assert image.media_type == "image/png"
            assert image.type == "image"
            # The workspace_path follows the Spec 03 sandbox layout.
            assert "documents/" in image.workspace_path
            assert image.workspace_path.endswith(".png")

    def test_rasterised_pages_persisted_to_workspace(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        scanned = FIXTURE_DIR / "scanned-like.pdf"
        ref = upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=scanned.read_bytes(),
            filename="scan.pdf",
            document_store=document_store,
        )
        for image in ref.images:
            absolute = sandbox_root / image.workspace_path
            assert absolute.exists(), f"rasterised page missing at {absolute}"
            # PNG magic bytes.
            assert absolute.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_scanned_pdf_listed_in_attached_documents(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        scanned = FIXTURE_DIR / "scanned-like.pdf"
        upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=scanned.read_bytes(),
            filename="scan.pdf",
            document_store=document_store,
        )
        refs = list_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
        )
        assert len(refs) == 1
        assert refs[0].strategy == IngestStrategy.VISION_HANDOFF


class TestListForConversation:
    def test_empty_when_no_uploads(self, sandbox_root: Path) -> None:
        refs = list_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
        )
        assert refs == []

    def test_returns_uploaded_documents(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=b"hi",
            filename="memo.txt",
            document_store=document_store,
        )
        upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=b"hi 2",
            filename="memo2.txt",
            document_store=document_store,
        )
        refs = list_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
        )
        assert len(refs) == 2

    def test_returns_document_ref_type(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=b"hi",
            filename="memo.txt",
            document_store=document_store,
        )
        refs = list_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
        )
        assert all(isinstance(r, DocumentRef) for r in refs)


class TestGetDocumentText:
    def test_returns_full_text_for_small_doc(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        ref = upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=b"The lease runs for twelve months.",
            filename="memo.txt",
            document_store=document_store,
        )
        text = get_document_text(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            doc_ref=ref.doc_ref,
        )
        assert "twelve months" in text

    def test_empty_for_missing_doc(self, sandbox_root: Path) -> None:
        text = get_document_text(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            doc_ref="does-not-exist",
        )
        assert text == ""


class TestRemoveDocument:
    def test_removes_workspace_files(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        ref = upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=b"hi",
            filename="memo.txt",
            document_store=document_store,
        )
        remove_document(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            doc_ref=ref.doc_ref,
            document_store=document_store,
        )
        refs = list_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
        )
        assert refs == []

    def test_removes_document_store_chunks(
        self, sandbox_root: Path, document_store: DocumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PERSONA_DOC_INJECT_THRESHOLD", "100")
        ref = upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=("Paragraph. " * 200).encode("utf-8"),
            filename="report.txt",
            document_store=document_store,
        )
        assert len(document_store.get_all("conv1")) >= 1
        remove_document(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            doc_ref=ref.doc_ref,
            document_store=document_store,
        )
        assert document_store.get_all("conv1") == []

    def test_idempotent_on_unknown_ref(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        # Removing a non-existent document is a no-op (no error).
        remove_document(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            doc_ref="not-here",
            document_store=document_store,
        )


class TestRemoveAllForConversation:
    """T19's cascade-helper — used when a conversation is deleted."""

    def test_clears_all_workspace_files(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=b"a",
            filename="a.txt",
            document_store=document_store,
        )
        upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=b"b",
            filename="b.txt",
            document_store=document_store,
        )
        remove_all_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            document_store=document_store,
        )
        refs = list_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
        )
        assert refs == []

    def test_clears_all_document_store_chunks(
        self, sandbox_root: Path, document_store: DocumentStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PERSONA_DOC_INJECT_THRESHOLD", "100")
        upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            file_bytes=("Paragraph. " * 200).encode("utf-8"),
            filename="big.txt",
            document_store=document_store,
        )
        remove_all_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="conv1",
            document_store=document_store,
        )
        assert document_store.get_all("conv1") == []

    def test_idempotent_on_empty_conversation(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        remove_all_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="never-existed",
            document_store=document_store,
        )

    def test_does_not_touch_other_conversations(
        self, sandbox_root: Path, document_store: DocumentStore
    ) -> None:
        upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="convA",
            file_bytes=b"a",
            filename="a.txt",
            document_store=document_store,
        )
        upload(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="convB",
            file_bytes=b"b",
            filename="b.txt",
            document_store=document_store,
        )
        remove_all_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="convA",
            document_store=document_store,
        )
        # convB still has its document.
        refs_b = list_for_conversation(
            file_storage=LocalFileStorage(sandbox_root),
            owner_id="owner",
            persona_id="astrid",
            conversation_id="convB",
        )
        assert len(refs_b) == 1


class TestCsa2DispatcherCompatibility:
    """Architectural contract: T17's content-type dispatcher (gated on
    Spec 13's T11) calls this service for document MIME types. The
    signature is workspace-scoped and conversation-scoped — naturally
    different from Spec 13's persona-scoped image_service.upload — but
    both fit a content-type dispatcher that branches on mime."""

    def test_upload_signature_has_minimal_dispatch_inputs(self) -> None:
        # Documents the contract: ``(file_storage, persona_id,
        # conversation_id, file_bytes, filename, document_store)`` is the
        # CSA-2-compliant call shape. T17 builds this from the request +
        # injected dependencies.
        import inspect

        sig = inspect.signature(upload)
        expected = {
            "file_storage",
            "owner_id",
            "persona_id",
            "conversation_id",
            "file_bytes",
            "filename",
            "document_store",
        }
        assert set(sig.parameters) == expected
