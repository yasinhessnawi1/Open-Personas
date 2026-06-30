"""D2 — fetch-time skill normalization into the mirror (Spec S2, S2-D-6/D-7).

The no-widening answer: at fetch, an external skill's ``references/*.md`` (text) are folded
into the mirror skill's ``supplements/`` so the runtime's EXISTING ``collect_skill_supplements``
covers them **byte-unchanged** (same ingress kind, same SandboxFile transport, no new boundary).
``scripts/`` and executable/binary files are **dropped at fetch** — never written to the mirror
(staging untrusted executables is the widening the steer forbade; the boundary stays
text-``.md``-only). Drops are **logged with a reason** (S2-D-7) so a curator sees a skill was
partially ingested (text-only) and it never presents as fully-featured.
"""

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used in fixture helpers at runtime

from loguru import logger
from persona.schema.skills import SkillProvenance, SkillSpec, SkillTrust
from persona.skills.sources.normalize import normalize_skill_into_mirror
from persona.skills.use_skill_tool import collect_skill_supplements

_SKILL = """\
---
name: gh_skill
description: Does a thing. Use when relevant.
---
Body.
"""


def _source_skill(base: Path) -> Path:
    """A source checkout skill dir using the Agent-Skills optional dirs."""
    d = base / "gh_skill"
    (d).mkdir(parents=True)
    (d / "SKILL.md").write_text(_SKILL, "utf-8")
    (d / "references").mkdir()
    (d / "references" / "deep.md").write_text("# deep reference\n", "utf-8")
    (d / "scripts").mkdir()
    # Inert fixture text standing in for an untrusted executable that must be DROPPED
    # (never executed here; the normalization only ever copies text .md, never runs code).
    (d / "scripts" / "run.py").write_text("# untrusted script body — must be dropped\n", "utf-8")
    (d / "assets").mkdir()
    (d / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n")
    return d


# --- property 1: references/*.md → supplements/ (no new ingress) -------------


def test_references_md_are_folded_into_supplements(tmp_path: Path) -> None:
    source = _source_skill(tmp_path / "src")
    dest = tmp_path / "mirror" / "gh_skill"

    normalize_skill_into_mirror(source, dest)

    assert (dest / "SKILL.md").is_file()
    # references/deep.md becomes supplements/deep.md — the existing boundary covers it.
    assert (dest / "supplements" / "deep.md").is_file()
    assert (dest / "supplements" / "deep.md").read_text("utf-8") == "# deep reference\n"


def test_existing_supplements_md_are_preserved(tmp_path: Path) -> None:
    source = _source_skill(tmp_path / "src")
    (source / "supplements").mkdir()
    (source / "supplements" / "extra.md").write_text("extra\n", "utf-8")
    dest = tmp_path / "mirror" / "gh_skill"

    normalize_skill_into_mirror(source, dest)

    assert (dest / "supplements" / "extra.md").is_file()


def test_collect_skill_supplements_reads_the_normalized_dir_unchanged(tmp_path: Path) -> None:
    """End-to-end: the runtime's EXISTING collect_skill_supplements covers the normalized
    supplements with no modification — proving the Spec-16 boundary is not widened.
    """
    source = _source_skill(tmp_path / "src")
    dest = tmp_path / "mirror" / "gh_skill"
    normalize_skill_into_mirror(source, dest)

    spec = SkillSpec(
        name="gh_skill",
        description="d.",
        path=dest,
        content="Body.",
        content_token_count=1,
        trust=SkillTrust.THIRD_PARTY,
        provenance=SkillProvenance(source="github:o/r", content_hash="h"),
    )
    staged = collect_skill_supplements(spec)
    assert [f.path for f in staged] == [".skills/gh_skill/supplements/deep.md"]


# --- property 2: scripts/ + executables dropped, never written --------------


def test_scripts_and_non_md_assets_are_dropped(tmp_path: Path) -> None:
    source = _source_skill(tmp_path / "src")
    dest = tmp_path / "mirror" / "gh_skill"

    dropped = normalize_skill_into_mirror(source, dest)

    # No executable / non-text content reaches the mirror — text-.md-only boundary.
    assert not (dest / "scripts").exists()
    assert not (dest / "assets").exists()
    assert not any(p.name == "run.py" for p in dest.rglob("*"))
    # The drops are reported (for logging + curator visibility).
    joined = " ".join(dropped)
    assert "scripts" in joined
    assert "run.py" in joined or "scripts" in joined


# --- property 3: drops are logged with a reason (S2-D-7) --------------------


def test_dropped_executables_are_logged_with_a_reason(tmp_path: Path) -> None:
    source = _source_skill(tmp_path / "src")
    dest = tmp_path / "mirror" / "gh_skill"

    captured: list[str] = []
    sink_id = logger.add(captured.append, level="WARNING", format="{message} | {extra}")
    try:
        normalize_skill_into_mirror(source, dest)
    finally:
        logger.remove(sink_id)

    blob = "".join(captured)
    assert "gh_skill" in blob
    assert "scripts" in blob or "executable" in blob.lower() or "dropped" in blob.lower()
