"""Discover ``SKILL.md`` files under a fetched checkout (Spec S2, shared by B/D).

A skill is a directory containing a ``SKILL.md`` (the Agent-Skills layout), so
discovery walks the checkout for ``SKILL.md`` files. This is the **curated-source**
walk (Anthropic/OpenClaw are trusted coordinates). The arbitrary-GitHub path (D1)
wraps discovery with the S2-D-8a ingest-act hardening (no-symlink-escape, resource
bounds, ephemeral cleaned clone) — that hardening lands at D1 where it is gated +
tested, since that is where the checkout is attacker-controlled.

OFFLINE-ONLY, like the MCP mirror sync: discovery runs over a fetched checkout in
the worker/sync path, never the request path.
"""

from __future__ import annotations

import os
from pathlib import Path

from persona.logging import get_logger

__all__ = ["discover_skill_mds", "discover_skill_mds_hardened"]

_log = get_logger("skills.sources.discovery")

#: D1 (S2-D-8a) default ingest bounds for the arbitrary-GitHub path — a giant /
#: zip-bomb-style repo can't exhaust the volume. Generous for honest repos, hard caps for
#: hostile ones; over-bound ⇒ skip-with-reason (never OOM / fill-disk).
_DEFAULT_MAX_FILES = 2000
_DEFAULT_MAX_FILE_BYTES = 1024 * 1024  # 1 MiB per SKILL.md
_DEFAULT_MAX_TOTAL_BYTES = 50 * 1024 * 1024  # 50 MiB of manifests total

#: Accepted manifest filenames under legacy casing (S2-D-10), case-insensitive.
#: ``"SKILL.md".lower() == "skill.md"`` so the canonical + ``skill.md`` collapse to
#: one bucket; ``skills.md`` is the deprecated plural OpenClaw also accepts.
_LEGACY_MANIFEST_LOWER = frozenset({"skill.md", "skills.md"})


def discover_skill_mds(checkout_root: Path, *, accept_legacy_casing: bool = False) -> list[Path]:
    """Return the skill-manifest paths under ``checkout_root``, in sorted order.

    Args:
        checkout_root: The root of a fetched source checkout.
        accept_legacy_casing: When ``True`` (OpenClaw, S2-D-10), also accept
            ``skill.md`` / ``skills.md`` casings — one manifest per directory,
            preferring canonical ``SKILL.md``. When ``False`` (the default;
            Anthropic / canonical sources), only exact ``SKILL.md`` matches.

    Returns:
        The discovered manifest paths, sorted (deterministic order). An absent root
        yields an empty list (warn-and-skip is the caller's job; discovery just
        finds files).
    """
    if not checkout_root.is_dir():
        return []
    if not accept_legacy_casing:
        return sorted(p for p in checkout_root.rglob("SKILL.md") if p.is_file())
    # Legacy casing: one manifest per directory, picked by priority.
    dirs = {
        p.parent
        for p in checkout_root.rglob("*")
        if p.is_file() and p.name.lower() in _LEGACY_MANIFEST_LOWER
    }
    out = [m for m in (_pick_manifest(d) for d in dirs) if m is not None]
    return sorted(out)


def discover_skill_mds_hardened(
    checkout_root: Path,
    *,
    max_files: int = _DEFAULT_MAX_FILES,
    max_file_bytes: int = _DEFAULT_MAX_FILE_BYTES,
    max_total_bytes: int = _DEFAULT_MAX_TOTAL_BYTES,
) -> list[Path]:
    """Discover ``SKILL.md`` under an ATTACKER-CONTROLLED checkout, hardened (D1, S2-D-8a).

    The arbitrary-GitHub path runs ``git`` on an attacker URL and walks attacker content, so
    discovery is hardened where the exposure lives (the fetch), independent of the downstream
    ``third_party`` tier + consent gate:

    - **No symlink-escape:** the walk does **not** follow directory symlinks
      (``os.walk(followlinks=False)``), a manifest that is itself a **symlink** is refused
      (a ``SKILL.md`` → ``/etc/passwd`` must read nothing), and any manifest whose real path
      escapes the clone root is refused (belt-and-suspenders, R2's resolve/is-relative-to).
    - **Resource bounds:** per-file size, total size, and file-count are bounded; an
      over-bound repo stops collecting (skip-with-reason), never exhausting the volume.

    Every refusal/stop logs a structured reason so a curator can diagnose (S2-D-7 discipline).

    Args:
        checkout_root: The (attacker-controlled) checkout root.
        max_files: Cap on the number of manifests collected.
        max_file_bytes: Cap on a single ``SKILL.md`` size.
        max_total_bytes: Cap on the cumulative manifest bytes.

    Returns:
        The safe, in-bounds ``SKILL.md`` paths, sorted.
    """
    if not checkout_root.is_dir():
        return []
    root_resolved = checkout_root.resolve()
    out: list[Path] = []
    total = 0
    for dirpath, _dirnames, filenames in os.walk(checkout_root, followlinks=False):
        if "SKILL.md" not in filenames:
            continue
        manifest = Path(dirpath) / "SKILL.md"
        if manifest.is_symlink():
            _log.warning("skipping symlinked skill manifest (escape guard)", path=str(manifest))
            continue
        try:
            resolved = manifest.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root_resolved):
            _log.warning("skipping manifest resolving outside the clone root", path=str(manifest))
            continue
        try:
            size = manifest.stat().st_size
        except OSError:
            continue
        if size > max_file_bytes:
            _log.warning("skipping oversize skill manifest", path=str(manifest), size=size)
            continue
        if len(out) >= max_files:
            _log.warning("skill manifest file-count bound reached; skipping rest", cap=max_files)
            break
        if total + size > max_total_bytes:
            _log.warning(
                "skill manifest total-size bound reached; skipping rest", cap=max_total_bytes
            )
            break
        total += size
        out.append(manifest)
    return sorted(out)


def _pick_manifest(directory: Path) -> Path | None:
    """Pick one manifest in ``directory`` — prefer canonical ``SKILL.md`` (S2-D-10)."""
    candidates = [
        e for e in directory.iterdir() if e.is_file() and e.name.lower() in _LEGACY_MANIFEST_LOWER
    ]
    if not candidates:
        return None

    def _rank(entry: Path) -> tuple[int, int, str]:
        # skill.md-family (incl. canonical SKILL.md) before skills.md; within a
        # family, exact canonical casing wins; name as a stable final tiebreak.
        family = 0 if entry.name.lower() == "skill.md" else 1
        canonical = 0 if entry.name == "SKILL.md" else 1
        return (family, canonical, entry.name)

    return min(candidates, key=_rank)
