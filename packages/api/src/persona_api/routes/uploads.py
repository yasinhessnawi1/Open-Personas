"""Image upload + fetch routes (spec 13 T11, §5.4).

Thin routes over :mod:`persona_api.services.image_service` (T10a/T10b own all
validation, magic-byte checks, decompression-bomb defence, downscale, and
EXIF strip). The route layer's job is the four standard concerns the
:mod:`personas` / :mod:`conversations` routes already model:

1. **Authentication** — :func:`get_current_user` sets the RLS contextvar
   (D-08-1) so every DB access is structurally tenant-scoped.
2. **Rate limiting** — D-13-X-rate-limit-bucket: reuse the existing
   ``default`` bucket at v0.1 (no new bucket category until upload traffic
   warrants its own knob).
3. **Pre-flight RLS persona check** — mirrors
   :func:`persona_api.routes.conversations.post_message`: a SELECT against
   the RLS-scoped engine returns 0 rows for any cross-tenant persona id,
   which surfaces as :class:`PersonaNotFoundError` (→ 404). This blocks
   cross-tenant uploads at the route boundary before any workspace I/O.
4. **Audit** — every successful upload records ``upload.create`` in the
   API ``audit_log`` (spec-08 §8.2). Reads (``GET``) are NOT audited per
   the spec-08 pattern (only state-changing operations).

Cross-tenant ``GET`` returns ``404`` by design — the image service maps
``PersonaError(reason="not_found")`` to :exc:`PersonaError`, which the
route remaps to :exc:`PersonaNotFoundError` so the existing 404 handler
fires. This is *existence-disclosure-safe*: A user cannot distinguish
"no such ref" from "ref belongs to another tenant" from the response.

T12 owns workspace cascade-delete on persona/conversation deletion — out
of scope here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from persona.documents.errors import (
    CorruptDocumentError,
    MissingDependencyError,
    UnsupportedFormatError,
)
from persona.documents.parsers import SUPPORTED_EXTENSIONS
from persona.errors import PersonaError, PersonaNotFoundError

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.middleware.rate_limit import rate_limit
from persona_api.services import audit_service, chat_service, document_service, image_service

router = APIRouter(prefix="/v1/personas", tags=["uploads"])

# Reasons the image service raises that map to 422 (validation failures).
# Anything else (e.g. ``decode_failed``, ``malformed_image``) maps to 422 too —
# the route surface treats all upload-input failures as client validation errors.
# ``not_found`` is the sole 404 path (existence-disclosure-safe).
_NOT_FOUND_REASONS: frozenset[str] = frozenset({"not_found"})


def _ensure_persona_visible(request: Request, persona_id: str) -> None:
    """Pre-flight RLS check: persona must be visible under the caller's scope.

    Mirrors the per-endpoint RLS pattern used by
    :func:`persona_api.routes.conversations.post_message`. The RLS-scoped
    engine returns 0 rows for any cross-tenant persona id, which surfaces
    as :class:`PersonaNotFoundError` (→ 404 via the existing handler).
    This blocks cross-tenant uploads before any workspace I/O occurs.
    """
    from persona_api.services import persona_service

    # Raises PersonaNotFoundError → 404 if the persona is not the caller's.
    persona_service.get_persona(rls_engine=request.app.state.rls_engine, persona_id=persona_id)


def _remap_image_error(exc: PersonaError) -> Exception:
    """Translate an image-service :class:`PersonaError` into the right HTTP shape.

    The image service raises bare :class:`PersonaError` with a ``reason``
    in ``context`` (e.g. ``unsupported_media_type``, ``oversize``,
    ``magic_bytes_mismatch``, ``malformed_header``, ``decompression_bomb``,
    ``image_too_large``, ``malformed_image``, ``decode_failed``,
    ``not_found``). The two cases the route handles distinctly are:

    * ``not_found`` → :class:`PersonaNotFoundError` (→ 404 via the existing
      handler, existence-disclosure-safe; cross-tenant fetch lands here).
    * Everything else → :class:`HTTPException` (422) with a structured
      body matching the rest of the API's validation-error shape. We don't
      add a new domain exception per the spec brief; ``HTTPException``
      bypasses the generic ``_domain_500`` handler.
    """
    reason = exc.context.get("reason", "")
    if reason in _NOT_FOUND_REASONS:
        not_found: Exception = PersonaNotFoundError(exc.message or "not found", context=exc.context)
        return not_found
    payload: dict[str, object] = {
        "error": "image_validation_error",
        "detail": exc.message or "image validation failed",
    }
    if exc.context:
        payload["context"] = dict(exc.context)
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail=payload,
    )


def _is_document_filename(filename: str) -> bool:
    """Whether the filename's extension is in
    :data:`persona.documents.parsers.SUPPORTED_EXTENSIONS`.

    Falls back on the extension when ``Content-Type`` is missing or
    ambiguous (common from non-browser clients). The dispatcher only
    needs to distinguish *image* from *document* — Spec 13's image route
    already owns image validation, and Spec 14's
    :func:`persona.documents.parsers.parse_document` owns document
    validation. Anything that's neither lands in 415.
    """
    if not filename:
        return False
    return Path(filename).suffix.lower() in SUPPORTED_EXTENSIONS


@router.post(
    "/{persona_id}/uploads",
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("default"))],
)
async def create_upload(
    persona_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    file: UploadFile = File(...),
    conversation_id: str | None = Form(None),
) -> dict[str, Any]:
    """Validate + store an upload under the caller's persona.

    Dispatches by content-type per CSA-2 + D-14-X-uploads-coordination:

    - ``image/*`` (PNG / JPEG / WebP / GIF) → :func:`image_service.upload`
      (Spec 13). Returns ``{"workspace_path", "media_type", "size_bytes"}``.
    - **Document MIME types** (PDF / DOCX / XLSX / CSV / TXT / MD / code)
      → :func:`document_service.upload` (Spec 14). Requires a
      ``conversation_id`` form field (documents are conversation-scoped per
      Dominant Concern #1; the conversation existence + ownership are
      verified via :func:`chat_service.get_conversation`). Returns the
      :class:`document_service.DocumentRef` as JSON.
    - Anything else → 415 Unsupported Media Type.

    Cross-tenant persona id → 404 (persona pre-flight); cross-tenant
    conversation_id → 404 (chat_service.get_conversation under RLS).
    Validation errors → 422 with structured body. Scanned PDFs raise
    :exc:`VisionHandoffRequiredError` → 422 ``"vision_handoff_required"``
    (T13 / T21 interim contract — Spec 13 fail-loud at Spec 14's interim
    state).
    """
    _ensure_persona_visible(request, persona_id)

    file_bytes = await file.read()
    declared_media_type = file.content_type or ""
    filename = file.filename or ""

    # Content-type dispatch — image first (Spec 13's shipped surface),
    # then document (Spec 14 extension), then 415.
    if declared_media_type.startswith("image/"):
        return _handle_image_upload(
            request=request,
            user=user,
            persona_id=persona_id,
            file_bytes=file_bytes,
            declared_media_type=declared_media_type,
            conversation_id=conversation_id,
            filename=filename or None,
        )

    if _is_document_filename(filename) or declared_media_type == "application/pdf":
        if conversation_id is None or not conversation_id.strip():
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "error": "conversation_id_required",
                    "detail": (
                        "Document uploads must carry a 'conversation_id' form "
                        "field (documents are conversation-scoped per Spec 14 §4)."
                    ),
                },
            )
        return _handle_document_upload(
            request=request,
            user=user,
            persona_id=persona_id,
            conversation_id=conversation_id,
            file_bytes=file_bytes,
            filename=filename,
        )

    raise HTTPException(
        status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
        detail={
            "error": "unsupported_media_type",
            "detail": f"Unsupported upload format: {declared_media_type or filename!r}",
        },
    )


def _handle_image_upload(
    *,
    request: Request,
    user: AuthenticatedUser,
    persona_id: str,
    file_bytes: bytes,
    declared_media_type: str,
    conversation_id: str | None = None,
    filename: str | None = None,
) -> dict[str, Any]:
    """The Spec 13 image path. Behaviour unchanged from before T17.

    Spec F5 T04 — forwards ``conversation_id`` and ``filename`` so
    ``image_service.upload`` can write the F5 sidecar
    (``WorkspaceArtifactMetadata``) alongside the bytes for the
    artifact-list endpoint (D-F5-X-artifact-metadata-convention).
    """
    try:
        ref = image_service.upload(
            file_storage=request.app.state.file_storage,
            owner_id=user.id,
            persona_id=persona_id,
            file_bytes=file_bytes,
            declared_media_type=declared_media_type,
            conversation_id=conversation_id,
            original_name=filename,
        )
    except PersonaError as exc:
        raise _remap_image_error(exc) from exc

    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="upload.create",
        target=persona_id,
        metadata={
            "workspace_path": ref.workspace_path,
            "media_type": ref.media_type,
            "size_bytes": str(ref.size_bytes),
        },
    )

    return {
        "workspace_path": ref.workspace_path,
        "media_type": ref.media_type,
        "size_bytes": ref.size_bytes,
    }


def _handle_document_upload(
    *,
    request: Request,
    user: AuthenticatedUser,
    persona_id: str,
    conversation_id: str,
    file_bytes: bytes,
    filename: str,
) -> dict[str, Any]:
    """The Spec 14 document path. Calls :func:`document_service.upload`.

    Conversation ownership is verified via
    :func:`chat_service.get_conversation` (404 if not the caller's). Then
    the document service runs the parse → ingest pipeline and returns a
    :class:`DocumentRef`.

    The interim ``VisionHandoffRequiredError`` → 422
    ``"vision_handoff_required"`` translation lives here. **TODO(T21):**
    remove the catch + the exception class when the scanned-PDF → vision
    handoff is wired.
    """
    # 404 on cross-tenant conversation_id (RLS-scoped via chat_service).
    chat_service.get_conversation(
        rls_engine=request.app.state.rls_engine,
        conversation_id=conversation_id,
    )

    builder = getattr(request.app.state, "build_document_store", None)
    if builder is None:
        msg = (
            "DocumentStore builder not wired in app.state — composition root "
            "needs to set app.state.build_document_store"
        )
        raise RuntimeError(msg)

    try:
        ref = document_service.upload(
            file_storage=request.app.state.file_storage,
            owner_id=user.id,
            persona_id=persona_id,
            conversation_id=conversation_id,
            file_bytes=file_bytes,
            filename=filename,
            document_store=builder(),
        )
    except UnsupportedFormatError as exc:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail={
                "error": "unsupported_format",
                "detail": exc.message or "unsupported document format",
                "context": dict(exc.context),
            },
        ) from exc
    except CorruptDocumentError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "error": "corrupt_document",
                "detail": exc.message or "document is corrupt or unreadable",
                "context": dict(exc.context),
            },
        ) from exc
    except MissingDependencyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "missing_dependency",
                "detail": exc.message or "parser library not installed",
                "context": dict(exc.context),
            },
        ) from exc
    # T21 — VisionHandoffRequiredError is GONE; scanned PDFs succeed via
    # rasterisation + ImageContent (Spec 13 D-13-X-pdf-contract). The
    # TODO(T21) catch-block + the interim exception class have been removed
    # per the close-out discipline. The returned DocumentRef carries the
    # rasterised page images for the runtime's vision-tier routing.

    audit_service.record(
        engine=request.app.state.rls_engine,
        user_id=user.id,
        action="upload.create",
        target=f"{conversation_id}/{ref.doc_ref}",
        metadata={
            "workspace_path": ref.workspace_path,
            "format": ref.format,
            "strategy": ref.strategy.value,
            "token_count": str(ref.token_count),
        },
    )

    return ref.model_dump(mode="json")


@router.get(
    "/{persona_id}/uploads/{ref:path}",
    dependencies=[Depends(rate_limit("default"))],
)
async def get_upload(
    persona_id: str,
    ref: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> Response:
    """Read an uploaded image by its workspace-relative ref.

    Cross-tenant access returns 404 by design (existence-disclosure-safe).
    Path-traversal attempts (``..``) reject as 404 via the sandbox resolver.
    """
    _ensure_persona_visible(request, persona_id)

    try:
        file_bytes, media_type = image_service.fetch(
            file_storage=request.app.state.file_storage,
            owner_id=user.id,
            persona_id=persona_id,
            ref=ref,
        )
    except PersonaError as exc:
        raise _remap_image_error(exc) from exc

    # Copy rate-limit headers from the stashed decision so the binary response
    # carries the standard X-RateLimit-* surface (same pattern as the SSE
    # response in routes/conversations.py).
    headers: dict[str, str] = {}
    decision = getattr(request.state, "rate_limit_decision", None)
    if decision is not None:
        headers = decision.headers()

    return Response(content=file_bytes, media_type=media_type, headers=headers)


__all__ = ["router"]
