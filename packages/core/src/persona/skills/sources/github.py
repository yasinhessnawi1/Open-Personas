"""The arbitrary-GitHub skill adapter — the ``third_party`` tier (Spec S2, D1).

The lowest-trust source: a generic ``owner/repo`` SKILL.md discoverer. Safe to offer ONLY
because S1's subordination guard + consent gate stand behind it (``third_party`` requires
consent before injection — S1-D-4). But the consent gate governs what an installed skill can
*do*; it does NOT cover the **ingest act**, which runs ``git`` against an attacker-controlled
URL and walks attacker-controlled content. So the fetch is hardened independently (S2-D-8a),
via :func:`~persona.skills.sources.discovery.discover_skill_mds_hardened`:

- **no symlink-escape** (no following dir symlinks; symlinked / out-of-root manifests refused),
- **resource bounds** (per-file / total / file-count caps; over-bound ⇒ skip-with-reason),
- **ephemeral, cleaned clone** (:func:`fetch_github_skills` clones to a temp dir and removes it;
  only the ingested specs — content inline — survive).

Defense-in-depth: third_party tier (consent-gated) + hardened ingest + D2 dropping executables,
each layer independent.
"""

from __future__ import annotations

import re
import tempfile
from dataclasses import dataclass
from typing import TYPE_CHECKING

from persona.logging import get_logger
from persona.schema.skills import SkillTrust
from persona.skills.sources.discovery import discover_skill_mds_hardened
from persona.skills.sources.ingest import ingest_external_skills

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from persona.schema.skills import SkillSpec
    from persona.skills.skill_sources_sync import SourceCheckout

__all__ = [
    "GithubRepoSpec",
    "fetch_github_skills",
    "ingest_github_skills",
    "parse_github_repo_specs",
]

_log = get_logger("skills.sources.github")

#: A conservative, filesystem/URL-safe charset for a GitHub owner or repo segment.
#: Real GitHub slugs are a subset of this; anything else in ``PERSONA_SKILL_GITHUB_REPOS``
#: is almost certainly a typo (or, in principle, an attempt to smuggle a path/URL fragment
#: into a value that flows straight into a clone URL) — reject it defensively (R9-040).
_COORD_RE = re.compile(r"[A-Za-z0-9._-]+")


@dataclass(frozen=True)
class GithubRepoSpec:
    """One parsed ``owner/repo[@ref]`` bring-your-own GitHub skill-source entry (R9-040).

    Attributes:
        owner: The repo owner (user or org).
        repo: The repo name.
        ref: The pinned ref (branch/tag/commit) to fetch, or ``None`` for the default
            branch HEAD (mirrors :func:`fetch_github_skills`'s ``ref`` parameter).
    """

    owner: str
    repo: str
    ref: str | None = None


def parse_github_repo_specs(value: str) -> list[GithubRepoSpec]:
    """Parse ``PERSONA_SKILL_GITHUB_REPOS`` — a comma list of ``owner/repo[@ref]`` (R9-040).

    Defensive, not fail-fast: this is an operator-typed free-form list (unlike the short,
    curated ``PERSONA_MCP_SERVERS``), so a malformed entry logs a WARNING and is SKIPPED
    rather than raising — one typo must not crash config load or take the whole sync down
    (the D-04-4 warn-and-skip discipline, applied here to config parsing). A blank entry
    (a stray comma / whitespace) is silently ignored — it's empty, not malformed.

    Args:
        value: The raw comma-separated env value (``""`` when unset).

    Returns:
        The valid entries, in encounter order. Not de-duplicated — a repeated
        ``owner/repo`` just re-clones and re-ingests; the mirror reconcile keys on
        skill name, so it is harmless, not an error worth flagging.
    """
    out: list[GithubRepoSpec] = []
    for raw in value.split(","):
        entry = raw.strip()
        if not entry:
            continue
        if entry.count("@") > 1:
            _log.warning(
                "skipping malformed PERSONA_SKILL_GITHUB_REPOS entry (more than one '@')",
                entry=entry,
            )
            continue
        if "@" in entry:
            coord, ref = entry.split("@", 1)
            ref = ref.strip()
            if not ref:
                _log.warning(
                    "skipping malformed PERSONA_SKILL_GITHUB_REPOS entry (empty ref after '@')",
                    entry=entry,
                )
                continue
        else:
            coord, ref = entry, None
        coord = coord.strip()
        if coord.count("/") != 1:
            _log.warning(
                "skipping malformed PERSONA_SKILL_GITHUB_REPOS entry (expected owner/repo)",
                entry=entry,
            )
            continue
        owner, repo = (part.strip() for part in coord.split("/", 1))
        if not (owner and repo and _COORD_RE.fullmatch(owner) and _COORD_RE.fullmatch(repo)):
            _log.warning(
                "skipping malformed PERSONA_SKILL_GITHUB_REPOS entry (invalid owner/repo)",
                entry=entry,
            )
            continue
        out.append(GithubRepoSpec(owner=owner, repo=repo, ref=ref))
    return out


def ingest_github_skills(
    checkout_root: Path,
    *,
    owner: str,
    repo: str,
    commit: str,
    max_files: int | None = None,
) -> list[SkillSpec]:
    """Ingest skills from an arbitrary GitHub checkout at ``third_party`` (hardened discovery).

    Args:
        checkout_root: The (attacker-controlled) checkout root.
        owner: The repo owner — recorded in provenance ``source = "github:<owner>/<repo>"``.
        repo: The repo name.
        commit: The fetched commit — recorded as ``source_ref`` (the re-consent handle).
        max_files: Optional override of the file-count bound (defaults to the hardened default).

    Returns:
        The ingested ``SkillSpec`` list at ``third_party`` (warn-and-skip per skill; unsafe /
        over-bound manifests already excluded by the hardened walk).
    """
    skill_mds = (
        discover_skill_mds_hardened(checkout_root, max_files=max_files)
        if max_files is not None
        else discover_skill_mds_hardened(checkout_root)
    )
    return ingest_external_skills(
        skill_mds,
        trust=SkillTrust.THIRD_PARTY,
        source=f"github:{owner}/{repo}",
        source_uri=f"https://github.com/{owner}/{repo}",
        source_ref=commit,
    )


def fetch_github_skills(
    owner: str,
    repo: str,
    *,
    ref: str | None = None,
    clone_fn: Callable[[str, str | None, Path], SourceCheckout] | None = None,
) -> list[SkillSpec]:
    """Clone ``owner/repo`` into an EPHEMERAL temp dir, ingest at third_party, then clean up.

    The raw attacker-controlled checkout never persists (S2-D-8a property 3): it is cloned
    under a :class:`tempfile.TemporaryDirectory` and removed on exit; only the ingested specs
    (content inline) survive. ``clone_fn`` is a test seam — the default lazily resolves the real
    git clone (:func:`~persona.skills.skill_sources_sync.clone_at_ref`).

    Args:
        owner: The repo owner.
        repo: The repo name.
        ref: The ref to pin the clone to (``None`` ⇒ default branch HEAD).
        clone_fn: Test seam for the clone (default = the real offline git clone).

    Returns:
        The ingested ``third_party`` ``SkillSpec`` list.
    """
    if clone_fn is None:
        from persona.skills.skill_sources_sync import clone_at_ref

        clone_fn = clone_at_ref
    from pathlib import Path

    with tempfile.TemporaryDirectory(prefix="persona-gh-skill-") as tmp:
        dest = Path(tmp) / "clone"
        checkout = clone_fn(f"https://github.com/{owner}/{repo}", ref, dest)
        return ingest_github_skills(checkout.root, owner=owner, repo=repo, commit=checkout.commit)
