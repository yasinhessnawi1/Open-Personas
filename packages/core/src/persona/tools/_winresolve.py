"""Resolve a sandbox path on Windows without letting Windows follow any link (Spec WIN, T1.3/T1.5).

``Path.resolve()`` on Windows opens the path and asks the system where it really
leads, which FOLLOWS every link on the way. A link planted in the workspace that
points at a network share therefore makes Windows contact that host and
authenticate to it before the sandbox check even runs.

This resolver never follows and never asks the system about a path by name:

1. The requested path is normalised lexically below the root (Win32 treats ``..``
   lexically too), so a path that climbs out is an escape without any filesystem call.
2. The existing components are walked by HANDLE (:func:`persona.tools._winopen.find_first_link`):
   each is opened relative to its parent's handle with ``OBJ_DONT_REPARSE`` and
   ``FILE_OPEN_REPARSE_POINT``, so a link is opened as itself and no Win32 path is
   parsed. The walk stops at the FIRST reparse point below the root.
3. That link's target (read through its own handle) is classified lexically: a
   target that stays inside the root is a ``link`` refusal, one that leaves it is an
   ``escape``, and a network share, a device or an unreadable target is an escape too.
   Nothing outside the root is ever opened, looked up or contacted, not even a mapped
   drive a link might name.

**This walk is advisory; the opener is the enforcement (the TOCTOU reasoning).**
Between this walk and the moment the path is used, anything in the workspace can
change: a plain folder the walk saw can be swapped for a junction, a plain file for a
symbolic link. The walk does not try to close that window, and nothing relies on it
doing so. Every file operation on a resolved path goes through
:mod:`persona.tools._winopen`, which opens relative to a handle on the root with
``OBJ_DONT_REPARSE``: the kernel refuses a reparse point in ANY component at the moment
of the open, so a link swapped in after this walk is still refused. A component swapped
DURING the walk is harmless as well: its children are opened relative to the handle
already held on the original folder, never by name.
"""

from __future__ import annotations

import ntpath
import sys
from pathlib import Path, PureWindowsPath
from typing import NamedTuple

if sys.platform != "win32":  # pragma: no cover - the walk needs the Windows primitives
    raise ImportError("persona.tools._winresolve is only available on Windows")

from persona.tools import _winopen

__all__ = ["WalkResult", "resolve_without_following"]

#: The prefix Windows puts on junction targets for a local drive (``\\?\C:\...``).
_LOCAL_DEVICE_PREFIX = "\\\\?\\"


class WalkResult(NamedTuple):
    """Where a sandbox path leads, and whether a link was met on the way.

    Attributes:
        path: The lexical absolute destination: the path itself when no link was met,
            or the first link's target joined with the rest of the path. ``None`` when
            that link names a network share, a device, a non-drive volume, or cannot
            be read (always an escape).
        crossed_link: Whether a component below the root is a link or other reparse
            point. The resolver refuses such a path even when it stays inside the root
            (Spec WIN ruling 3: the workspace does not follow links or shortcuts).
    """

    path: Path | None
    crossed_link: bool


def resolve_without_following(root: Path, requested: str) -> WalkResult:
    """Where ``requested`` (below ``root``) leads, without letting Windows follow links.

    Args:
        root: The already-resolved workspace root (trusted; links above it are fine).
        requested: The relative path, already through the lexical checks (``/``-separated).

    Returns:
        The :class:`WalkResult`.
    """
    candidate = Path(ntpath.normpath(root / requested))
    if not candidate.is_relative_to(root):
        return WalkResult(path=candidate, crossed_link=False)  # an escape, lexically
    parts = candidate.relative_to(root).parts
    if not parts:
        return WalkResult(path=candidate, crossed_link=False)
    found = _winopen.find_first_link(root, parts)
    if found is None:
        return WalkResult(path=candidate, crossed_link=False)
    link = root.joinpath(*parts[: found.position + 1])
    if found.target is None:
        # A reparse point that is not a readable symbolic link or junction: refused as a
        # link where it sits (the opener would refuse it at open time anyway).
        return WalkResult(path=link, crossed_link=True)
    destination = _local_target(link, found.target)
    if destination is None:
        return WalkResult(path=None, crossed_link=True)
    rest = parts[found.position + 1 :]
    return WalkResult(
        path=Path(ntpath.normpath(PureWindowsPath(destination, *rest))), crossed_link=True
    )


def _local_target(link: Path, target: str) -> PureWindowsPath | None:
    """The link's target as an absolute local path, or ``None`` for anything else."""
    if target.startswith(_LOCAL_DEVICE_PREFIX):
        target = target[len(_LOCAL_DEVICE_PREFIX) :]
        if not (len(target) >= 2 and target[1] == ":" and target[0].isalpha()):
            return None  # \\?\UNC\..., \\?\Volume{...}\, other devices
    resolved = PureWindowsPath(target)
    if not resolved.is_absolute():
        resolved = PureWindowsPath(link.parent) / resolved
    # A local target has a drive letter. A network share (a UNC path, with either slash)
    # or a device (the \\.\ namespace) has a UNC-shaped drive instead: never walked into.
    if not resolved.drive or resolved.drive.startswith(("\\\\", "//")):
        return None
    return PureWindowsPath(ntpath.normpath(resolved))
