"""Test helper: run an upload the way the route does (validate, parse through the port, store).

The route parses through the :class:`DocumentParser` port and then hands the result to
:func:`document_service.upload`. Tests that exercise the service directly use this so
they keep calling one function with the old arguments while the real parse still runs.

An upload no longer embeds anything (D-FV0-31): a retrieval document is stored ``pending``
and the background index writes its chunks. Given a ``document_store``, this helper runs
that index to the end, in this thread, the way the job would, slice by slice, and returns
the document as a listing then reads it. ``index=False`` returns it as the upload left it.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from persona.documents.chunker import ChunkCursor
from persona_api.document_worker.untrusted import parse_untrusted_document
from persona_api.services import document_service
from persona_api.services.document_index import (
    SLICE_CHUNKS,
    DocumentIndexDeps,
    DocumentIndexer,
    InMemoryIndexQuota,
    run_slice,
)


def upload(*, index: bool = True, **kwargs: Any) -> document_service.DocumentRef:  # noqa: ANN401
    """Validate, parse inline and store; the same keyword arguments as before the port."""
    store = kwargs.pop("document_store", None)
    extension = document_service.validate_upload(
        file_bytes=kwargs["file_bytes"], filename=kwargs["filename"]
    )
    with tempfile.TemporaryDirectory(prefix="persona-test-upload-") as scratch:
        source = Path(scratch) / f"upload{extension}"
        source.write_bytes(kwargs["file_bytes"])
        render_dir = Path(scratch) / "pages"
        render_dir.mkdir()
        parsed = parse_untrusted_document(source, render_dir=render_dir)
        ref = document_service.upload(**kwargs, parsed=parsed)
    if not index or store is None or ref.index_state != "pending":
        return ref
    index_document(
        file_storage=kwargs["file_storage"],
        owner_id=kwargs["owner_id"],
        persona_id=kwargs["persona_id"],
        conversation_id=kwargs["conversation_id"],
        doc_ref=ref.doc_ref,
        document_store=store,
    )
    listed = document_service.list_for_conversation(
        file_storage=kwargs["file_storage"],
        owner_id=kwargs["owner_id"],
        persona_id=kwargs["persona_id"],
        conversation_id=kwargs["conversation_id"],
    )
    return next(r for r in listed if r.doc_ref == ref.doc_ref)


def index_document(
    *,
    file_storage: Any,  # noqa: ANN401
    owner_id: str,
    persona_id: str,
    conversation_id: str,
    doc_ref: str,
    document_store: Any,  # noqa: ANN401
) -> int:
    """Run every slice of one document's index in this thread; return the slice count."""
    deps = DocumentIndexDeps(
        file_storage=file_storage,
        document_store=lambda: document_store,
        indexer=DocumentIndexer(),
        quota=InMemoryIndexQuota(),
    )
    cursor = ChunkCursor()
    slices = 0
    while True:
        outcome = run_slice(
            deps=deps,
            owner_id=owner_id,
            persona_id=persona_id,
            conversation_id=conversation_id,
            doc_ref=doc_ref,
            cursor=cursor,
            limit=SLICE_CHUNKS,
        )
        slices += 1
        if outcome.state != "indexing":
            return slices
        cursor = outcome.cursor
