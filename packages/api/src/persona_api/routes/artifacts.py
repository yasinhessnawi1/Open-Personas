"""Workspace artifact list route — Spec F5 T02 (D-F5-1).

Aggregates files from the per-persona workspace tree
(``workspace_root/<owner_id>/<persona_id>/``), reads sidecar metadata
(``<filename>.meta.json``) where present, applies query filters, and
returns a paginated window.

Backend half of the F5 artifact view (T14/T15 are the UI). Pure
filesystem aggregation — no DB changes. Per the four dominant concerns:
backend changes are minimal + justified + on the Spec 08 patterns
(RLS-scoped via the D-08-1 pool listener invariant, paginated,
structured errors).

The companion DELETE route is added at F5 T15 (D-F5-X-artifact-delete-shape)
alongside the UI delete flow.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from persona.errors import PersonaError, PersonaNotFoundError, SandboxViolationError
from pydantic import ValidationError

from persona_api.auth import AuthenticatedUser, get_current_user
from persona_api.middleware.rate_limit import rate_limit
from persona_api.schemas import (
    ArtifactItem,
    ArtifactListResponse,
    ArtifactMetadataView,
)
from persona_api.services import persona_service
from persona_api.services.artifact_metadata import (
    SIDECAR_SUFFIX,
    WorkspaceArtifactMetadata,
    is_any_sidecar,
)
from persona_api.services.artifact_storage import read_sidecar
from persona_api.services.provenance import ai_generated_from_source

if TYPE_CHECKING:
    from persona_api.storage import FileStorage

router = APIRouter(prefix="/v1/personas", tags=["artifacts"])

# Mirror image_service._EXT_MEDIA_TYPES for content-type derivation. Kept local
# so this route doesn't import a private symbol.
_EXT_MEDIA_TYPES: dict[str, str] = {
    # images
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
    # documents
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    # data
    ".json": "application/json",
    ".parquet": "application/vnd.apache.parquet",
}


def _media_type_for(path: Path) -> str:
    """Derive a media type from the file extension.

    Defaults to ``application/octet-stream`` for unknown extensions so the
    response shape is always well-formed (clients can still display by
    filename + size).
    """
    return _EXT_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def _ensure_persona_visible(request: Request, persona_id: str) -> None:
    """Pre-flight RLS check: persona must be visible under the caller's scope.

    Mirrors the per-endpoint pattern used by :mod:`routes.uploads` and
    :mod:`routes.conversations`. Cross-tenant persona id → 404 via the
    standard handler. Blocks cross-tenant artifact enumeration before any
    workspace I/O.
    """
    persona_service.get_persona(rls_engine=request.app.state.rls_engine, persona_id=persona_id)


# Sidecar enumeration is delegated to is_any_sidecar (covers both F5 and Spec 14
# sidecars per the Phase 5 discovery — see artifact_metadata module docstring).


def _to_view(meta: WorkspaceArtifactMetadata | None) -> ArtifactMetadataView | None:
    if meta is None:
        return None
    return ArtifactMetadataView(
        source=meta.source,
        # Spec R3 (R3-D-4 / Art. 50): derive the disclosure from the stored source.
        ai_generated=ai_generated_from_source(meta.source),
        type=meta.type,
        producing_spec=meta.producing_spec,
        conversation_id=meta.conversation_id,
        created_at=meta.created_at,
        original_name=meta.original_name,
    )


def _matches_filters(
    *,
    meta: WorkspaceArtifactMetadata | None,
    ref: str,
    source: str | None,
    artifact_type: str | None,
    conversation_id: str | None,
    q: str | None,
) -> bool:
    """Whether an artifact row passes the requested filter combination.

    Filters that require metadata (source / type / conversation_id) skip
    rows with missing sidecars when set — so unmetadata'd legacy files
    appear only in unfiltered or q-only views. ``q`` matches against the
    ref path + the sidecar's ``original_name`` (case-insensitive substring).
    """
    if source is not None and (meta is None or meta.source != source):
        return False
    if artifact_type is not None and (meta is None or meta.type != artifact_type):
        return False
    if conversation_id is not None and (meta is None or meta.conversation_id != conversation_id):
        return False
    if q is not None and q != "":
        needle = q.lower()
        haystack_parts = [ref.lower()]
        if meta is not None and meta.original_name is not None:
            haystack_parts.append(meta.original_name.lower())
        if not any(needle in part for part in haystack_parts):
            return False
    return True


def _sort_key(item: tuple[str, WorkspaceArtifactMetadata | None, int]) -> tuple[float, str]:
    """Sort key for artifact rows: created_at DESC, ref ASC for stability.

    Uses the sidecar ``created_at`` when present; a metadata-less legacy artifact
    (R5-D-4: no mtime available through the backend abstraction) sorts as oldest
    (``0.0``) — such files appear only in unfiltered / q-only views anyway."""
    ref, meta, _size = item
    ts = meta.created_at.timestamp() if meta is not None else 0.0
    # Negate for DESC (Python's tuple sort is ASC); ref ASC ties as secondary.
    return (-ts, ref)


@router.get(
    "/{persona_id}/artifacts",
    response_model=ArtifactListResponse,
    dependencies=[Depends(rate_limit("default"))],
)
async def list_artifacts(
    persona_id: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),  # noqa: ARG001 — RLS via contextvar
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source: Literal["upload", "generated"] | None = Query(None),
    type: Literal["image", "chart", "doc", "data", "diagram"] | None = Query(None),  # noqa: A002
    conversation_id: str | None = Query(None, max_length=200),
    q: str | None = Query(None, max_length=200),
) -> ArtifactListResponse:
    """List the persona's workspace artifacts (D-F5-1).

    - Walks ``workspace_root/<owner_id>/<persona_id>/**`` for non-sidecar
      files; reads ``.meta.json`` sidecars where present.
    - Filters by source / type / conversation_id / q (all optional).
    - Sorts by ``created_at`` DESC, paginates by ``offset``/``limit``.
    - ``limit`` is hard-capped at 200 via Pydantic ``Query(le=200)`` —
      requesting more returns ``422`` with a structured error (NOT silent
      truncation), per D-F5-X-artifact-list-pagination.

    Cross-tenant persona ids return 404 via the pre-flight RLS check.
    """
    _ensure_persona_visible(request, persona_id)

    # R5-D-4: enumerate through the storage backend (local walk / S3 list_objects_v2)
    # so the Files view works on S3 too. Keys come back as ``{owner}/{persona}/…``;
    # ``ref`` strips that prefix to match the pre-R5 persona-root-relative form.
    file_storage: FileStorage = request.app.state.file_storage
    prefix = f"{user.id}/{persona_id}"
    strip = f"{prefix}/"

    candidates: list[tuple[str, WorkspaceArtifactMetadata | None, int]] = []
    for obj in file_storage.list(prefix):
        if is_any_sidecar(Path(obj.key)):
            continue
        try:
            meta = read_sidecar(file_storage, obj.key)
        except PersonaError as exc:  # pragma: no cover — defence-in-depth
            raise PersonaNotFoundError(
                "artifact metadata malformed",
                context={"reason": "invalid", "ref": obj.key[:120]},
            ) from exc
        except ValidationError as exc:
            raise PersonaNotFoundError(
                "artifact metadata malformed",
                context={"reason": "invalid", "ref": obj.key[:120]},
            ) from exc
        ref = obj.key[len(strip) :] if obj.key.startswith(strip) else obj.key
        candidates.append((ref, meta, obj.size))

    filtered = [
        (ref, meta, size)
        for ref, meta, size in candidates
        if _matches_filters(
            meta=meta,
            ref=ref,
            source=source,
            artifact_type=type,
            conversation_id=conversation_id,
            q=q,
        )
    ]
    filtered.sort(key=_sort_key)

    total = len(filtered)
    window = filtered[offset : offset + limit]

    items = [
        ArtifactItem(
            ref=ref,
            size_bytes=size,
            media_type=_media_type_for(Path(ref)),
            metadata=_to_view(meta),
        )
        for ref, meta, size in window
    ]

    return ArtifactListResponse(
        total=total,
        limit=limit,
        offset=offset,
        items=items,
    )


@router.delete(
    "/{persona_id}/artifacts/{ref:path}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(rate_limit("default"))],
)
async def delete_artifact(
    persona_id: str,
    ref: str,
    request: Request,
    user: AuthenticatedUser = Depends(get_current_user),
) -> None:
    """Delete a workspace artifact (bytes + sidecar) per D-F5-X-artifact-delete-shape.

    Atomic invariant: bytes deleted BEFORE sidecar. A failure at the sidecar
    step surfaces 500 with structured detail so the operator can investigate;
    the bytes are already gone (ghost-sidecar state is recoverable).

    Cross-tenant persona ids return 404 via the pre-flight RLS check.
    """
    _ensure_persona_visible(request, persona_id)

    file_storage: FileStorage = request.app.state.file_storage
    key = f"{user.id}/{persona_id}/{ref}"

    # Existence + traversal-safety through the backend: LocalFileStorage.exists
    # runs resolve_sandbox_path (traversal → SandboxViolationError) +
    # is_regular_file_nofollow (swapped symlink / missing → False); S3 head_object.
    try:
        present = file_storage.exists(key)
    except SandboxViolationError as exc:
        raise PersonaNotFoundError(
            "artifact not found",
            context={"reason": "not_found", "ref": ref[:120]},
        ) from exc
    if not present:
        raise PersonaNotFoundError(
            "artifact not found",
            context={"reason": "not_found", "ref": ref[:120]},
        )

    # Bytes-first per the atomicity invariant (D-F5-X-artifact-delete-shape).
    try:
        file_storage.delete(key)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "workspace_consistency_error",
                "phase": "delete_bytes",
                "ref": ref[:120],
            },
        ) from exc

    # The F5 ``.f5.json`` sidecar is a sibling object — remove it after the bytes.
    try:
        file_storage.delete(key + SIDECAR_SUFFIX)
    except OSError as exc:
        raise HTTPException(
            status_code=500,
            detail={
                "error": "workspace_consistency_error",
                "phase": "delete_sidecar",
                "ref": ref[:120],
                "bytes_already_deleted": True,
            },
        ) from exc

    return


__all__ = ["router"]
