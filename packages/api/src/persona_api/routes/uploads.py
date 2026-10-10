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

import asyncio
import functools
import re
import shutil
import tempfile
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import Response
from fastapi.routing import APIRoute
from persona.documents.errors import (
    CorruptDocumentError,
    MissingDependencyError,
    UnsupportedFormatError,
)
from persona.documents.parsers import SUPPORTED_EXTENSIONS
from persona.errors import PersonaError, PersonaNotFoundError
from persona.logging import get_logger

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.auth.deps import get_verify_token
from persona_api.document_worker.admission import (
    DocumentAdmission,
    DocumentParseBudgetError,
    DocumentParseCooldownError,
    DocumentUploadInProgressError,
    DocumentUploadsArrivingError,
    UploadLease,
)
from persona_api.document_worker.pool import (
    DocumentParserBusyError,
    DocumentParserUnavailableError,
)
from persona_api.middleware.rate_limit import RateLimiter, rate_limit
from persona_api.services import (
    audit_service,
    chat_service,
    document_service,
    image_service,
    task_attachment_service,
)
from persona_api.services.document_index import DocumentIndexBacklogFullError
from persona_api.services.document_parsing import ParsedDocument

if TYPE_CHECKING:
    from collections.abc import Callable

#: Room for the multipart framing around the file: boundaries, part headers and the
#: two small form fields. Generous; a real request uses well under a kilobyte.
_MULTIPART_OVERHEAD_BYTES: int = 64 * 1024

#: The most bytes an upload request body may carry (D-FV0-08, review item 6).
MAX_UPLOAD_BODY_BYTES: int = image_service.MAX_UPLOAD_BYTES + _MULTIPART_OVERHEAD_BYTES

#: The longest an upload's body may take to arrive (second security review F7). A
#: client trickling a byte at a time otherwise holds a connection and a request slot
#: for as long as it likes. Sixty seconds carries the full 20 MiB at about 2.8 Mbit/s;
#: a slower link can still send a smaller file.
MAX_UPLOAD_BODY_SECONDS: float = 60.0

#: The rate-limit bucket uploads count against, checked before the body is read.
_UPLOAD_RATE_LIMIT_KEY = "default"


def _too_slow() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_408_REQUEST_TIMEOUT,
        detail={
            "error": "upload_too_slow",
            "detail": "the upload took too long to arrive",
            "context": {"max_seconds": str(int(MAX_UPLOAD_BODY_SECONDS))},
        },
    )


def _too_large(received: int | None) -> HTTPException:
    context = {"max_bytes": str(MAX_UPLOAD_BODY_BYTES)}
    if received is not None:
        context["received_bytes"] = str(received)
    return HTTPException(
        status_code=status.HTTP_413_CONTENT_TOO_LARGE,
        detail={
            "error": "upload_too_large",
            "detail": "the upload is larger than the server accepts",
            "context": context,
        },
    )


class _BoundedUploadRequest(Request):
    """A request whose multipart form allows one file and a few small fields.

    Starlette's defaults spool up to 1,000 files and 1,000 fields per request to disk
    before the handler runs. FastAPI calls ``form()`` with no arguments, so the bounds
    are the defaults of this override.
    """

    def form(
        self,
        *,
        max_files: int | float = 1,
        max_fields: int | float = 8,
        max_part_size: int = 64 * 1024,
    ) -> Any:  # noqa: ANN401 (Starlette's awaitable-or-context-manager wrapper)
        return super().form(max_files=max_files, max_fields=max_fields, max_part_size=max_part_size)


def _counting_receive(
    receive: Any,  # noqa: ANN401 (the ASGI receive callable)
    on_body_done: Callable[[], None] | None = None,
) -> Any:  # noqa: ANN401 (the ASGI receive callable)
    """Wrap ``receive`` so the body stops being read past its byte and time limits.

    :data:`MAX_UPLOAD_BODY_BYTES` bounds a chunked body, or one whose ``Content-Length``
    lies: the count is of bytes actually received, whatever the headers said.
    :data:`MAX_UPLOAD_BODY_SECONDS` bounds how long the whole body may take, from the
    first read, however slowly it trickles; a read that would wait past it is cut off.
    Once the body is complete, ``on_body_done`` runs, and later reads (a disconnect
    check) pass straight through.
    """
    received = 0
    deadline: float | None = None
    body_done = False

    async def counted() -> Any:  # noqa: ANN401 (an ASGI message)
        nonlocal received, deadline, body_done
        if body_done:
            return await receive()
        loop = asyncio.get_running_loop()
        if deadline is None:
            deadline = loop.time() + MAX_UPLOAD_BODY_SECONDS
        remaining = deadline - loop.time()
        if remaining <= 0:
            raise _too_slow()
        try:
            message = await asyncio.wait_for(receive(), remaining)
        except TimeoutError:
            raise _too_slow() from None
        if message.get("type") == "http.request":
            received += len(message.get("body", b""))
            if received > MAX_UPLOAD_BODY_BYTES:
                raise _too_large(received)
            if not message.get("more_body", False):
                body_done = True
                if on_body_done is not None:
                    on_body_done()
        return message

    return counted


async def _authenticate_before_body(request: Request) -> AuthenticatedUser:
    """Resolve the caller the way :func:`get_current_user` does, before the body is read.

    Raises the same authentication error the dependency would (401). The dependency
    still runs for the handler and binds the RLS owner as before; this only moves the
    refusal ahead of the body.
    """
    resolver = request.app.state.owner_resolver
    user: AuthenticatedUser = await resolver.resolve(request, get_verify_token(request))
    return user


def _rate_limit_before_body(request: Request, user: AuthenticatedUser) -> None:
    """Count the upload against the caller's rate limit before the body is read.

    Third security review finding 5: the limit was a dependency, and FastAPI reads the
    form before it solves dependencies, so a refused upload had already been received.
    The decision is left on the request, and the route's own rate-limit dependency
    uses it instead of counting the request a second time.
    """
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        return
    decision = limiter.check(user.id, _UPLOAD_RATE_LIMIT_KEY)
    request.state.rate_limit_decision = decision
    request.state.rate_limit_counted = (user.id, _UPLOAD_RATE_LIMIT_KEY)


def _too_many_bodies(exc: DocumentUploadsArrivingError) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail={
            "error": "uploads_arriving",
            "detail": exc.message or "other uploads of yours are still arriving",
            "context": dict(exc.context),
        },
        headers={"Retry-After": exc.context.get("retry_after_seconds", "5")},
    )


class _BoundedUploadRoute(APIRoute):
    """Bounds every upload request body before Starlette spools any of it.

    Before the body is read: the caller is authenticated, counted against the rate
    limit, and given one of their places for a body arriving (``MAX_BODIES_PER_OWNER``,
    else 429), and a declared ``Content-Length`` over the limit is refused. The body
    itself is counted as it arrives; the form allows one file.
    """

    def get_route_handler(self) -> Any:  # noqa: ANN401 (Starlette's handler type)
        original = super().get_route_handler()

        async def bounded(request: Request) -> Response:
            if request.method == "POST":
                # F7 (second review): who is asking is settled before a byte of the body
                # is read, so an unauthenticated client cannot make the api receive and
                # spool a 20 MiB body only to be refused afterwards.
                user = await _authenticate_before_body(request)
                _rate_limit_before_body(request, user)
                declared = request.headers.get("content-length")
                if declared is not None:
                    try:
                        declared_bytes = int(declared)
                    except ValueError as exc:
                        raise HTTPException(
                            status_code=status.HTTP_400_BAD_REQUEST,
                            detail={"error": "bad_content_length", "detail": "invalid length"},
                        ) from exc
                    if declared_bytes > MAX_UPLOAD_BODY_BYTES:
                        raise _too_large(declared_bytes)
                admission: DocumentAdmission | None = getattr(
                    request.app.state, "document_admission", None
                )
                try:
                    body = admission.begin_body(user.id) if admission is not None else None
                except DocumentUploadsArrivingError as exc:
                    raise _too_many_bodies(exc) from exc
                try:
                    receive = _counting_receive(
                        request.receive, body.release if body is not None else None
                    )
                    return await original(_BoundedUploadRequest(request.scope, receive))
                finally:
                    if body is not None:
                        body.release()
            return await original(request)

        return bounded


_log = get_logger("api.uploads")

router = APIRouter(prefix="/v1/personas", tags=["uploads"], route_class=_BoundedUploadRoute)

#: The most bytes the upload route reads from the request body (D-FV0-08). One byte more
#: than the largest per-branch cap, so every branch still sees an oversized file as
#: oversized and refuses it, while a multi-gigabyte body is never pulled into memory
#: whole. Every branch's cap is the same constant today; a test pins that, because a
#: branch with a larger cap would be silently truncated by this read.
_MAX_UPLOAD_READ_BYTES: int = (
    max(
        image_service.MAX_UPLOAD_BYTES,
        document_service.MAX_DOCUMENT_BYTES,
        task_attachment_service.MAX_ATTACHMENT_BYTES,
    )
    + 1
)

#: A content-addressed workspace file name: ``blake2b(bytes, digest_size=16)``
#: rendered as 32 hex characters (services/image_service.py, imagegen/service.py,
#: services/workspace_persister.py all mint refs this way).
_CONTENT_ADDRESSED_STEM = re.compile(r"^[0-9a-f]{32}$")

#: One year, the longest value HTTP caches honour, plus ``immutable`` so a
#: reload does not even send a revalidation request.
_IMMUTABLE_CACHE_CONTROL = "private, max-age=31536000, immutable"


def is_content_addressed_ref(ref: str) -> bool:
    """Whether ``ref`` names a file whose bytes can never change.

    Content-addressed refs are named after a hash of their own bytes, so a new
    portrait (an avatar regeneration, a re-upload) is a NEW ref and the old one
    keeps serving exactly the bytes it always served. That is what makes a
    year-long ``immutable`` response safe, and why no cache invalidation exists
    anywhere: there is nothing to invalidate.

    Anything else (a human-named upload, say) gets no caching promise.
    """
    return bool(_CONTENT_ADDRESSED_STEM.match(PurePosixPath(ref).stem))


# Reasons the image service raises that map to 422 (validation failures).
# Anything else (e.g. ``decode_failed``, ``malformed_image``) maps to 422 too —
# the route surface treats all upload-input failures as client validation errors.
# ``not_found`` is the sole 404 path (existence-disclosure-safe).
_NOT_FOUND_REASONS: frozenset[str] = frozenset({"not_found"})


def _download_name(ref: str) -> str:
    """The plain ``filename=`` value to offer for a downloaded artifact.

    Takes the ref's last path segment and keeps only ASCII characters that
    cannot break the quoted parameter (quotes, backslashes, control characters
    and newlines are dropped). ASCII-only is not fussiness: header values are
    encoded latin-1, so a model-chosen name like ``rapport_日本.xlsx`` would
    turn a working download into a 500. The full name still reaches the browser
    through the ``filename*`` parameter the caller adds beside this one.

    Args:
        ref: The workspace-relative ref the caller asked for.

    Returns:
        A quoted-string-safe filename, or ``download`` if nothing survives.
    """
    base = PurePosixPath(ref).name
    cleaned = "".join(c for c in base if c.isascii() and c.isprintable() and c not in '"\\')[:120]
    return cleaned or "download"


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
    dependencies=[Depends(rate_limit(_UPLOAD_RATE_LIMIT_KEY))],
)
async def create_upload(
    persona_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
    file: UploadFile = File(...),
    conversation_id: str | None = Form(None),
    scope: str = Form("conversation"),
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
    Validation errors → 422 with structured body. A scanned PDF is not one of
    them: T21 rasterises its pages and the returned
    :class:`document_service.DocumentRef` carries them as ``ImageContent``
    references for the vision tier (Spec 13's PDF contract).
    """
    _ensure_persona_visible(request, persona_id)

    file_bytes = await file.read(_MAX_UPLOAD_READ_BYTES)
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
        # Issue #16: a task or routine hand-off has no conversation, so its attachment is
        # stored persona-scoped and named on the task contract instead of being ingested
        # into a conversation's document store. Explicit scope, never an inferred one: a
        # chat upload that forgot its conversation_id still gets the 422 below.
        if scope == "task":
            return _handle_task_attachment_upload(
                request=request,
                user=user,
                persona_id=persona_id,
                file_bytes=file_bytes,
                filename=filename,
                declared_media_type=declared_media_type,
            )
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
        return await _handle_document_upload(
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


def _remap_attachment_error(exc: PersonaError) -> Exception:
    """Translate a task-attachment failure into a 422 that says what went wrong.

    The attachment service raises ``unsupported_media_type`` (a format the document
    parsers do not know) or ``oversize``. Same structured body shape as the rest of the
    API, under its own error code so a client never has to read a document failure as an
    image one.
    """
    payload: dict[str, object] = {
        "error": "attachment_validation_error",
        "detail": exc.message or "that file could not be attached",
    }
    if exc.context:
        payload["context"] = dict(exc.context)
    return HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_CONTENT, detail=payload)


def _handle_task_attachment_upload(
    *,
    request: Request,
    user: AuthenticatedUser,
    persona_id: str,
    file_bytes: bytes,
    filename: str,
    declared_media_type: str,
) -> dict[str, Any]:
    """Store a document attached to a task or routine hand-off (issue #16).

    Persona-scoped, no parse, no ingest: the bytes land in the same ``uploads/`` directory
    a chat image does, and the returned ``workspace_path`` is what the task contract
    carries so every leg can open it with ``file_read``.
    """
    try:
        ref = task_attachment_service.upload(
            file_storage=request.app.state.file_storage,
            owner_id=user.id,
            persona_id=persona_id,
            file_bytes=file_bytes,
            filename=filename,
            declared_media_type=declared_media_type,
        )
    except PersonaError as exc:
        raise _remap_attachment_error(exc) from exc

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
        "filename": ref.filename,
        "media_type": ref.media_type,
        "size_bytes": ref.size_bytes,
    }


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


async def _handle_document_upload(
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

    A scanned PDF is not rejected here. T21 wired the real vision handoff, so
    the document service rasterises the pages and the returned
    :class:`DocumentRef` carries them as ``ImageContent`` references (Spec 13's
    PDF contract); the interim exception and the catch that translated it to a
    422 are both gone, as the comment further down records.
    """
    # 404 on cross-tenant conversation_id (RLS-scoped via chat_service).
    chat_service.get_conversation(
        rls_engine=request.app.state.rls_engine,
        conversation_id=conversation_id,
    )

    if getattr(request.app.state, "build_document_store", None) is None:
        msg = (
            "DocumentStore builder not wired in app.state — composition root "
            "needs to set app.state.build_document_store"
        )
        raise RuntimeError(msg)

    parser = request.app.state.document_parser
    admission: DocumentAdmission = request.app.state.document_admission
    lease: UploadLease | None = None
    store_scheduled = False
    try:
        extension = document_service.validate_upload(file_bytes=file_bytes, filename=filename)
        # F2: one upload per owner, held until the worker AND the store are done with it,
        # within the owner's worker-time budget and not during a cooldown.
        lease = admission.begin_upload(user.id)
        # F7 (third review): the scratch directory outlives a cancelled request. It is
        # removed when the lease is last released, that is once neither the worker
        # (still rendering into it) nor the store (reading the page images) can use it.
        scratch = tempfile.mkdtemp(prefix="persona-upload-")
        lease.on_released(functools.partial(shutil.rmtree, scratch, ignore_errors=True))
        # The parser port reads a path, never request state: the bytes go to a
        # private scratch directory, and page images come back inside it.
        source = Path(scratch) / f"upload{extension}"
        render_dir = Path(scratch) / "pages"
        render_dir.mkdir()
        await asyncio.to_thread(source.write_bytes, file_bytes)
        # D-FV0-31: the parse runs in a limited, killable worker process. One upload
        # per user at a time; a busy pool queues briefly, then refuses.
        parsed = await parser.parse(
            source, render_dir=render_dir, owner_id=user.id, purpose="upload", lease=lease
        )
        store_scheduled = True
        # Persisting and auditing are synchronous; they run together in ONE thread
        # call. ``asyncio.to_thread`` copies the context, so the RLS owner contextvar
        # is bound inside it. Recording the audit in the same call means an upload
        # whose client disconnects mid-store is still both stored and audited: the
        # thread finishes what it started, and nothing is stored before the parse has
        # returned. Nothing is embedded here (D-FV0-31): a retrieval document is
        # stored ``pending`` and indexed in the background, scheduled below.
        try:
            ref = await asyncio.to_thread(
                _store_and_audit,
                request=request,
                user=user,
                persona_id=persona_id,
                conversation_id=conversation_id,
                file_bytes=file_bytes,
                filename=filename,
                parsed=parsed,
                lease=lease,
            )
        except asyncio.CancelledError:
            # Gone before the store thread started: it will not store, and the lease
            # is released here. Once started, it stores, audits and releases itself.
            lease.abandon_store()
            raise
    except document_service.DocumentTooLargeError as exc:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail={
                "error": "document_too_large",
                "detail": exc.message or "document exceeds the upload size limit",
                "context": dict(exc.context),
            },
        ) from exc
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
    except DocumentIndexBacklogFullError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "index_backlog_full",
                "detail": exc.message or "too many documents are waiting to be indexed",
                "context": dict(exc.context),
            },
            headers={"Retry-After": exc.context.get("retry_after_seconds", "60")},
        ) from exc
    except DocumentUploadInProgressError as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": "upload_in_progress",
                "detail": exc.message or "an upload of yours is still being read",
                "context": dict(exc.context),
            },
        ) from exc
    except (DocumentParseBudgetError, DocumentParseCooldownError) as exc:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail={
                "error": exc.context.get("reason", "parse_budget_exhausted"),
                "detail": exc.message or "try again later",
                "context": dict(exc.context),
            },
            headers={"Retry-After": exc.context.get("retry_after_seconds", "60")},
        ) from exc
    except DocumentParserBusyError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "parser_busy",
                "detail": exc.message or "the document parser is busy; try again shortly",
                "context": dict(exc.context),
            },
            headers={"Retry-After": exc.context.get("retry_after_seconds", "10")},
        ) from exc
    except DocumentParserUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "error": "parser_unavailable",
                "detail": exc.message or "the document parser is unavailable",
                "context": dict(exc.context),
            },
        ) from exc
    finally:
        if lease is not None and not store_scheduled:
            lease.release()
    assert ref is not None  # noqa: S101 (only an abandoned store returns None)
    if ref.index_state == "pending":
        await _schedule_index(
            request,
            owner_id=user.id,
            persona_id=persona_id,
            conversation_id=conversation_id,
            ref=ref,
        )
    return ref.model_dump(mode="json")


async def _schedule_index(
    request: Request,
    *,
    owner_id: str,
    persona_id: str,
    conversation_id: str,
    ref: document_service.DocumentRef,
) -> None:
    """Start the background index of a retrieval document (D-FV0-31). Never fails the upload.

    A failure here, or a request gone before it, leaves the document ``pending``; the
    next turn or document listing that sees it schedules it again (``pick_up``).
    """
    scheduler = getattr(request.app.state, "document_index_scheduler", None)
    if scheduler is None:
        return
    try:
        await scheduler.schedule(
            owner_id=owner_id,
            persona_id=persona_id,
            conversation_id=conversation_id,
            doc_ref=ref.doc_ref,
        )
    except Exception:  # noqa: BLE001 - picked up again on the next turn
        _log.warning("could not schedule the index of {}", ref.doc_ref)


def _store_and_audit(
    *,
    request: Request,
    user: AuthenticatedUser,
    persona_id: str,
    conversation_id: str,
    file_bytes: bytes,
    filename: str,
    parsed: ParsedDocument,
    lease: UploadLease,
) -> document_service.DocumentRef | None:
    """Persist a parsed upload, then record its audit row (runs in a thread).

    Releases the upload lease when done, so the owner's upload slot is held through the
    store. Returns ``None`` without storing when the request was abandoned before this
    thread started. Nothing is embedded here, so there is no api time to charge: the
    background index has its own per-owner quota (D-FV0-31).
    """
    if not lease.start_store():
        return None
    try:
        return _store_and_audit_held(
            request=request,
            user=user,
            persona_id=persona_id,
            conversation_id=conversation_id,
            file_bytes=file_bytes,
            filename=filename,
            parsed=parsed,
        )
    finally:
        lease.release()


def _store_and_audit_held(
    *,
    request: Request,
    user: AuthenticatedUser,
    persona_id: str,
    conversation_id: str,
    file_bytes: bytes,
    filename: str,
    parsed: ParsedDocument,
) -> document_service.DocumentRef:
    scheduler = getattr(request.app.state, "document_index_scheduler", None)
    ref = document_service.upload(
        file_storage=request.app.state.file_storage,
        owner_id=user.id,
        persona_id=persona_id,
        conversation_id=conversation_id,
        file_bytes=file_bytes,
        filename=filename,
        parsed=parsed,
        # Fourth review finding 1: at most MAX_PENDING_DOCUMENTS_PER_OWNER documents wait
        # for the index per owner; past it the upload is refused before anything is stored.
        admit_retrieval=(lambda: scheduler.admit(user.id)) if scheduler is not None else None,
    )
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
    return ref


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
    """Read an uploaded or generated file by its workspace-relative ref.

    Serves every artifact the workspace holds, whatever produced it: uploads,
    generated images, charts, the Spec 28 rich-output set, and the office
    documents Spec 24 generates (R9-149).

    Cross-tenant access returns 404 by design (existence-disclosure-safe).
    Path-traversal attempts (``..``) reject as 404 via the sandbox resolver.

    The response always carries ``X-Content-Type-Options: nosniff``. Office
    documents (docx / pptx / xlsx) additionally come back as an attachment: no
    browser renders them inline, so a download is the only honest offer. A
    content-addressed ref also carries a year-long ``immutable`` Cache-Control
    (see :func:`is_content_addressed_ref`), so the browser stops re-downloading
    persona avatars on every navigation.
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

    # The served type is derived from the extension, so tell the browser to
    # trust it rather than sniff the bytes into something executable.
    headers["X-Content-Type-Options"] = "nosniff"
    if is_content_addressed_ref(ref):
        # The bytes behind a hashed ref are immutable, so let the browser keep
        # them: the web fetches these with a Bearer token (a plain <img> cannot
        # send one), and without this every persona avatar was re-downloaded on
        # every navigation. Deliberately NO ``Vary: Authorization``: the token
        # rotates every few minutes, so varying on it would key the cache to a
        # value that is always new and nothing would ever be reused. ``private``
        # keeps shared caches out of it, and a ref is only reachable by the
        # owner whose persona it belongs to (cross-tenant reads 404).
        headers["Cache-Control"] = _IMMUTABLE_CACHE_CONTROL
    if media_type in image_service.ATTACHMENT_MEDIA_TYPES:
        # Both parameters per RFC 6266: the quoted ASCII name every client
        # understands, and the percent-encoded UTF-8 name modern browsers
        # prefer, so a non-ASCII filename survives the download intact.
        headers["Content-Disposition"] = (
            f'attachment; filename="{_download_name(ref)}"; '
            f"filename*=UTF-8''{quote(PurePosixPath(ref).name, safe='')}"
        )

    return Response(content=file_bytes, media_type=media_type, headers=headers)


__all__ = ["is_content_addressed_ref", "router"]
