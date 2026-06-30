"""Skill-mirror reconcile — the auto-sync's idempotent diff/write (Spec S2, C1).

A second mirror on N2's reconcile shape (S2-D-1), parallel to
:mod:`persona.tools.mcp.mirror_reconcile`. The per-source adapters (Group B/D) produce
the freshly-ingested ``SkillSpec`` list; this module:

1. loads the EXISTING snapshot (an absent/unreadable one is treated as empty);
2. diffs **added / updated / removed** over a name-keyed diff, where *updated* keys on the
   body ``content_hash`` (S1-D-5 — a body change is exactly the re-consent trigger, so it is
   what counts as an update);
3. writes the new snapshot **atomically** (temp+rename — a failure leaves the last-good
   mirror intact, D-N1-4).

A **removed** skill drops from the snapshot (so it is never offered as available — the N2
``availability`` boundary) and is surfaced in :attr:`SkillMirrorSyncResult.removed` (the loss
is observable, never silent); a persona currently using it degrades gracefully downstream
(the scanner omits-with-warning — D-04-4).

**OFFLINE-ONLY** — the network-touching fetch lives in
:mod:`persona.skills.skill_sources_sync`; this module is pure diff + atomic write.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, ValidationError

from persona.logging import get_logger
from persona.skills.skill_mirror import write_skill_mirror_atomic

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from persona.schema.skills import SkillSpec

__all__ = ["SkillMirrorSyncResult", "diff_specs", "reconcile_skill_mirror"]

_log = get_logger("skills.skill_mirror_reconcile")


class SkillMirrorSyncResult(BaseModel):
    """The outcome of one reconcile — what changed, for observability.

    Attributes:
        added: Skill ids present in the new snapshot but not the old, sorted.
        updated: Skill ids in both whose body ``content_hash`` changed, sorted.
        removed: Skill ids in the old snapshot but not the new, sorted (surfaced,
            never silent).
        total: The total skill count in the new snapshot.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    added: tuple[str, ...]
    updated: tuple[str, ...]
    removed: tuple[str, ...]
    total: int


def _hash_of(spec: SkillSpec) -> str | None:
    return spec.provenance.content_hash if spec.provenance else None


def diff_specs(old: dict[str, SkillSpec], new: Sequence[SkillSpec]) -> SkillMirrorSyncResult:
    """Classify the change between an old snapshot and freshly-ingested specs.

    Pure, name-keyed: ``added`` = new ids not in old; ``removed`` = old ids not in new;
    ``updated`` = ids in both whose body ``content_hash`` differs (S1-D-5). All sorted for a
    deterministic, log-stable result.
    """
    new_by_name = {s.name: s for s in new}
    old_names = set(old)
    new_names = set(new_by_name)
    added = sorted(new_names - old_names)
    removed = sorted(old_names - new_names)
    updated = sorted(
        name for name in old_names & new_names if _hash_of(old[name]) != _hash_of(new_by_name[name])
    )
    return SkillMirrorSyncResult(
        added=tuple(added),
        updated=tuple(updated),
        removed=tuple(removed),
        total=len(new_by_name),
    )


def _load_existing_specs(path: Path) -> dict[str, SkillSpec]:
    """Read the existing snapshot's specs, or ``{}`` if absent/unreadable/invalid.

    Deliberately NOT :func:`~persona.skills.skill_mirror.load_skill_mirror` rebasing — for a
    diff we need the literal prior records keyed by id; an unreadable prior is honestly "no
    prior state" → everything counts as added, and the file is rewritten clean.
    """
    if not path.exists():
        return {}
    try:
        from persona.schema.skills import SkillSpec

        raw = json.loads(path.read_text(encoding="utf-8"))
        specs: dict[str, SkillSpec] = {}
        for item in raw["skills"]:
            spec = SkillSpec.model_validate(item)
            specs[spec.name] = spec
    except (OSError, ValueError, KeyError, TypeError, ValidationError) as exc:
        _log.warning(
            "existing skill mirror snapshot unreadable; treating as empty for reconcile",
            path=str(path),
            error=type(exc).__name__,
        )
        return {}
    return specs


def reconcile_skill_mirror(
    new_specs: Sequence[SkillSpec],
    *,
    mirror_path: Path,
) -> SkillMirrorSyncResult:
    """Reconcile the snapshot at ``mirror_path`` against freshly-ingested ``new_specs``.

    Loads the existing snapshot, diffs it against ``new_specs``, and writes the new snapshot
    atomically. ``new_specs`` is built by the fetch layer (which raises on a clone/parse
    failure BEFORE reaching here, so the last-good mirror is preserved).

    Args:
        new_specs: The freshly-ingested external skills (aggregated across sources).
        mirror_path: The snapshot file (read for the old state, written with the new).

    Returns:
        The :class:`SkillMirrorSyncResult` counts (added/updated/removed/total).
    """
    old = _load_existing_specs(mirror_path)
    result = diff_specs(old, new_specs)
    write_skill_mirror_atomic(list(new_specs), mirror_path)
    _log.info(
        "skill mirror reconciled",
        path=str(mirror_path),
        added=len(result.added),
        updated=len(result.updated),
        removed=len(result.removed),
        total=result.total,
    )
    return result
