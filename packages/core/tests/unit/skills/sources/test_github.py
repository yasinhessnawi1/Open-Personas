"""D1 — the arbitrary-GitHub adapter (third_party) + the S2-D-8a ingest-act hardening.

D1 is the single riskiest task in S2: the one path that runs ``git`` against an
**attacker-controlled URL** and walks **attacker-controlled content**. The tier/consent
model governs what an installed skill can *do*; it does NOT cover the ingest act itself.
So the fetch is hardened with three properties, tested here as a **security contract**
(the same scrutiny as B1's authenticity), not as scaffolding:

1. **No symlink-escape** — the walk neither follows a directory symlink out of the clone
   nor reads a manifest symlinked outside the clone root (no exfiltration of ``/etc`` or
   another owner's volume).
2. **Resource bounds** — file-count + per-file + total size are bounded, so a giant /
   zip-bomb-style repo can't exhaust the volume; over-bound ⇒ skip-with-reason.
3. **Ephemeral, cleaned clone** — clone to a temp dir, extract only the normalized ``.md``,
   then remove the clone; the raw attacker checkout never persists.

Defense-in-depth: even a malicious arbitrary-GitHub skill is safe because it is
``third_party`` (consent-gated by S1 downstream), the ingest-act is hardened (the three
above), and D2 drops executables — each layer independent.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used in fixture helpers at runtime

from loguru import logger
from persona.schema.skills import SkillTrust
from persona.skills.sources.discovery import discover_skill_mds_hardened
from persona.skills.sources.github import (
    GithubRepoSpec,
    fetch_github_skills,
    ingest_github_skills,
    parse_github_repo_specs,
)

_SKILL = """\
---
name: {name}
description: {name} does a thing. Use when relevant.
---
Body for {name}.
"""


def _skill_dir(root: Path, name: str, body: str | None = None) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(body if body is not None else _SKILL.format(name=name), "utf-8")
    return d


# --- positive control -------------------------------------------------------


def test_in_bounds_non_symlink_skill_is_discovered_and_third_party(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    _skill_dir(clone, "gh_skill")
    specs = ingest_github_skills(clone, owner="someone", repo="theirrepo", commit="abc123")
    assert [s.name for s in specs] == ["gh_skill"]
    spec = specs[0]
    assert spec.trust is SkillTrust.THIRD_PARTY  # lowest tier; S1 consent gates it
    assert spec.provenance is not None
    assert spec.provenance.source == "github:someone/theirrepo"
    assert spec.provenance.source_ref == "abc123"


# --- S2-D-8a property 1: no symlink-escape ----------------------------------


def test_a_manifest_symlinked_outside_the_clone_is_refused(tmp_path: Path) -> None:
    secret = tmp_path / "outside" / "secret.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("---\nname: x\ndescription: stolen secret.\n---\nSECRET\n", "utf-8")
    clone = tmp_path / "clone"
    evil = clone / "evil"
    evil.mkdir(parents=True)
    # A SKILL.md that is a symlink pointing OUT of the clone root.
    (evil / "SKILL.md").symlink_to(secret)

    found = discover_skill_mds_hardened(clone)

    assert found == []  # the symlinked manifest is refused, secret not read


def test_a_directory_symlink_out_of_the_clone_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _skill_dir(outside, "sneaky")  # outside/sneaky/SKILL.md
    clone = tmp_path / "clone"
    clone.mkdir()
    _skill_dir(clone, "legit")  # a real in-clone skill
    (clone / "linked").symlink_to(outside)  # a dir symlink escaping the clone

    found = discover_skill_mds_hardened(clone)

    names = {p.parent.name for p in found}
    assert names == {"legit"}  # the symlinked dir is not descended; "sneaky" never reached


# --- S2-D-8a property 2: resource bounds ------------------------------------


def test_file_count_bound_stops_after_the_cap(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    for i in range(5):
        _skill_dir(clone, f"s{i}")
    found = discover_skill_mds_hardened(clone, max_files=2)
    assert len(found) == 2


def test_oversize_manifest_is_skipped(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    _skill_dir(clone, "small")
    big_body = "---\nname: big\ndescription: huge.\n---\n" + ("x" * 5000)
    _skill_dir(clone, "big", body=big_body)

    found = discover_skill_mds_hardened(clone, max_file_bytes=1024)

    assert {p.parent.name for p in found} == {"small"}


def test_total_size_bound_stops_collecting(tmp_path: Path) -> None:
    clone = tmp_path / "clone"
    body = "---\nname: n\ndescription: d.\n---\n" + ("y" * 400)
    for i in range(5):
        _skill_dir(clone, f"s{i}", body=body.replace("name: n", f"name: s{i}"))
    # Each manifest ~430 bytes; a 1000-byte total budget admits ~2 then stops.
    found = discover_skill_mds_hardened(clone, max_total_bytes=1000)
    assert 0 < len(found) < 5


# --- S2-D-8a property 3: ephemeral, cleaned clone ---------------------------


def test_clone_dir_is_ephemeral_and_removed_after_fetch() -> None:
    captured: dict[str, Path] = {}

    def _fake_clone(repo_url: str, _ref: str | None, dest: Path) -> object:
        # Simulate the clone populating the dest dir, then record it.
        from persona.skills.skill_sources_sync import SourceCheckout

        _skill_dir(dest, "gh_skill")
        captured["dest"] = dest
        return SourceCheckout(root=dest, repo_url=repo_url, commit="sha999")

    specs = fetch_github_skills("o", "r", clone_fn=_fake_clone)

    assert [s.name for s in specs] == ["gh_skill"]  # specs returned (content inline)
    # The raw attacker-controlled checkout never persists.
    assert "dest" in captured
    assert not captured["dest"].exists()


def test_a_skipped_manifest_warns_with_a_reason(tmp_path: Path) -> None:
    secret = tmp_path / "outside" / "secret.md"
    secret.parent.mkdir(parents=True)
    secret.write_text("data", "utf-8")
    clone = tmp_path / "clone"
    (clone / "evil").mkdir(parents=True)
    (clone / "evil" / "SKILL.md").symlink_to(secret)

    captured: list[str] = []
    sink_id = logger.add(captured.append, level="WARNING", format="{message} | {extra}")
    try:
        discover_skill_mds_hardened(clone)
    finally:
        logger.remove(sink_id)

    blob = "".join(captured)
    assert "symlink" in blob.lower()


# --- parse_github_repo_specs — PERSONA_SKILL_GITHUB_REPOS config parsing (R9-040) -----


def test_empty_string_yields_no_repos() -> None:
    assert parse_github_repo_specs("") == []


def test_whitespace_only_yields_no_repos() -> None:
    assert parse_github_repo_specs("   ") == []


def test_a_single_owner_repo_with_no_ref() -> None:
    assert parse_github_repo_specs("acme/skills") == [
        GithubRepoSpec(owner="acme", repo="skills", ref=None)
    ]


def test_a_single_owner_repo_with_a_ref() -> None:
    assert parse_github_repo_specs("acme/skills@v1.2.3") == [
        GithubRepoSpec(owner="acme", repo="skills", ref="v1.2.3")
    ]


def test_multiple_entries_all_parse_in_order() -> None:
    assert parse_github_repo_specs("acme/skills,other-org/repo2@main") == [
        GithubRepoSpec(owner="acme", repo="skills", ref=None),
        GithubRepoSpec(owner="other-org", repo="repo2", ref="main"),
    ]


def test_surrounding_and_internal_whitespace_is_tolerated() -> None:
    assert parse_github_repo_specs(" acme/skills , other/repo2 @ main ") == [
        GithubRepoSpec(owner="acme", repo="skills", ref=None),
        GithubRepoSpec(owner="other", repo="repo2", ref="main"),
    ]


def test_blank_entries_from_stray_commas_are_ignored_not_malformed() -> None:
    assert parse_github_repo_specs(",,acme/skills,,") == [
        GithubRepoSpec(owner="acme", repo="skills", ref=None)
    ]


def test_a_ref_may_itself_contain_slashes() -> None:
    # git refs like "release/1.0" are legitimate; only the owner/repo coordinate is
    # restricted to exactly one '/'.
    assert parse_github_repo_specs("acme/skills@release/1.0") == [
        GithubRepoSpec(owner="acme", repo="skills", ref="release/1.0")
    ]


def test_missing_slash_is_malformed_and_skipped() -> None:
    assert parse_github_repo_specs("ownerrepo") == []


def test_too_many_slashes_is_malformed_and_skipped() -> None:
    assert parse_github_repo_specs("a/b/c") == []


def test_empty_owner_is_malformed_and_skipped() -> None:
    assert parse_github_repo_specs("/repo") == []


def test_empty_repo_is_malformed_and_skipped() -> None:
    assert parse_github_repo_specs("owner/") == []


def test_empty_ref_after_at_is_malformed_and_skipped() -> None:
    assert parse_github_repo_specs("owner/repo@") == []


def test_multiple_at_signs_is_malformed_and_skipped() -> None:
    assert parse_github_repo_specs("owner/repo@ref@extra") == []


def test_a_space_inside_owner_or_repo_is_malformed_and_skipped() -> None:
    assert parse_github_repo_specs("own er/repo") == []


def test_one_bad_entry_does_not_drop_the_good_ones_around_it() -> None:
    # Defensive parsing: a single typo must not take down the whole configured list.
    assert parse_github_repo_specs("acme/skills,not-valid,other/repo2@v1") == [
        GithubRepoSpec(owner="acme", repo="skills", ref=None),
        GithubRepoSpec(owner="other", repo="repo2", ref="v1"),
    ]


def test_malformed_entries_warn_with_a_reason_never_raise() -> None:
    captured: list[str] = []
    sink_id = logger.add(captured.append, level="WARNING", format="{message} | {extra}")
    try:
        result = parse_github_repo_specs("ownerrepo")
    finally:
        logger.remove(sink_id)

    assert result == []
    blob = "".join(captured)
    assert "PERSONA_SKILL_GITHUB_REPOS" in blob
    assert "ownerrepo" in blob
