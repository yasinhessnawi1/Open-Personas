"""Conversation-scoped document routes (spec 14 T18).

Two endpoints — both **Spec-14-only** (no Spec-13 file overlap; T17's
upload endpoint is the shared one that lives in ``routes/uploads.py``
per CSA-2 + D-14-X-uploads-coordination):

- ``GET    /v1/conversations/:id/documents`` — list attached documents.
- ``DELETE /v1/conversations/:id/documents/:ref`` — remove one document
  (workspace files + DocumentStore chunks).

Each endpoint first verifies conversation ownership via
:func:`persona_api.services.chat_service.get_conversation` (404 if not
the caller's — RLS-scoped via the per-request engine pool listener,
D-08-1) **before** touching the workspace. The route reads
``app.state.sandbox_root`` for the workspace root and
``app.state.build_document_store`` for the per-request
:class:`~persona.stores.document_store.DocumentStore`.

The conversation-cascade-delete extension (T19, GATED on Spec 13's T12)
reuses :func:`document_service.remove_all_for_conversation`; this route
file is not involved in the cascade path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import APIRouter, Depends, Request, status

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.services import audit_service, chat_service, document_service

if TYPE_CHECKING:
    from collections.abc import Callable

    from persona.stores.document_store import DocumentStore

    from persona_api.services.document_service import DocumentRef


router = APIRouter(prefix="/v1", tags=["documents"])


def _build_document_store(request: Request) -> DocumentStore:
    """Per-request :class:`DocumentStore` from the composition root.

    Tests override ``app.state.build_document_store`` with an in-memory
    fake. The composition root (post-T13 wiring) provides the production
    builder.
    """
    builder: Callable[[], DocumentStore] | None = getattr(
        request.app.state, "build_document_store", None
    )
    if builder is None:
        msg = (
            "DocumentStore builder not wired in app.state — composition root "
            "needs to set app.state.build_document_store"
        )
        raise RuntimeError(msg)
    return builder()


@router.get(
    "/conversations/{conversation_id}/documents",
    response_model=list[document_service.DocumentRef],
)
async def list_documents(
    conversation_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> list[DocumentRef]:
    """List documents attached to a conversation (RLS-scoped; 404 if not the caller's)."""
    # Verify conversation ownership BEFORE touching the workspace. RLS hides
    # cross-tenant conversations; chat_service.get_conversation raises
    # ConversationNotFoundError (translated to 404 by the API error handler).
    conv = chat_service.get_conversation(
        rls_engine=request.app.state.rls_engine,
        conversation_id=conversation_id,
    )
    persona_id = str(conv["persona_id"])
    return document_service.list_for_conversation(
        file_storage=request.app.state.file_storage,
        owner_id=user.id,
        persona_id=persona_id,
        conversation_id=conversation_id,
    )


@router.delete(
    "/conversations/{conversation_id}/documents/{doc_ref}",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_document(
    conversation_id: str,
    doc_ref: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Remove a document from a conversation (workspace files + chunks).

    Idempotent — removing a non-existent ``doc_ref`` is a no-op (204).
    """
    conv = chat_service.get_conversation(
        rls_engine=request.app.state.rls_engine,
        conversation_id=conversation_id,
    )
    persona_id = str(conv["persona_id"])
    document_service.remove_document(
        file_storage=request.app.state.file_storage,
        owner_id=user.id,
        persona_id=persona_id,
        conversation_id=conversation_id,
        doc_ref=doc_ref,
        document_store=_build_document_store(request),
    )
    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="document.delete",
        target=f"{conversation_id}/{doc_ref}",
    )
