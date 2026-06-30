"""Fetch-time skill normalization into the mirror (Spec S2, D2 / S2-D-6, S2-D-7).

External skills use the Agent-Skills optional dirs ``scripts/`` (executable), ``references/``
(``.md`` docs), ``assets/`` (templates/data) — not our ``supplements/``. This module
**normalizes** a source skill into the mirror so the runtime's EXISTING boundary covers it
**unchanged** (no new ingress, the no-widening rule):

- the manifest is written as canonical ``SKILL.md``;
- ``references/*.md`` (text) are folded into ``supplements/`` alongside any existing
  ``supplements/*.md`` — so ``persona.skills.use_skill_tool.collect_skill_supplements`` (which
  scans ``<path>/supplements/*.md`` into the Spec-16 ``SandboxFile`` transport) covers them
  byte-unchanged;
- ``scripts/`` and every non-``.md`` (executable/binary asset) are **DROPPED** — never written
  to the mirror (staging untrusted executables is the widening the steer forbade; the boundary
  stays text-``.md``-only).

Drops are **logged with a reason** (S2-D-7): a curator must see a skill was partially ingested
(text-only) so it never presents as fully-featured. A skill that is non-functional without its
``scripts/`` therefore degrades to a text-only entry (warned) or is dropped upstream — never
"stage the executable to make it work".
"""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.skills.sources.discovery import discover_skill_mds

if TYPE_CHECKING:
    from pathlib import Path

__all__ = ["normalize_skill_into_mirror"]

_log = get_logger("skills.sources.normalize")

#: Source subdirectories whose text ``.md`` files are folded into ``supplements/``.
_TEXT_SUPPLEMENT_DIRS = ("references", "supplements")


def normalize_skill_into_mirror(source_dir: Path, dest_dir: Path) -> list[str]:
    """Materialize ``source_dir`` into the mirror at ``dest_dir`` (text-``.md``-only).

    Writes ``SKILL.md`` + ``supplements/*.md`` (from ``references/`` + ``supplements/``) and
    **drops** ``scripts/`` + every non-``.md`` file. The runtime's ``collect_skill_supplements``
    then covers the normalized ``supplements/`` unchanged.

    Args:
        source_dir: The source skill directory (in the fetched checkout).
        dest_dir: The mirror skill directory to materialize into.

    Returns:
        The relative paths of everything **dropped** (scripts + non-text), for caller logging /
        curator visibility. Also logged here with a reason (S2-D-7).
    """
    dest_dir.mkdir(parents=True, exist_ok=True)

    # The manifest — written as canonical SKILL.md (legacy casings normalized, S2-D-10).
    manifests = discover_skill_mds(source_dir, accept_legacy_casing=True)
    if manifests:
        shutil.copyfile(manifests[0], dest_dir / "SKILL.md")

    dropped: list[str] = []
    supplements_dest = dest_dir / "supplements"

    for sub in _TEXT_SUPPLEMENT_DIRS:
        src_sub = source_dir / sub
        if not src_sub.is_dir():
            continue
        for entry in sorted(src_sub.iterdir()):
            if entry.is_file() and not entry.is_symlink() and entry.suffix == ".md":
                supplements_dest.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(entry, supplements_dest / entry.name)
            else:
                dropped.append(f"{sub}/{entry.name}")

    # scripts/ (executable code) + any other non-text top-level content: DROPPED.
    for entry in sorted(source_dir.iterdir()):
        if entry.name == "SKILL.md" or (entry.is_dir() and entry.name in _TEXT_SUPPLEMENT_DIRS):
            continue
        if entry.name.lower() in {"skill.md", "skills.md"}:  # legacy manifest already copied
            continue
        dropped.append(f"{entry.name}/" if entry.is_dir() else entry.name)

    if dropped:
        _log.warning(
            "dropped untrusted non-text content during skill ingest (text-only)",
            skill=dest_dir.name,
            dropped=", ".join(sorted(dropped)),
        )
    return sorted(dropped)
