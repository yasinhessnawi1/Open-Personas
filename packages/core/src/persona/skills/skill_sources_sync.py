"""Skill-sources fetch + aggregation (Spec S2, C1) — OFFLINE-ONLY.

The one structural difference from N2's single-upstream sync (S2-D-1): the build-new
step **fans out across the enabled per-source adapters** (Anthropic=``vetted`` ⊕
OpenClaw=``community``; arbitrary GitHub=``third_party`` joins in D1), each assigning its
tier. The reconcile + loader are reused unchanged.

This module is the network seam (git clone lives here, like N1's ``mirror_sync``); the
aggregation (:func:`build_curated_source_specs`) is pure so it is unit-tested with injected
local checkouts, and :func:`clone_at_ref` is the offline clone the worker uses. Never reached
from the request path.
"""

from __future__ import annotations

import shutil
import subprocess  # noqa: S404 — git clone of operator/curated repos; offline path
from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.skills.skill_mirror_reconcile import SkillMirrorSyncResult, reconcile_skill_mirror
from persona.skills.sources.anthropic import ingest_anthropic_skills
from persona.skills.sources.normalize import normalize_skill_into_mirror
from persona.skills.sources.openclaw import ingest_openclaw_skills

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from persona.schema.skills import SkillSpec

__all__ = [
    "SourceCheckout",
    "build_curated_source_specs",
    "clone_at_ref",
    "materialize_skill_mirror",
    "sync_skill_mirror",
]

_log = get_logger("skills.skill_sources_sync")


@dataclass(frozen=True)
class SourceCheckout:
    """A fetched source checkout + its resolved coordinate (for provenance/verification).

    Attributes:
        root: The checkout root on disk (a temp dir during a sync).
        repo_url: The origin URL the checkout was fetched from (``git remote get-url
            origin``) — verified for ``vetted`` (S2-D-4).
        commit: The commit the checkout is at (``git rev-parse HEAD``) — verified for
            ``vetted`` against the pinned constant (S2-D-4/D-5).
    """

    root: Path
    repo_url: str
    commit: str


def build_curated_source_specs(
    *,
    anthropic: SourceCheckout | None,
    openclaw: SourceCheckout | None,
) -> list[SkillSpec]:
    """Aggregate the enabled curated source adapters into one ``SkillSpec`` list.

    Each source contributes its skills at its assigned tier; a ``None`` source contributes
    nothing. The Anthropic ingest verifies the pinned canonical coordinate before stamping
    ``vetted`` (S2-D-4) and raises on mismatch (fail-closed) — that propagates here, so a
    vetted-source authenticity failure aborts the build before any write.
    """
    out: list[SkillSpec] = []
    if anthropic is not None:
        out.extend(
            ingest_anthropic_skills(
                anthropic.root,
                fetched_repo_url=anthropic.repo_url,
                fetched_commit=anthropic.commit,
            )
        )
    if openclaw is not None:
        out.extend(
            ingest_openclaw_skills(
                openclaw.root,
                source_uri=openclaw.repo_url,
                source_ref=openclaw.commit,
            )
        )
    return out


def materialize_skill_mirror(specs: Sequence[SkillSpec], mirror_root: Path) -> None:
    """Materialize the normalized skill trees into the mirror dir + clean removed ones (D2).

    For each spec, :func:`~persona.skills.sources.normalize.normalize_skill_into_mirror`
    writes ``mirror_root/<name>`` with ``SKILL.md`` + ``supplements/*.md`` (executables dropped,
    S2-D-6/D-7), so the runtime's ``collect_skill_supplements`` covers them unchanged. Skill
    directories no longer in ``specs`` are removed — a removed skill's tree degrades gracefully
    (it does not linger as a stale, orphaned dir).

    The source directory for each spec is ``spec.path`` (the fetched checkout dir, still intact
    during the sync — before the snapshot rebases the path to the mirror).
    """
    keep = {spec.name for spec in specs}
    for spec in specs:
        normalize_skill_into_mirror(spec.path, mirror_root / spec.name)
    if mirror_root.is_dir():
        for child in mirror_root.iterdir():
            if child.is_dir() and child.name not in keep:
                shutil.rmtree(child, ignore_errors=True)


def sync_skill_mirror(
    *,
    mirror_path: Path,
    anthropic: SourceCheckout | None,
    openclaw: SourceCheckout | None,
) -> SkillMirrorSyncResult:
    """Build the aggregated specs, materialize their trees, and reconcile the snapshot.

    The build runs first (and raises on a vetted-authenticity / parse failure BEFORE any
    write), so a failure leaves the last-good mirror intact (D-N1-4). Materialization writes the
    normalized skill trees (D2) while the fetched checkout is still intact, then the reconcile
    writes the snapshot atomically.
    """
    new_specs = build_curated_source_specs(anthropic=anthropic, openclaw=openclaw)
    materialize_skill_mirror(new_specs, mirror_path.parent)
    return reconcile_skill_mirror(new_specs, mirror_path=mirror_path)


def clone_at_ref(repo_url: str, ref: str | None, dest: Path) -> SourceCheckout:
    """Shallow-clone ``repo_url`` (at ``ref`` when given) into ``dest`` (OFFLINE-ONLY).

    Resolves the checkout's actual origin URL + HEAD commit into a :class:`SourceCheckout`
    so the adapter can verify the coordinate (S2-D-4). Reuses N1's fixed-argv, no-shell git
    invocation shape. ``ref`` pins the fetch to an immutable commit (S2-D-5) when supplied.

    Raises:
        subprocess.CalledProcessError: the clone or rev-parse failed.
    """
    subprocess.run(  # noqa: S603 — fixed argv, no shell; repo is a constant/operator arg
        ["git", "clone", "--depth", "1", repo_url, str(dest)],  # noqa: S607 — git on PATH by design
        check=True,
        capture_output=True,
        text=True,
    )
    if ref is not None:
        subprocess.run(  # noqa: S603
            ["git", "-C", str(dest), "fetch", "--depth", "1", "origin", ref],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
        subprocess.run(  # noqa: S603
            ["git", "-C", str(dest), "checkout", ref],  # noqa: S607
            check=True,
            capture_output=True,
            text=True,
        )
    commit = subprocess.run(  # noqa: S603
        ["git", "-C", str(dest), "rev-parse", "HEAD"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    return SourceCheckout(root=dest, repo_url=repo_url, commit=commit)
