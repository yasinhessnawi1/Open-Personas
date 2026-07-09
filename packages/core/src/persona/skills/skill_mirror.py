"""The file-on-volume skill mirror — snapshot loader + atomic write (Spec S2, C1).

A second mirror on N2's substrate (S2-D-1), parallel to
:mod:`persona.tools.mcp.mirror`: the offline/worker sync writes a ``skill_mirror.json``
snapshot of externally-sourced skills (each carrying its source-assigned ``trust`` +
``SkillProvenance`` — S2-D-3, never re-derived from front matter), and the request path
reads it **zero-network** and **fail-soft**.

**Fail-soft is load-bearing (D-N1-4 inherited):** the snapshot is optional and may be
absent (no sync yet), stale, or corrupt. :func:`load_skill_mirror` NEVER raises at boot —
a missing / unreadable / structurally-invalid snapshot degrades to the **empty external
set** (builtins, scanned separately, are unaffected).

The snapshot persists each skill's full ``SkillSpec`` (``model_dump(mode="json")``); on
load the ``path`` is **rebased** to the mirror directory (``mirror_root / <name>``), so the
record is robust to where it was written (the on-disk skill dir + its ``supplements/`` land
there in D2). Content rides inline, so injection works from the snapshot alone.

This module is deliberately git-/network-free: the fetching sync lives in
:mod:`persona.skills.skill_sources_sync`, never reachable from the request path.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path

from pydantic import ValidationError

from persona.logging import get_logger
from persona.schema.skills import SkillSpec

__all__ = [
    "SKILL_MIRROR_PATH",
    "declared_mirror_skills",
    "load_skill_mirror",
    "resolve_skill_mirror_read_path",
    "resolve_skill_mirror_write_path",
    "write_skill_mirror_atomic",
]

_log = get_logger("skills.skill_mirror")

#: The bundled snapshot path (package data beside the skills package). Absent until a
#: sync has run (fail-soft → empty). On a deployed image this is root-owned + lost on
#: redeploy, so the auto-sync writes to the volume override instead (S2-D-1 / N2-D-1) and
#: this stays the read-time default only.
SKILL_MIRROR_PATH: Path = Path(__file__).parent / "skill_mirror.json"


def _rebase_spec(raw: dict[str, object], mirror_root: Path) -> SkillSpec:
    """Reconstruct a ``SkillSpec`` from a snapshot record, rebasing its path.

    The persisted ``path`` is replaced with ``mirror_root / <name>`` so the skill dir
    (and its ``supplements/``) resolves under the local mirror regardless of where the
    snapshot was written.
    """
    data = dict(raw)
    data["path"] = str(mirror_root / str(data["name"]))
    return SkillSpec.model_validate(data)


def load_skill_mirror(path: Path = SKILL_MIRROR_PATH) -> list[SkillSpec]:
    """Load the skill-mirror snapshot, or ``[]`` on any failure (never raises).

    Args:
        path: The snapshot file (the writable volume override in production, else the
            bundled default).

    Returns:
        The externally-sourced ``SkillSpec`` list (trust + provenance reattached, path
        rebased to the mirror dir), or ``[]`` when the snapshot is absent / unreadable /
        structurally invalid — fail-soft, so a corrupt mirror can never break boot.
    """
    if not path.exists():
        return []
    mirror_root = path.parent
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        specs = [_rebase_spec(item, mirror_root) for item in raw["skills"]]
    except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
        _log.warning(
            "skill mirror snapshot unreadable; degrading to empty external set",
            path=str(path),
            error=type(exc).__name__,
        )
        return []
    _log.info("skill mirror snapshot loaded", path=str(path), skill_count=len(specs))
    return specs


def write_skill_mirror_atomic(specs: list[SkillSpec], path: Path) -> None:
    """Write the snapshot atomically (temp file + ``os.replace``); D-N1-4 inherited.

    A partially-written file is never observable: a sibling temp file is written and
    atomically renamed over ``path`` only once complete. On any failure the temp file is
    removed and the existing ``path`` is left untouched (last-good preserved).
    """
    payload = {"version": 1, "skills": [s.model_dump(mode="json") for s in specs]}
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".skill_mirror.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
        Path(tmp_name).replace(path)  # atomic on the same filesystem
    except BaseException:
        with contextlib.suppress(OSError):
            Path(tmp_name).unlink()
        raise


def declared_mirror_skills(
    declared: list[str],
    *,
    resolved_names: set[str],
    mirror_specs: list[SkillSpec],
) -> list[SkillSpec]:
    """Select the mirror specs for declared names not already resolved (availability ≠ enablement).

    The mirror makes external skills *available*; a persona loads only the ones it **declared**
    (the N2 boundary, S2-R-3). This returns, for each declared name that is in the mirror and not
    already resolved as a builtin, its mirror ``SkillSpec`` (carrying the source-assigned trust +
    provenance) — and **nothing** for an undeclared mirror skill (a sync never enables a skill on a
    persona) or for one a builtin already provides (no duplicate).

    Args:
        declared: The persona's expanded declared skill names (collections/aliases resolved).
        resolved_names: Names already resolved as builtins by the scanner.
        mirror_specs: The loaded mirror snapshot.

    Returns:
        The declared-and-available external specs, in declared order, de-duplicated.
    """
    mirror_by_name = {s.name: s for s in mirror_specs}
    out: list[SkillSpec] = []
    seen: set[str] = set()
    for name in declared:
        if name in resolved_names or name in seen:
            continue
        spec = mirror_by_name.get(name)
        if spec is not None:
            out.append(spec)
            seen.add(name)
    return out


def resolve_skill_mirror_read_path(override: Path | None) -> Path:
    """The snapshot path LOADS read (S2-D-1 / N2-D-1): the override, else the bundled fallback.

    Loads may fall back to the bundled :data:`SKILL_MIRROR_PATH` — reading committed
    package data is always safe (and :func:`load_skill_mirror` degrades to ``[]`` when it
    is absent). Only WRITES require an explicit target
    (:func:`resolve_skill_mirror_write_path`).
    """
    return override if override is not None else SKILL_MIRROR_PATH


def resolve_skill_mirror_write_path(override: Path | None) -> Path | None:
    """The path the auto-sync writes the reconciled snapshot to, or ``None`` (S2-D-1 / N2-D-1).

    The configured ``override`` (``PERSONA_SKILL_MIRROR_PATH`` — the writable mirror on the
    mounted volume) when set, else ``None`` — the sync must then SKIP (warn), never write.
    The bundled :data:`SKILL_MIRROR_PATH` is committed package data and a **read-time
    fallback only** (the N2-D-1 posture the skill mirror inherits): on a deployed image it
    is root-owned + lost on redeploy, and in a dev/test checkout writing it dirties the git
    tree (R9-011). Writes therefore REQUIRE an explicit target.
    """
    return override
