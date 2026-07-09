"""Tests for the file-on-volume skill mirror loader (Spec S2, C1).

The mirror is a snapshot of externally-sourced skills, parallel to N2's
``mirror.json``: written atomically by the offline/worker sync, read zero-network
and **fail-soft** on the request path — an absent / unreadable / corrupt snapshot
NEVER raises at boot (it degrades to an empty external set; builtins are
unaffected). Trust + provenance round-trip from the snapshot (the loader
reattaches them — S2-D-X-scanner-paths), never re-derived from front matter.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used in fixture helpers at runtime

from persona.schema.skills import SkillProvenance, SkillSpec, SkillTrust
from persona.skills.skill_mirror import (
    SKILL_MIRROR_PATH,
    load_skill_mirror,
    resolve_skill_mirror_read_path,
    resolve_skill_mirror_write_path,
    write_skill_mirror_atomic,
)


def _spec(name: str, *, trust: SkillTrust, body: str = "Body.") -> SkillSpec:
    return SkillSpec(
        name=name,
        description=f"{name} description.",
        path=Path("/unused/at/write/time") / name,
        content=body,
        content_token_count=3,
        trust=trust,
        provenance=SkillProvenance(
            source="anthropic", source_ref="pinned", content_hash="h" + name
        ),
    )


def test_round_trip_preserves_trust_provenance_and_rebases_path(tmp_path: Path) -> None:
    mirror_path = tmp_path / "skill_mirror.json"
    write_skill_mirror_atomic(
        [_spec("pdf", trust=SkillTrust.VETTED), _spec("weather", trust=SkillTrust.COMMUNITY)],
        mirror_path,
    )

    specs = load_skill_mirror(mirror_path)

    by_name = {s.name: s for s in specs}
    assert set(by_name) == {"pdf", "weather"}
    # Trust + provenance survive the round-trip (reattached from the snapshot).
    assert by_name["pdf"].trust is SkillTrust.VETTED
    assert by_name["weather"].trust is SkillTrust.COMMUNITY
    assert by_name["pdf"].provenance is not None
    assert by_name["pdf"].provenance.source == "anthropic"
    assert by_name["pdf"].provenance.content_hash == "hpdf"
    # Path is rebased to the mirror dir on load (robust to where it was written).
    assert by_name["pdf"].path == mirror_path.parent / "pdf"


def test_absent_snapshot_is_failsoft_empty(tmp_path: Path) -> None:
    assert load_skill_mirror(tmp_path / "does_not_exist.json") == []


def test_corrupt_snapshot_is_failsoft_empty(tmp_path: Path) -> None:
    bad = tmp_path / "skill_mirror.json"
    bad.write_text("{ this is not valid json", encoding="utf-8")
    # Never raises at boot — degrades to empty external set.
    assert load_skill_mirror(bad) == []


def test_atomic_write_leaves_no_temp_file(tmp_path: Path) -> None:
    mirror_path = tmp_path / "skill_mirror.json"
    write_skill_mirror_atomic([_spec("pdf", trust=SkillTrust.VETTED)], mirror_path)
    # Only the snapshot remains; no ``.tmp`` sibling left behind.
    assert mirror_path.exists()
    assert [p.name for p in tmp_path.iterdir()] == ["skill_mirror.json"]


def test_resolve_read_path_prefers_override_else_bundled(tmp_path: Path) -> None:
    override = tmp_path / "vol" / "skill_mirror.json"
    assert resolve_skill_mirror_read_path(override) == override
    # LOADS may fall back to the bundled snapshot — reading package data is safe.
    assert resolve_skill_mirror_read_path(None) == SKILL_MIRROR_PATH


def test_resolve_write_path_never_falls_back_to_bundled_file(tmp_path: Path) -> None:
    """R9-011 regression pin: with no override, the WRITE path is ``None`` — never the
    bundled package-data ``SKILL_MIRROR_PATH`` (a sync would rewrite a committed file)."""
    override = tmp_path / "vol" / "skill_mirror.json"
    assert resolve_skill_mirror_write_path(override) == override
    resolved = resolve_skill_mirror_write_path(None)
    assert resolved is None
    assert resolved != SKILL_MIRROR_PATH
