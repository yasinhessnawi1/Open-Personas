"""Tests for the skill-sources fetch/aggregation layer (Spec S2, C1).

The sync aggregates the enabled per-source adapters into one ``SkillSpec`` list
(Anthropic=vetted ⊕ OpenClaw=community), then reconciles it into the mirror — the
one structural difference from N2 (S2-D-1: build-new fans out across sources;
reconcile/loader unchanged). Tested with injected local checkouts (no network).
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used in fixture helpers at runtime

from persona.schema.skills import SkillTrust
from persona.skills.skill_mirror import load_skill_mirror
from persona.skills.skill_sources_sync import (
    SourceCheckout,
    build_curated_source_specs,
    sync_skill_mirror,
)
from persona.skills.sources.anthropic import ANTHROPIC_PINNED_COMMIT, ANTHROPIC_REPO_URL

_SKILL = """\
---
name: {name}
description: {name} does a thing. Use when relevant.
---
Body for {name}.
"""


def _make_checkout(base: Path, label: str, *names: str) -> Path:
    root = base / label
    for name in names:
        d = root / name
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(_SKILL.format(name=name), "utf-8")
    return root


def _anthropic(base: Path) -> SourceCheckout:
    return SourceCheckout(
        root=_make_checkout(base, "anthropic", "claude-api"),
        repo_url=ANTHROPIC_REPO_URL,
        commit=ANTHROPIC_PINNED_COMMIT,
    )


def _openclaw(base: Path) -> SourceCheckout:
    return SourceCheckout(
        root=_make_checkout(base, "openclaw", "weather"),
        repo_url="https://github.com/openclaw/clawhub",
        commit="oc123",
    )


def test_aggregates_both_sources_with_correct_tiers(tmp_path: Path) -> None:
    specs = build_curated_source_specs(anthropic=_anthropic(tmp_path), openclaw=_openclaw(tmp_path))
    by_name = {s.name: s for s in specs}
    assert by_name["claude-api"].trust is SkillTrust.VETTED
    assert by_name["weather"].trust is SkillTrust.COMMUNITY
    assert by_name["claude-api"].provenance is not None
    assert by_name["claude-api"].provenance.source == "anthropic"
    assert by_name["weather"].provenance is not None
    assert by_name["weather"].provenance.source == "openclaw"


def test_a_disabled_source_just_contributes_nothing(tmp_path: Path) -> None:
    specs = build_curated_source_specs(anthropic=_anthropic(tmp_path), openclaw=None)
    assert {s.name for s in specs} == {"claude-api"}


def test_sync_writes_the_mirror_and_reports_the_diff(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror" / "skill_mirror.json"
    result = sync_skill_mirror(
        mirror_path=mirror,
        anthropic=_anthropic(tmp_path),
        openclaw=_openclaw(tmp_path),
    )
    assert set(result.added) == {"claude-api", "weather"}
    assert {s.name for s in load_skill_mirror(mirror)} == {"claude-api", "weather"}


class TestMaterialization:
    """D2: the sync materializes normalized skill trees (supplements) + cleans removals."""

    def _openclaw_with_refs(self, base: Path) -> SourceCheckout:
        root = base / "oc"
        d = root / "weather"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(_SKILL.format(name="weather"), "utf-8")
        (d / "references").mkdir()
        (d / "references" / "api.md").write_text("# api notes\n", "utf-8")
        (d / "scripts").mkdir()
        (d / "scripts" / "fetch.sh").write_text("# dropped\n", "utf-8")
        return SourceCheckout(
            root=root, repo_url="https://github.com/openclaw/clawhub", commit="oc1"
        )

    def test_sync_materializes_supplements_and_drops_scripts(self, tmp_path: Path) -> None:
        mirror = tmp_path / "mirror" / "skill_mirror.json"
        sync_skill_mirror(
            mirror_path=mirror, anthropic=None, openclaw=self._openclaw_with_refs(tmp_path)
        )
        skill_dir = mirror.parent / "weather"
        # references/api.md folded into supplements/; scripts/ dropped (text-only boundary).
        assert (skill_dir / "supplements" / "api.md").is_file()
        assert not (skill_dir / "scripts").exists()

    def test_sync_cleans_a_removed_skills_tree(self, tmp_path: Path) -> None:
        mirror = tmp_path / "mirror" / "skill_mirror.json"
        sync_skill_mirror(
            mirror_path=mirror, anthropic=None, openclaw=self._openclaw_with_refs(tmp_path)
        )
        assert (mirror.parent / "weather").is_dir()
        # A re-sync with the skill gone removes its materialized tree (graceful degradation).
        sync_skill_mirror(mirror_path=mirror, anthropic=None, openclaw=None)
        assert not (mirror.parent / "weather").exists()
        assert {s.name for s in load_skill_mirror(mirror)} == set()
