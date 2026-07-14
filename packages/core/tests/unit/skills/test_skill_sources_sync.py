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
    GithubSourceCheckout,
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


def _github(
    base: Path,
    *,
    label: str = "github",
    owner: str = "someone",
    repo: str = "theirrepo",
    skill_name: str = "gh_skill",
) -> GithubSourceCheckout:
    return GithubSourceCheckout(
        owner=owner,
        repo=repo,
        checkout=SourceCheckout(
            root=_make_checkout(base, label, skill_name),
            repo_url=f"https://github.com/{owner}/{repo}",
            commit="ghsha1",
        ),
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


# --- R9-040: the arbitrary-GitHub BYO repos fan in via the EXISTING hardened D1 -------
# adapter (ingest_github_skills), additively alongside the curated sources. ------------


def test_a_github_repo_ingests_at_third_party_tier(tmp_path: Path) -> None:
    specs = build_curated_source_specs(anthropic=None, openclaw=None, github=[_github(tmp_path)])
    assert [s.name for s in specs] == ["gh_skill"]
    spec = specs[0]
    assert spec.trust is SkillTrust.THIRD_PARTY  # lowest tier; S1 consent gates it downstream
    assert spec.provenance is not None
    assert spec.provenance.source == "github:someone/theirrepo"
    assert spec.provenance.source_ref == "ghsha1"


def test_no_github_repos_is_the_default_and_contributes_nothing(tmp_path: Path) -> None:
    # The default `github=()` must not change existing anthropic/openclaw-only behavior.
    specs = build_curated_source_specs(anthropic=_anthropic(tmp_path), openclaw=None)
    assert {s.name for s in specs} == {"claude-api"}


def test_github_composes_additively_with_the_curated_sources(tmp_path: Path) -> None:
    specs = build_curated_source_specs(
        anthropic=_anthropic(tmp_path),
        openclaw=_openclaw(tmp_path),
        github=[_github(tmp_path)],
    )
    by_name = {s.name: s for s in specs}
    assert set(by_name) == {"claude-api", "weather", "gh_skill"}
    assert by_name["claude-api"].trust is SkillTrust.VETTED
    assert by_name["weather"].trust is SkillTrust.COMMUNITY
    assert by_name["gh_skill"].trust is SkillTrust.THIRD_PARTY


def test_multiple_github_repos_all_ingest_each_with_its_own_provenance(
    tmp_path: Path,
) -> None:
    first = _github(tmp_path, label="gh1", owner="acme", repo="skills-one", skill_name="s_one")
    second = _github(tmp_path, label="gh2", owner="other", repo="skills-two", skill_name="s_two")
    specs = build_curated_source_specs(anthropic=None, openclaw=None, github=[first, second])
    by_name = {s.name: s for s in specs}
    assert set(by_name) == {"s_one", "s_two"}
    assert by_name["s_one"].provenance is not None
    assert by_name["s_one"].provenance.source == "github:acme/skills-one"
    assert by_name["s_two"].provenance is not None
    assert by_name["s_two"].provenance.source == "github:other/skills-two"


def test_sync_writes_github_skills_into_the_mirror(tmp_path: Path) -> None:
    mirror = tmp_path / "mirror" / "skill_mirror.json"
    result = sync_skill_mirror(
        mirror_path=mirror, anthropic=None, openclaw=None, github=[_github(tmp_path)]
    )
    assert set(result.added) == {"gh_skill"}
    mirrored = {s.name: s for s in load_skill_mirror(mirror)}
    assert mirrored["gh_skill"].trust is SkillTrust.THIRD_PARTY
