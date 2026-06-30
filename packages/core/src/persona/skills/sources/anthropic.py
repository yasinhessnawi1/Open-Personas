"""The Anthropic skill-store adapter — the ``vetted`` tier (Spec S2, B1).

Anthropic is the **consent-bypass tier** (``vetted`` — ``SkillTrust.requires_consent``
is ``False``), so it carries no downstream backstop: the integrity of "vetted"
rests entirely on this adapter proving the source is *genuinely* the canonical
Anthropic skill store. Upstream ``anthropics/skills`` ships no per-skill signing
(S2 research), so authenticity is **pinned provenance, not cryptographic**:

- **The canonical coordinate is hard-coded** (:data:`ANTHROPIC_REPO_URL`) — never
  read from form/config/env (mirrors N4-D-10: anchor the endpoint in code so
  vetting-a-name and the-thing-it-authorizes are the same record).
- **The commit is pinned** (:data:`ANTHROPIC_PINNED_COMMIT`) — an immutable content
  address, *not* a mutable branch that could be force-pushed/compromised.
- **``vetted`` is assigned ONLY conditional on a verified coordinate** — the fetched
  checkout must be the canonical repo AT the pinned commit, or
  :func:`ingest_anthropic_skills` **refuses** (raises
  :class:`~persona.errors.VettedSourceAuthenticityError` — a sync error, never a
  silent downgrade to a consent-gated tier: S2-D-4, fail-closed not fail-quiet).

**S2-D-5 — the pin is a code constant the auto-sync cannot advance.** The pinned
commit lives here, in code, NOT in any config/env/mirror the leader-gated sync
reads or can mutate. The sync supplies the *fetched* HEAD only for verification;
the authority it is checked against — and stamped into provenance — is always the
constant. So a sync can re-fetch the pinned content (idempotent), but it can never
bump the SHA to a future (possibly compromised) commit at ``vetted``: verification
compares against the constant and refuses. Advancing the pin is a deliberate,
reviewed code change (a human diffs old→new pinned trees before the new content
rides at the bypass tier).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.errors import VettedSourceAuthenticityError
from persona.schema.skills import SkillTrust
from persona.skills.sources.discovery import discover_skill_mds
from persona.skills.sources.ingest import ingest_external_skills

if TYPE_CHECKING:
    from pathlib import Path

    from persona.schema.skills import SkillSpec

__all__ = [
    "ANTHROPIC_PINNED_COMMIT",
    "ANTHROPIC_REPO_URL",
    "ANTHROPIC_SOURCE_ID",
    "ANTHROPIC_VETTED_SKILLS",
    "ingest_anthropic_skills",
    "verify_vetted_coordinate",
]

#: The canonical Anthropic skill-store coordinate — hard-coded, never from config
#: (N4-D-10). Vetting binds to this exact repo; a different repo is refused.
ANTHROPIC_REPO_URL = "https://github.com/anthropics/skills"

#: The pinned commit — the vetting record + a CODE CONSTANT the auto-sync cannot
#: advance (S2-D-5). This SHA is itself the vetting act (S2-D-9 / Group E): a real,
#: reviewed commit of ``anthropics/skills`` (``main`` HEAD reviewed 2026-07-01). The
#: authenticity mechanism does not depend on the value, only on it being a fixed code
#: constant — advancing it is a deliberate, reviewed code commit (S2-D-5).
ANTHROPIC_PINNED_COMMIT = "35414756ca55738e050562e272a6bbc6273aa926"

#: The provenance source id recorded on every ingested Anthropic skill.
ANTHROPIC_SOURCE_ID = "anthropic"

#: The curated vetted allowlist (S2-D-9) — a DEFAULT-DENY set: only these skills from the
#: pinned ``anthropics/skills`` tree ingest at ``vetted``. ``vetted`` bypasses consent, so the
#: library is an explicit allowlist, not "whatever the repo ships". Curated at the pinned commit
#: (reviewed 2026-07-01): the instructional, self-contained skills — **excluding** the
#: script-dependent ones (``algorithmic-art``/``docx``/``pdf``/``pptx``/``mcp-builder``/
#: ``skill-creator`` — they degrade to text-only under D2, so they must not ship as
#: "vetted-complete") and any that duplicate our four builtins. Adding a skill here is part of
#: the same reviewed pin-advancement act.
ANTHROPIC_VETTED_SKILLS: frozenset[str] = frozenset(
    {
        "brand-guidelines",
        "canvas-design",
        "claude-api",
        "doc-coauthoring",
        "frontend-design",
        "internal-comms",
    }
)


def _canon_repo(url: str) -> str:
    """Normalise a repo URL for comparison (strip trailing ``/`` and ``.git``)."""
    return url.rstrip("/").removesuffix(".git")


def verify_vetted_coordinate(*, fetched_repo_url: str, fetched_commit: str) -> None:
    """Refuse unless the fetched checkout IS the pinned canonical coordinate (S2-D-4).

    Args:
        fetched_repo_url: The origin URL the checkout was actually fetched from
            (the sync resolves this via ``git remote get-url origin``).
        fetched_commit: The commit the checkout is actually at (the sync resolves
            this via ``git rev-parse HEAD``).

    Raises:
        VettedSourceAuthenticityError: the repo is not the canonical Anthropic
            coordinate, or the checkout is not at the pinned commit. A vetted-source
            failure is a sync error, never a silent downgrade (fail-closed).
    """
    if _canon_repo(fetched_repo_url) != _canon_repo(ANTHROPIC_REPO_URL):
        raise VettedSourceAuthenticityError(
            "refusing to vet: source repo is not the canonical Anthropic coordinate",
            context={"expected": ANTHROPIC_REPO_URL, "fetched": fetched_repo_url},
        )
    if fetched_commit != ANTHROPIC_PINNED_COMMIT:
        raise VettedSourceAuthenticityError(
            "refusing to vet: checkout is not at the pinned commit",
            context={"expected": ANTHROPIC_PINNED_COMMIT, "fetched": fetched_commit},
        )


def ingest_anthropic_skills(
    checkout_root: Path,
    *,
    fetched_repo_url: str,
    fetched_commit: str,
    allowlist: frozenset[str] | None = None,
) -> list[SkillSpec]:
    """Ingest the Anthropic skill store at ``vetted`` — only if provenance verifies.

    The fetched coordinate is verified against the pinned canonical constants
    (S2-D-4) **before** any skill is stamped ``vetted``; a mismatch refuses the
    whole ingest (raises). Provenance is stamped with the pinned constants, not the
    fetched values — so ``vetted`` always means "the pinned canonical content".
    Only skills on the curated **allowlist** (default-deny, S2-D-9) ingest.

    Args:
        checkout_root: The root of a fetched ``anthropics/skills`` checkout.
        fetched_repo_url: The checkout's actual origin URL (for verification).
        fetched_commit: The checkout's actual HEAD commit (for verification).
        allowlist: The curated vetted set (defaults to :data:`ANTHROPIC_VETTED_SKILLS`).
            A test seam; production always uses the curated default.

    Returns:
        The ingested ``SkillSpec`` list at ``vetted`` (warn-and-skip per skill), filtered to
        the curated allowlist.

    Raises:
        VettedSourceAuthenticityError: the fetched coordinate is not the pinned
            canonical one (S2-D-4 / S2-D-5).
    """
    verify_vetted_coordinate(fetched_repo_url=fetched_repo_url, fetched_commit=fetched_commit)
    vetted = ANTHROPIC_VETTED_SKILLS if allowlist is None else allowlist
    skill_mds = [m for m in discover_skill_mds(checkout_root) if m.parent.name in vetted]
    return ingest_external_skills(
        skill_mds,
        trust=SkillTrust.VETTED,
        source=ANTHROPIC_SOURCE_ID,
        source_uri=ANTHROPIC_REPO_URL,
        source_ref=ANTHROPIC_PINNED_COMMIT,
    )
