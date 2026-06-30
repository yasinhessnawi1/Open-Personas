"""External skill sources (Spec S2) — per-source adapters + native ingest.

S2 sources skills from external catalogs (the Anthropic skill store, OpenClaw
community, arbitrary GitHub repos) into the skill catalog, each tagged with the
S1 trust tier + provenance. The on-disk format is already the Anthropic
``SKILL.md`` format, so ingest is **native** (validate + tier + mirror; no
conversion).

:func:`ingest_external_skill` is the shared native-ingest seam (A1): it reuses
the scanner's front-matter parse + ``SkillSpec`` validation, but assigns trust +
provenance **at the source boundary** — never reading a self-declared tier from
author-controlled front matter (S2-D-3 / S1-D-3, the rule the whole tier model
rests on).
"""

from __future__ import annotations

from persona.skills.sources.anthropic import (
    ANTHROPIC_PINNED_COMMIT,
    ANTHROPIC_REPO_URL,
    ANTHROPIC_SOURCE_ID,
    ingest_anthropic_skills,
    verify_vetted_coordinate,
)
from persona.skills.sources.discovery import discover_skill_mds, discover_skill_mds_hardened
from persona.skills.sources.github import fetch_github_skills, ingest_github_skills
from persona.skills.sources.ingest import ingest_external_skill, ingest_external_skills
from persona.skills.sources.openclaw import OPENCLAW_SOURCE_ID, ingest_openclaw_skills

__all__ = [
    "ANTHROPIC_PINNED_COMMIT",
    "ANTHROPIC_REPO_URL",
    "ANTHROPIC_SOURCE_ID",
    "OPENCLAW_SOURCE_ID",
    "discover_skill_mds",
    "discover_skill_mds_hardened",
    "fetch_github_skills",
    "ingest_anthropic_skills",
    "ingest_external_skill",
    "ingest_external_skills",
    "ingest_github_skills",
    "ingest_openclaw_skills",
    "verify_vetted_coordinate",
]
