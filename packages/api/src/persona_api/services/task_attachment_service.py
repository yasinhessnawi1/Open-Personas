"""Storing a file someone attached to a task or routine hand-off (issue #16).

Chat has had attachments for a long time: you drop a file on the composer, it lands in the
persona's workspace under ``uploads/<hash>.<ext>``, and the turn is told where to read it.
Handing a persona a task had no such door, so people pasted file contents into the goal or
just described them. This module is the missing half, and it deliberately keeps the same
storage shape as :mod:`persona_api.services.image_service`: the persona-scoped ``uploads/``
directory, a content-addressed name, a workspace-relative ref. A leg then opens the file
with the ``file_read`` tool inside the persona's own sandbox, exactly as it would any other
workspace file.

Why not :mod:`persona_api.services.document_service`: that one parses and ingests a document
into a CONVERSATION-scoped store so chat retrieval can search it. A task has no
conversation, and a leg does not need the chunks. Here the bytes are stored and named, and
nothing else happens to them.

Images do not come through here at all. They already land in the same ``uploads/``
directory through the image service, which runs the magic-byte and decompression-bomb
gates; the upload route keeps sending them there.
"""

from __future__ import annotations

import hashlib
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from persona.documents.parsers import SUPPORTED_EXTENSIONS
from persona.errors import PersonaError
from persona.logging import get_logger
from pydantic import BaseModel, ConfigDict

from persona_api.services.artifact_metadata import WorkspaceArtifactMetadata, utcnow
from persona_api.services.artifact_storage import artifact_key, write_sidecar
from persona_api.services.image_service import MAX_UPLOAD_BYTES

if TYPE_CHECKING:
    from persona_api.storage import FileStorage

__all__ = ["MAX_ATTACHMENT_BYTES", "TaskAttachmentRef", "upload"]

_log = get_logger("api.task_attachments")

#: Workspace sub-directory holding persona-scoped uploads (shared with the image service,
#: so an attached file sits beside an attached picture rather than in a parallel world).
_UPLOAD_DIR_NAME = "uploads"

#: The same size ceiling an uploaded image gets. One cap, one number to change.
MAX_ATTACHMENT_BYTES = MAX_UPLOAD_BYTES


class TaskAttachmentRef(BaseModel):
    """What the caller gets back: where the file lives and what it is called."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    #: Workspace-relative path, ``uploads/<hash><ext>``.
    workspace_path: str
    #: The name the person knows the file by, preserved for display and for the leg.
    filename: str
    #: The media type as declared, or "" when the client did not say.
    media_type: str
    size_bytes: int


def upload(
    *,
    file_storage: FileStorage,
    owner_id: str,
    persona_id: str,
    file_bytes: bytes,
    filename: str,
    declared_media_type: str = "",
) -> TaskAttachmentRef:
    """Store an attached document under the persona's workspace and describe it back.

    Args:
        file_storage: The storage backend the workspace is served from.
        owner_id: The authenticated tenant (RLS-scoped by the caller).
        persona_id: The persona whose workspace holds the file.
        file_bytes: The raw bytes from the multipart upload.
        filename: The client-supplied name; its extension decides acceptance.
        declared_media_type: The multipart part's media type, when it had one.

    Returns:
        The :class:`TaskAttachmentRef` the task contract carries.

    Raises:
        PersonaError: ``reason="unsupported_media_type"`` for a format the document
            parsers do not know, or ``reason="oversize"`` past the cap.
    """
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix not in SUPPORTED_EXTENSIONS:
        raise PersonaError(
            "unsupported attachment format",
            context={"reason": "unsupported_media_type", "filename": filename[:120]},
        )
    if len(file_bytes) > MAX_ATTACHMENT_BYTES:
        raise PersonaError(
            "attachment exceeds size cap",
            context={
                "reason": "oversize",
                "size_bytes": str(len(file_bytes)),
                "max_bytes": str(MAX_ATTACHMENT_BYTES),
            },
        )

    ref = hashlib.blake2b(file_bytes, digest_size=16).hexdigest()
    relative = f"{_UPLOAD_DIR_NAME}/{ref}{suffix}"
    key = artifact_key(owner_id, persona_id, relative)
    file_storage.put(key, file_bytes, content_type=declared_media_type or None)

    write_sidecar(
        file_storage,
        key,
        WorkspaceArtifactMetadata(
            source="upload",
            type="doc",
            producing_spec="14",
            conversation_id=None,
            created_at=utcnow(),
            original_name=filename,
        ),
    )

    _log.info(
        "task attachment stored",
        owner_id=owner_id,
        persona_id=persona_id,
        workspace_path=relative,
        size_bytes=len(file_bytes),
    )
    return TaskAttachmentRef(
        workspace_path=relative,
        filename=filename,
        media_type=declared_media_type,
        size_bytes=len(file_bytes),
    )
