"""The OpenClaw community skill adapter — the ``community`` tier (Spec S2, B2).

OpenClaw skills ingest at ``community`` (``SkillTrust.requires_consent`` is
``True``), so unlike the Anthropic ``vetted`` path (B1) this tier is **gated
downstream by S3's consent** — that gate is its backstop, so it needs no pinned
authenticity refusal. The adapter assigns ``community`` + provenance and reuses
the shared ingest (S2-D-2/D-3 — trust source-assigned, never self-declared).

**S2-D-10 — legacy casing.** OpenClaw historically published manifests as
``SKILL.md`` *or* the legacy ``skill.md`` / ``skills.md``; discovery accepts all
three (``accept_legacy_casing=True``), normalizing to canonical ``SKILL.md`` in
the mirror (Group C). The runtime scanner's ``SKILL.md``-only ``_locate`` stays
unchanged because the mirror only ever holds canonical names.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from persona.schema.skills import SkillTrust
from persona.skills.sources.discovery import discover_skill_mds
from persona.skills.sources.ingest import ingest_external_skills

if TYPE_CHECKING:
    from pathlib import Path

    from persona.schema.skills import SkillSpec

__all__ = ["OPENCLAW_SOURCE_ID", "ingest_openclaw_skills"]

#: The provenance source id recorded on every ingested OpenClaw skill.
OPENCLAW_SOURCE_ID = "openclaw"


def ingest_openclaw_skills(
    checkout_root: Path,
    *,
    source_uri: str | None = None,
    source_ref: str | None = None,
) -> list[SkillSpec]:
    """Ingest OpenClaw community skills from a fetched checkout at ``community``.

    No authenticity pin (community is consent-gated downstream); provenance carries
    the source coordinate + the body ``content_hash`` (S1-D-5) so a content sync
    re-gates through S3's store.

    Args:
        checkout_root: The root of a fetched OpenClaw skills checkout.
        source_uri: The source URL recorded in provenance.
        source_ref: The source ref (commit / catalog version) recorded in
            provenance.

    Returns:
        The ingested ``SkillSpec`` list at ``community`` (warn-and-skip per skill).
    """
    skill_mds = discover_skill_mds(checkout_root, accept_legacy_casing=True)
    return ingest_external_skills(
        skill_mds,
        trust=SkillTrust.COMMUNITY,
        source=OPENCLAW_SOURCE_ID,
        source_uri=source_uri,
        source_ref=source_ref,
    )
