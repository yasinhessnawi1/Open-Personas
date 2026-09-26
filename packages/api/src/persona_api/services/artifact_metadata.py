"""Workspace artifact metadata sidecar — Spec F5 T03 (D-F5-2).

Sidecar JSON files live next to workspace bytes at ``<bytes-path>.f5.json``.
Producer code (F3 upload service, Spec 15 imagegen, Spec 16/17 sandbox persister)
writes a sidecar at the same write-time as the bytes; the F5 artifact-list
endpoint (T02) reads sidecars to filter/sort/display.

**Suffix choice — Phase 5 discovery (2026-06-07):** Spec 14's
``document_service.upload`` already writes ``<bytes-path>.meta.json``
sidecars containing the :class:`DocumentRef` shape (workspace_path /
format / strategy / token_count / page_count / images). F5's sidecar
shape is semantically different (source / type / producing_spec /
conversation_id / created_at / original_name) so the two sidecars must
co-exist on disk without collision. F5 uses the dedicated ``.f5.json``
suffix; the artifact-list endpoint enumerates non-sidecar files and
skips both ``.meta.json`` AND ``.f5.json`` during the walk. Documented
as a refinement of D-F5-X-artifact-metadata-convention; the decisions
mirror records the amendment.

Why a sidecar over a DB-backed table at v0.1 — D-F5-2: the cheapest cross-spec
amendment surface (~30 LOC per producer family vs ~500+ LOC for a DB-backed
migration + model + RLS policy). Path-prefix is rigid; DB-backed is v0.2
candidate when query complexity demands SQL JOIN.

The sidecar is purely an API-layer concern (not a memory protocol), so this
module lives in ``persona-api/services`` rather than ``persona-core``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from persona.tools._sandbox import (
    delete_regular_file_nofollow,
    is_regular_file_nofollow,
    read_nofollow_bytes,
    write_nofollow_bytes,
)
from pydantic import AwareDatetime, BaseModel, ConfigDict

__all__ = [
    "SIDECAR_SUFFIX",
    "SPEC_14_SIDECAR_SUFFIX",
    "WorkspaceArtifactMetadata",
    "delete_artifact_sidecar",
    "is_any_sidecar",
    "read_artifact_sidecar",
    "sidecar_path_for",
    "write_artifact_sidecar",
]

#: Suffix for F5 artifact metadata sidecars (this module).
SIDECAR_SUFFIX = ".f5.json"

#: Suffix Spec 14's ``document_service.upload`` uses for its DocumentRef
#: sidecar. F5's artifact-list endpoint skips both during enumeration so the
#: sidecar files themselves never surface as artifacts.
SPEC_14_SIDECAR_SUFFIX = ".meta.json"


def is_any_sidecar(path: Path) -> bool:
    """Whether ``path`` is a sidecar file (F5 or Spec 14) — skip during walks."""
    name = path.name
    return name.endswith(SIDECAR_SUFFIX) or name.endswith(SPEC_14_SIDECAR_SUFFIX)


class WorkspaceArtifactMetadata(BaseModel):
    """Sidecar shape for a workspace artifact (D-F5-X-artifact-metadata-convention).

    Frozen + ``extra="forbid"`` so producers cannot drift the shape silently;
    the artifact-list endpoint relies on strict validation when reading
    sidecars off disk.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["upload", "generated"]
    """Where the bytes came from."""

    type: Literal["image", "chart", "doc", "data", "diagram"]
    """What the bytes represent at the F4 dispatcher layer. ``"diagram"`` added
    in Spec 28 for ``render_diagram`` Mermaid/Graphviz source artifacts
    (D-28-X-f5-literal-extension)."""

    producing_spec: Literal["12", "13", "14", "15", "16", "17", "28"]
    """Spec that produced this artifact (Spec 12 sandbox general files /
    Spec 13 image upload / Spec 14 doc upload / Spec 15 imagegen / Spec 16
    doc generation / Spec 17 charts + data analysis / Spec 28 rich-output
    persister: chat-path image / file / diagram bytes)."""

    conversation_id: str | None
    """Conversation that produced this artifact (None for persona-scoped uploads
    outside a conversation context)."""

    created_at: AwareDatetime
    """Wall-clock time the sidecar was written (UTC, tz-aware)."""

    original_name: str | None
    """User-facing filename (uploads carry the original; generated artifacts
    are typically None — the producer chose the on-disk name)."""


def sidecar_path_for(bytes_path: Path) -> Path:
    """Return the canonical sidecar path for a given workspace bytes path.

    The sidecar lives at ``<bytes-path>.meta.json`` (suffix appended, not
    replacement) so the bytes filename remains a substring of the sidecar
    name for easy grep / find-by-stem.
    """
    return bytes_path.with_name(bytes_path.name + SIDECAR_SUFFIX)


def write_artifact_sidecar(
    bytes_path: Path, meta: WorkspaceArtifactMetadata, *, root: Path
) -> None:
    """Write a sidecar JSON next to ``bytes_path``.

    Idempotent overwrite: re-uploading or re-generating the same path writes
    a fresh sidecar. Concurrent writes from two requests for the same path
    are last-writer-wins at v0.1 (acceptable; documented in D-F5-X-artifact-
    metadata-convention).

    The parent directory must already exist (the producer always creates
    parents before calling — this helper does not autocreate).
    """
    sidecar = sidecar_path_for(bytes_path)
    payload = meta.model_dump_json()
    # Spec WIN T1.5/T1.6: never through a link planted at the sidecar's name, and always
    # under the workspace root (required: no weaker fallback for a forgotten argument).
    write_nofollow_bytes(sidecar, payload.encode("utf-8"), root=root)


def read_artifact_sidecar(bytes_path: Path, *, root: Path) -> WorkspaceArtifactMetadata | None:
    """Read a sidecar for ``bytes_path``; return ``None`` if missing.

    Returns ``None`` for missing sidecars (artifact predates sidecar
    convention, or the producer-side touch hasn't shipped yet). Raises
    ``pydantic.ValidationError`` on schema mismatch — surfacing the bug
    rather than silently dropping malformed metadata.
    """
    sidecar = sidecar_path_for(bytes_path)
    if not is_regular_file_nofollow(sidecar, root=root):
        return None
    raw = read_nofollow_bytes(sidecar, root=root)
    return WorkspaceArtifactMetadata.model_validate_json(raw.decode("utf-8"))


def delete_artifact_sidecar(bytes_path: Path, *, root: Path) -> bool:
    """Delete the sidecar for ``bytes_path``; return True if it existed.

    Used by the F5 artifact-delete endpoint (T15) — the atomicity invariant
    is bytes-deleted-BEFORE-sidecar per D-F5-X-artifact-delete-shape, so
    this helper runs AFTER the bytes ``unlink`` has succeeded.

    Returns ``True`` if a sidecar was removed, ``False`` if none existed.
    Raises ``OSError`` on filesystem failure — the caller surfaces it as
    ``WorkspaceConsistencyError(context={"phase": "delete_sidecar"})``.
    """
    sidecar = sidecar_path_for(bytes_path)
    return delete_regular_file_nofollow(sidecar, root=root)


def utcnow() -> datetime:
    """Wall-clock UTC for ``created_at`` — tz-aware datetime.

    Centralised so producer code reads consistently and tests can monkey-patch
    this single symbol to inject deterministic timestamps.
    """
    return datetime.now(tz=UTC)
