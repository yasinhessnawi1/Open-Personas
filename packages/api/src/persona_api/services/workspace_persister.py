"""Concrete ``WorkspacePersister`` adapter — local-FS workspace directory.

Spec 28 (D-28-X-persisted-artifact-shape / D-28-X-persister-user-scope). The
hexagonal adapter for the persona-core ``WorkspacePersister`` port: it persists
in-memory tool bytes (``generate_image`` / ``file_write`` / ``render_diagram``)
to the per-persona workspace using the same recipe the operator
``image_service`` path uses (``imagegen/service.py:_persist_bytes``) —
content-addressed blake2b name under ``uploads/``, ``O_NOFOLLOW`` write
(TOCTOU-safe via :func:`resolve_sandbox_path`), best-effort F5 ``.f5.json``
sidecar — generalized across arbitrary media types.

RLS-scoped to the persona owner: the per-(owner, persona) workspace path is
resolved from the sandbox request context (the same contextvar the code
execution persister uses), so an artifact is owned by — and served only to —
the persona owner via the existing ``GET /v1/personas/{id}/uploads/{ref}`` route
(D-28-10; ``download_url`` is derived from ``workspace_path`` at the web layer).

The returned ``workspace_path`` is the ``uploads/<hash><ext>`` workspace-relative
reference (identical in shape to ``GeneratedImage.workspace_path`` and the F5
artifact-list ``ref``), so generated-image artifacts from the chat path are
indistinguishable from the operator path downstream.
"""

from __future__ import annotations

import hashlib
import mimetypes
from typing import TYPE_CHECKING

from persona.schema.tools import PersistedArtifact

from persona_api.sandbox import get_sandbox_request_context
from persona_api.services.artifact_metadata import WorkspaceArtifactMetadata, utcnow
from persona_api.services.artifact_storage import artifact_key, write_sidecar

if TYPE_CHECKING:
    from persona_api.storage import FileStorage

__all__ = ["WorkspaceDirPersister"]

_UPLOAD_DIR_NAME = "uploads"

#: Media-type → extension for the Spec 28 render set. Falls back to
#: ``mimetypes.guess_extension`` then ``.bin`` for unknown types.
_EXT_BY_MIME: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "text/markdown": ".md",
    "text/plain": ".txt",
    "text/csv": ".csv",
    "text/html": ".html",
    "application/json": ".json",
    "application/pdf": ".pdf",
    "text/vnd.mermaid": ".mmd",
    "text/vnd.graphviz": ".dot",
}

#: Media-type → F5 sidecar ``type`` literal.
_SIDECAR_TYPE_BY_MIME_PREFIX: tuple[tuple[str, str], ...] = (
    ("image/", "image"),
    ("text/vnd.mermaid", "diagram"),
    ("text/vnd.graphviz", "diagram"),
)
_SIDECAR_TYPE_BY_MIME: dict[str, str] = {
    "text/csv": "data",
    "application/json": "data",
}


def _ext_for(mime_type: str, suggested_filename: str) -> str:
    """Resolve a file extension from the media type (preferred) or filename."""
    if mime_type in _EXT_BY_MIME:
        return _EXT_BY_MIME[mime_type]
    if "." in suggested_filename:
        return "." + suggested_filename.rsplit(".", 1)[-1].lower()
    return mimetypes.guess_extension(mime_type) or ".bin"


def _sidecar_type_for(mime_type: str) -> str:
    """Map a media type to the F5 sidecar ``type`` (default ``"doc"``)."""
    for prefix, kind in _SIDECAR_TYPE_BY_MIME_PREFIX:
        if mime_type.startswith(prefix):
            return kind
    return _SIDECAR_TYPE_BY_MIME.get(mime_type, "doc")


def _display_name_for(suggested_filename: str) -> str | None:
    """Sanitise the tool-suggested filename for the F5 sidecar ``original_name``.

    The bytes are stored content-addressed (``<blake2b><ext>``); without a
    human-readable ``original_name`` the Files viewer falls back to the hash.
    The tool always supplies a meaningful basename (``report.docx``,
    ``diagram.svg``, ``generated_image_0.png``) — keep it, stripped of control
    chars / path separators and length-capped. Returns ``None`` only when
    nothing usable survives (the viewer then falls back to the path segment).
    """
    base = suggested_filename.rsplit("/", 1)[-1].rsplit("\\", 1)[-1]
    cleaned = "".join(c for c in base if ord(c) >= 32).strip()[:120]
    return cleaned or None


class WorkspaceDirPersister:
    """Persist tool bytes to ``<workspace_root>/<owner>/<persona>/uploads/``.

    Satisfies the persona-core ``WorkspacePersister`` Protocol. RLS-scoped to
    the persona owner via the sandbox request context (D-28-X-persister-user-scope).
    """

    def __init__(self, *, file_storage: FileStorage, persona_id: str) -> None:
        self._file_storage = file_storage
        self._persona_id = persona_id

    async def persist(
        self,
        data: bytes,
        *,
        mime_type: str,
        suggested_filename: str,
    ) -> PersistedArtifact:
        """Persist ``data`` and return its :class:`PersistedArtifact`.

        Content-addressed (blake2b-16) so identical bytes collapse to one object
        (idempotent). R5-D-4: written through the storage backend (local
        byte-identical / S3 object); the F5 sidecar is best-effort (a sidecar
        failure never fails the persist — the bytes are the deliverable).

        Raises:
            RuntimeError: when no sandbox request context is bound — the chat
                persist path requires an authenticated, persona-scoped request.
        """
        ctx = get_sandbox_request_context()
        if ctx is None:
            msg = "WorkspaceDirPersister.persist requires a bound sandbox request context"
            raise RuntimeError(msg)

        ext = _ext_for(mime_type, suggested_filename)
        digest = hashlib.blake2b(data, digest_size=16).hexdigest()
        relative = f"{_UPLOAD_DIR_NAME}/{digest}{ext}"
        key = artifact_key(ctx.owner_id, self._persona_id, relative)

        self._file_storage.put(key, data, content_type=mime_type)
        write_sidecar(
            self._file_storage,
            key,
            WorkspaceArtifactMetadata(
                source="generated",
                type=_sidecar_type_for(mime_type),  # type: ignore[arg-type]
                producing_spec="28",
                conversation_id=ctx.conversation_id,
                created_at=utcnow(),
                original_name=_display_name_for(suggested_filename),
            ),
        )

        return PersistedArtifact(
            workspace_path=relative,
            mime_type=mime_type,
            size_bytes=len(data),
        )
