"""Workspace-artifact I/O through the FileStorage seam (Spec R5, R5-D-4).

The producer families (imagegen / image_service / document upload /
workspace_persister) and the serve/list paths used to resolve a path under the
per-persona sandbox root and write/read bytes inline with ``O_NOFOLLOW``. R5
routes them through ``app.state.file_storage`` instead, so an S3 backend lifts
the Fly-volume single-Machine pin. This module is the thin adapter:

- ``artifact_key`` builds the logical key ``{owner}/{persona}/{relative}`` — the
  key the ``LocalFileStorage`` resolves under the workspace root (byte-identical
  to the old ``sandbox_root / relative``) and the ``S3FileStorage`` uses as the
  object key.
- ``write_sidecar`` / ``read_sidecar`` route the ``.f5.json`` F5 sidecar through
  the SAME backend as a **sibling object** (R5-D-4), so artifact-listing works on
  S3. Writes are best-effort (a sidecar failure never fails the persist — the
  bytes are the deliverable); reads treat a missing sidecar as ``None``.
"""

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING

from persona_api.services.artifact_metadata import (
    SIDECAR_SUFFIX,
    WorkspaceArtifactMetadata,
)

if TYPE_CHECKING:
    from persona_api.storage import FileStorage

__all__ = ["artifact_key", "read_sidecar", "write_sidecar"]


def artifact_key(owner_id: str, persona_id: str, relative: str) -> str:
    """The logical storage key for a persona-scoped workspace-relative path."""
    return f"{owner_id}/{persona_id}/{relative}"


def write_sidecar(storage: FileStorage, key: str, meta: WorkspaceArtifactMetadata) -> None:
    """Write the F5 ``.f5.json`` sidecar as a sibling object (best-effort).

    Mirrors ``artifact_metadata.write_artifact_sidecar`` but through the backend
    so it lands next to the bytes on S3 too. A failure is suppressed — the
    sidecar is enrichment, not the deliverable (matches the inline producers)."""
    with contextlib.suppress(Exception):
        storage.put(key + SIDECAR_SUFFIX, meta.model_dump_json().encode("utf-8"))


def read_sidecar(storage: FileStorage, key: str) -> WorkspaceArtifactMetadata | None:
    """Read the F5 sidecar for ``key``; ``None`` when absent.

    Raises ``pydantic.ValidationError`` on a malformed sidecar (surfacing the
    bug, as the Path-based ``read_artifact_sidecar`` does)."""
    sidecar_key = key + SIDECAR_SUFFIX
    if not storage.exists(sidecar_key):
        return None
    return WorkspaceArtifactMetadata.model_validate_json(storage.get(sidecar_key).decode("utf-8"))
