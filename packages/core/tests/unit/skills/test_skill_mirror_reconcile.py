"""Tests for the skill-mirror reconcile (Spec S2, C1) — the N2-shaped diff/write.

Reconcile keeps the mirror fresh: load old → diff added/updated/removed → atomic
write. ``updated`` keys on the body ``content_hash`` (S1-D-5 — the re-consent
trigger, so a body change is exactly what counts as an update). A **removed** skill
drops from the snapshot (never offered as available) but is **surfaced** in the
result (observability, warn-and-skip-on-remove). Idempotent: same specs ⇒ all-zero
diff + byte-identical file.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used in fixture helpers at runtime

from persona.schema.skills import SkillProvenance, SkillSpec, SkillTrust
from persona.skills.skill_mirror import load_skill_mirror
from persona.skills.skill_mirror_reconcile import reconcile_skill_mirror


def _spec(name: str, *, content_hash: str, body: str = "Body.") -> SkillSpec:
    return SkillSpec(
        name=name,
        description=f"{name} description.",
        path=Path("/unused") / name,
        content=body,
        content_token_count=3,
        trust=SkillTrust.COMMUNITY,
        provenance=SkillProvenance(source="openclaw", content_hash=content_hash),
    )


def test_first_sync_is_all_added(tmp_path: Path) -> None:
    mirror = tmp_path / "skill_mirror.json"
    result = reconcile_skill_mirror(
        [_spec("a", content_hash="h1"), _spec("b", content_hash="h2")], mirror_path=mirror
    )
    assert result.added == ("a", "b")
    assert result.updated == ()
    assert result.removed == ()
    assert result.total == 2
    assert {s.name for s in load_skill_mirror(mirror)} == {"a", "b"}


def test_resync_unchanged_is_idempotent_zero_diff_and_byte_identical(tmp_path: Path) -> None:
    mirror = tmp_path / "skill_mirror.json"
    specs = [_spec("a", content_hash="h1"), _spec("b", content_hash="h2")]
    reconcile_skill_mirror(specs, mirror_path=mirror)
    first_bytes = mirror.read_bytes()

    result = reconcile_skill_mirror(specs, mirror_path=mirror)

    assert result.added == ()
    assert result.updated == ()
    assert result.removed == ()
    assert mirror.read_bytes() == first_bytes  # byte-identical (sort_keys)


def test_body_change_counts_as_updated(tmp_path: Path) -> None:
    mirror = tmp_path / "skill_mirror.json"
    reconcile_skill_mirror([_spec("a", content_hash="h1")], mirror_path=mirror)

    result = reconcile_skill_mirror([_spec("a", content_hash="h2_changed")], mirror_path=mirror)

    assert result.updated == ("a",)
    assert result.added == ()
    assert result.removed == ()


def test_removed_skill_drops_from_snapshot_but_is_surfaced(tmp_path: Path) -> None:
    mirror = tmp_path / "skill_mirror.json"
    reconcile_skill_mirror(
        [_spec("a", content_hash="h1"), _spec("gone", content_hash="h2")], mirror_path=mirror
    )

    result = reconcile_skill_mirror([_spec("a", content_hash="h1")], mirror_path=mirror)

    # Surfaced (observability), never silent — and dropped from the snapshot so it is
    # never offered as available; a persona using it degrades downstream (warn-and-skip).
    assert result.removed == ("gone",)
    assert {s.name for s in load_skill_mirror(mirror)} == {"a"}
