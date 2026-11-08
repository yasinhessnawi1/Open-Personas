"""Where a persona's files live, and the only sanctioned way to name a place inside them.

Every persona's files sit under ``{owner_id}/{persona_id}/``, both as storage keys
(:class:`persona_api.storage.FileStorage`) and as folders below the workspace root. This module
is the one place those pieces are put together, and it refuses anything that would make the
result mean a different place:

- the owner and persona are each exactly one name (:func:`require_path_segment`): ``..``,
  ``.``, a separator or a Windows-only meaning is refused;
- a relative path inside the persona is a plain downward path: no ``..`` or ``.`` component,
  no empty component, not absolute, no backslash, no control character.

The resolver behind ``LocalFileStorage`` only checks that a key stays inside the workspace
root, which every tenant shares, so ``../../<other owner>/<their persona>/uploads/x.png`` used
to pass it. Checking the pieces here closes that for both backends: on S3 a ``..`` is not a
traversal, but it is still not a name this code ever writes.

It also owns the persona id format, because the routes refuse every other shape (R9-284 audit:
a URL id of ``%2E%2E`` reached the delete as ``..``). Ids have been minted as
``persona_<32 lower-case hex>`` since the first hosted release, and nowhere else, so the format
is checked against the one function that mints them.
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path, PurePosixPath

from persona.errors import SandboxViolationError
from persona.tools._sandbox import require_path_segment, resolve_sandbox_path

__all__ = [
    "PERSONA_ID_PATTERN",
    "is_persona_id",
    "new_persona_id",
    "persona_key",
    "persona_prefix",
    "persona_root",
    "require_relative_path",
]

#: The only shape a persona id has ever had. ``new_persona_id`` mints it; the routes accept
#: nothing else (``persona_api.routes._params.PersonaIdPath``).
PERSONA_ID_PATTERN = r"^persona_[0-9a-f]{32}$"

_PERSONA_ID = re.compile(PERSONA_ID_PATTERN)


def new_persona_id() -> str:
    """A fresh persona id, in the one format :data:`PERSONA_ID_PATTERN` accepts."""
    return f"persona_{uuid.uuid4().hex}"


def is_persona_id(value: str) -> bool:
    """Whether ``value`` is a persona id in the minted format (a trailing newline is not)."""
    return _PERSONA_ID.fullmatch(value) is not None


def require_relative_path(relative: str) -> str:
    """Return ``relative`` if it is a plain downward path, else refuse it.

    Stricter than the resolver on purpose: the resolver accepts ``a/../b`` because it stays
    inside the root, but no key this code writes looks like that, so one arriving from a
    request is someone trying a path, and it gets no benefit of the doubt.

    Raises:
        SandboxViolationError: ``reason="not_a_plain_path"``.
    """
    parts = relative.split("/")
    refused = (
        not relative
        or PurePosixPath(relative).is_absolute()
        or "\\" in relative
        or any(ord(c) < 32 or ord(c) == 127 for c in relative)
        or any(part in ("", ".", "..") for part in parts)
    )
    if refused:
        raise SandboxViolationError(
            "path inside a persona is not a plain relative path",
            context={"reason": "not_a_plain_path", "preview": relative[:80]},
        )
    return relative


def persona_prefix(owner_id: str, persona_id: str) -> str:
    """The storage prefix for everything a persona owns, ending at its folder (``.../``)."""
    owner = require_path_segment(owner_id, what="owner id")
    persona = require_path_segment(persona_id, what="persona id")
    return f"{owner}/{persona}/"


def persona_key(owner_id: str, persona_id: str, relative: str) -> str:
    """The storage key for ``relative`` inside one persona: ``{owner}/{persona}/{relative}``."""
    return persona_prefix(owner_id, persona_id) + require_relative_path(relative)


def persona_root(workspace_root: Path, owner_id: str, persona_id: str) -> Path:
    """The persona's folder below the workspace root, and nothing above or beside it.

    Returns the plain join ``workspace_root / owner_id / persona_id`` (callers compare and
    display it, so it is not canonicalised). Before returning, the folder is resolved the way
    the file tools resolve paths (on Windows without following a link) and must land exactly
    one level below the owner's folder.

    Raises:
        SandboxViolationError: an owner or persona that is not one name, or a folder that
            resolves anywhere else.
    """
    owner = require_path_segment(owner_id, what="owner id")
    persona = require_path_segment(persona_id, what="persona id")
    resolved = resolve_sandbox_path(workspace_root, f"{owner}/{persona}")
    owner_folder = resolve_sandbox_path(workspace_root, owner)
    if resolved.parent != owner_folder:
        raise SandboxViolationError(
            "persona folder is not directly inside its owner's folder",
            context={"reason": "escape", "what": "persona folder"},
        )
    return workspace_root / owner / persona
