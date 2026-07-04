"""B1a: enhanced SkillSpec v2 fields + scanner population (Spec 24)."""

from __future__ import annotations

from pathlib import Path

import pytest
from persona.schema.skills import SkillSpec
from persona.skills import BUILTIN_ROOT, SkillInjector, SkillScanner, count_tokens
from persona.skills.injector import MARKER

_META_SKILL = """---
name: meta_skill
description: A skill exercising the v2 metadata block.
when_to_use: when testing
tools_required:
  - code_execution
metadata:
  parameters:
    type: object
    additionalProperties: false
    required: [format]
    properties:
      format:
        type: string
        enum: [docx, pdf]
  not_for:
    - inline replies
  composes_with:
    - web_research
  output_format: a file in /workspace/out
  token_budget: 1500
---

# Body
Guidance.
"""


def _write_skill(root: Path, name: str, text: str) -> None:
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")


def test_skillspec_v2_fields_default_to_backward_compatible_empties() -> None:
    spec = SkillSpec(name="x", description="d", path=Path("/tmp/x"))
    assert spec.parameters is None
    assert spec.not_for == []
    assert spec.composes_with == []
    assert spec.output_format is None
    assert spec.token_budget is None


def test_scanner_populates_v2_fields_from_metadata_block(tmp_path: Path) -> None:
    _write_skill(tmp_path, "meta_skill", _META_SKILL)
    [spec] = SkillScanner([tmp_path]).scan(["meta_skill"])
    assert spec.parameters is not None
    assert spec.parameters["properties"]["format"]["enum"] == ["docx", "pdf"]
    assert spec.not_for == ["inline replies"]
    assert spec.composes_with == ["web_research"]
    assert spec.output_format == "a file in /workspace/out"
    assert spec.token_budget == 1500


def test_skill_without_metadata_block_yields_empty_v2_fields(tmp_path: Path) -> None:
    text = "---\nname: plain\ndescription: no metadata block\n---\n\n# Body\n"
    _write_skill(tmp_path, "plain", text)
    [spec] = SkillScanner([tmp_path]).scan(["plain"])
    assert spec.parameters is None
    assert spec.composes_with == []


def test_non_mapping_metadata_does_not_drop_the_skill(tmp_path: Path) -> None:
    # A scalar `metadata:` must degrade to no v2 fields, not omit the skill.
    text = "---\nname: weird\ndescription: scalar metadata\nmetadata: oops\n---\n\n# B\n"
    _write_skill(tmp_path, "weird", text)
    specs = SkillScanner([tmp_path]).scan(["weird"])
    assert len(specs) == 1
    assert specs[0].parameters is None


@pytest.mark.asyncio
async def test_per_skill_token_budget_override_tightens_injection() -> None:
    # D-24-5: a skill declaring a smaller token_budget is truncated to it,
    # below the class-wide 2000 default.
    body = "word " * 400  # ~400 tokens, under the 2000 default
    spec = SkillSpec(
        name="tight",
        description="d",
        path=Path("/tmp/tight"),
        content=body,
        content_token_count=count_tokens(body),
        token_budget=50,
    )
    out = await SkillInjector().inject(spec)
    assert count_tokens(out) <= 50
    assert out.endswith(MARKER)


@pytest.mark.asyncio
async def test_no_override_uses_class_default_budget() -> None:
    body = "word " * 400
    spec = SkillSpec(
        name="default",
        description="d",
        path=Path("/tmp/default"),
        content=body,
        content_token_count=count_tokens(body),
    )
    # ~400 tokens < 2000 default → verbatim, no truncation marker.
    out = await SkillInjector().inject(spec)
    assert out == body
    assert MARKER not in out


def test_document_generation_builtin_carries_v2_parameters() -> None:
    [spec] = SkillScanner([BUILTIN_ROOT]).scan(["document_generation"])
    assert spec.parameters is not None
    assert "format" in spec.parameters["properties"]
    assert spec.token_budget == 2200  # D-24-5 per-skill loosening (the P5 full-fidelity variant)
    assert "web_research" in spec.composes_with
