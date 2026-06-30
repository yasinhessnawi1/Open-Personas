"""Tests for declared-mirror-skill resolution (Spec S2, C1) — availability ≠ enablement.

The mirror makes external skills *available*; a persona only loads the ones it
**declared** (the N2 boundary, S2-R-3). This pure helper selects, from the mirror,
the specs for declared names not already resolved as builtins — and crucially
returns **nothing** for a mirror skill the persona did not declare.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used in fixture helpers at runtime

from persona.schema.skills import SkillProvenance, SkillSpec, SkillTrust
from persona.skills.skill_mirror import declared_mirror_skills


def _spec(name: str, trust: SkillTrust = SkillTrust.COMMUNITY) -> SkillSpec:
    return SkillSpec(
        name=name,
        description=f"{name}.",
        path=Path("/m") / name,
        content="x",
        content_token_count=1,
        trust=trust,
        provenance=SkillProvenance(source="openclaw", content_hash=name),
    )


def test_declared_external_skill_is_selected_from_the_mirror() -> None:
    mirror = [_spec("ext_a", SkillTrust.VETTED), _spec("ext_b")]
    out = declared_mirror_skills(["ext_a"], resolved_names=set(), mirror_specs=mirror)
    assert [s.name for s in out] == ["ext_a"]
    assert out[0].trust is SkillTrust.VETTED  # mirror trust carried through


def test_undeclared_mirror_skill_is_never_loaded() -> None:
    """Availability ≠ enablement: ext_b is in the mirror but not declared ⇒ not loaded."""
    mirror = [_spec("ext_a"), _spec("ext_b")]
    out = declared_mirror_skills(["ext_a"], resolved_names=set(), mirror_specs=mirror)
    assert [s.name for s in out] == ["ext_a"]
    assert "ext_b" not in {s.name for s in out}


def test_already_resolved_builtin_is_not_duplicated_from_mirror() -> None:
    mirror = [_spec("shadowed")]
    out = declared_mirror_skills(["shadowed"], resolved_names={"shadowed"}, mirror_specs=mirror)
    assert out == []


def test_declared_name_absent_from_mirror_is_skipped() -> None:
    out = declared_mirror_skills(["nope"], resolved_names=set(), mirror_specs=[_spec("ext_a")])
    assert out == []


def test_no_duplicate_when_declared_twice() -> None:
    mirror = [_spec("ext_a")]
    out = declared_mirror_skills(["ext_a", "ext_a"], resolved_names=set(), mirror_specs=mirror)
    assert [s.name for s in out] == ["ext_a"]
