"""B2 — the OpenClaw community adapter (Spec S2).

OpenClaw ingests at ``community`` (consent-gated downstream by S3, so bounded
blast radius — scaffolding-grade, not the B1 carve-out). Two B2 specifics:

- it stamps ``community`` (never ``vetted``) and carries provenance, but needs
  **no pinned-authenticity refusal** (the consent gate is its backstop);
- **S2-D-10**: legacy ``skill.md`` / ``skills.md`` casing is accepted at ingest
  (and normalized to canonical ``SKILL.md`` in the mirror, Group C).
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used in fixture helpers at runtime

from persona.schema.skills import SkillTrust
from persona.skills.sources.openclaw import OPENCLAW_SOURCE_ID, ingest_openclaw_skills

_VALID_SKILL = """\
---
name: weather_lookup
description: Look up the weather. Use when the user asks about weather.
---
Weather instructions.
"""


def _make_skill(root: Path, name: str, *, manifest: str = "SKILL.md") -> None:
    """Create ``root/<name>/<manifest>`` (manifest casing varies for S2-D-10)."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / manifest).write_text(_VALID_SKILL.replace("weather_lookup", name), "utf-8")


def test_openclaw_ingests_at_community_with_provenance(tmp_path: Path) -> None:
    root = tmp_path / "checkout"
    _make_skill(root, "weather_lookup")

    specs = ingest_openclaw_skills(
        root,
        source_uri="https://github.com/openclaw/clawhub",
        source_ref="commitabc",
    )

    assert [s.name for s in specs] == ["weather_lookup"]
    spec = specs[0]
    # community, never vetted — the gate behind it is the backstop.
    assert spec.trust is SkillTrust.COMMUNITY
    assert spec.provenance is not None
    assert spec.provenance.source == OPENCLAW_SOURCE_ID
    assert spec.provenance.source_ref == "commitabc"


def test_openclaw_accepts_legacy_skill_md_casing(tmp_path: Path) -> None:
    """S2-D-10: a skill published with legacy ``skill.md`` casing still ingests."""
    root = tmp_path / "checkout"
    _make_skill(root, "legacy_skill", manifest="skill.md")

    specs = ingest_openclaw_skills(root)

    assert [s.name for s in specs] == ["legacy_skill"]
    assert specs[0].trust is SkillTrust.COMMUNITY
