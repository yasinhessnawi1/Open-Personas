"""Tests for ``persona.skills._frontmatter.parse_skill_markdown`` (T03, D-04-3).

Mirrors the fixture set from Phase 3 research §2 (the comparison probe
against ``python-frontmatter``). Confirms the hand-rolled parser:

- Accepts valid front matter (happy path).
- Rejects missing-opening / missing-closing delimiters with structured
  ``SkillManifestError`` context.
- Rejects malformed YAML.
- Rejects non-mapping front matter (top-level list or scalar).
- Tolerates UTF-8 BOM and CRLF line endings.
- Accepts ``---\\n<yaml>\\n---`` with no trailing newline (body-empty).
"""

# ruff: noqa: ANN401, ARG001, ARG002

from __future__ import annotations

from pathlib import Path  # noqa: TC003 — used at runtime in fixtures

import pytest
from persona.errors import SkillManifestError
from persona.skills._frontmatter import parse_skill_markdown


def _write(tmp_path: Path, name: str, content: str) -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


HAPPY = """\
---
name: web_research
description: Research a topic.
when_to_use: When the user asks to research.
tools_required:
  - web_search
  - web_fetch
---

# Web Research

Step 1: search.
Step 2: fetch.
"""


class TestHappyPath:
    def test_returns_dict_and_body(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "SKILL.md", HAPPY)
        meta, body = parse_skill_markdown(path)
        assert meta["name"] == "web_research"
        assert meta["description"] == "Research a topic."
        assert meta["when_to_use"].startswith("When the user")
        assert meta["tools_required"] == ["web_search", "web_fetch"]
        assert body.startswith("\n# Web Research")
        assert "Step 1: search." in body

    def test_body_preserves_newlines_verbatim(self, tmp_path: Path) -> None:
        text = "---\nname: x\ndescription: y\n---\n\n## H1\n\n- a\n- b\n"
        path = _write(tmp_path, "SKILL.md", text)
        _, body = parse_skill_markdown(path)
        assert body == "\n## H1\n\n- a\n- b\n"

    def test_minimal_valid_front_matter(self, tmp_path: Path) -> None:
        text = "---\nname: x\ndescription: y\n---\nBody.\n"
        path = _write(tmp_path, "SKILL.md", text)
        meta, body = parse_skill_markdown(path)
        assert meta == {"name": "x", "description": "y"}
        assert body == "Body.\n"

    def test_front_matter_only_no_trailing_newline(self, tmp_path: Path) -> None:
        # Body is empty; closing delimiter at EOF with no trailing newline.
        text = "---\nname: x\ndescription: y\n---"
        path = _write(tmp_path, "SKILL.md", text)
        meta, body = parse_skill_markdown(path)
        assert meta == {"name": "x", "description": "y"}
        assert body == ""

    def test_front_matter_only_with_trailing_newline(self, tmp_path: Path) -> None:
        text = "---\nname: x\ndescription: y\n---\n"
        path = _write(tmp_path, "SKILL.md", text)
        meta, body = parse_skill_markdown(path)
        assert meta == {"name": "x", "description": "y"}
        assert body == ""


class TestEncodingTolerance:
    def test_utf8_bom_stripped(self, tmp_path: Path) -> None:
        text = "﻿---\nname: x\ndescription: y\n---\n\nBody.\n"
        path = tmp_path / "SKILL.md"
        # Write bytes directly so the BOM survives.
        path.write_bytes(text.encode("utf-8"))
        meta, body = parse_skill_markdown(path)
        assert meta == {"name": "x", "description": "y"}
        assert body == "\nBody.\n"

    def test_crlf_normalised(self, tmp_path: Path) -> None:
        text = "---\r\nname: x\r\ndescription: y\r\n---\r\n\r\nBody.\r\n"
        # write_bytes: write_text would turn each \n into \r\n again on Windows.
        path = tmp_path / "SKILL.md"
        path.write_bytes(text.encode("utf-8"))
        meta, body = parse_skill_markdown(path)
        assert meta == {"name": "x", "description": "y"}
        # CRLF normalised inside parser; body retains LF
        assert body == "\nBody.\n"


class TestMalformed:
    def test_missing_opening_delimiter(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "SKILL.md", "# Just a markdown file\n\nNo front matter.\n")
        with pytest.raises(SkillManifestError) as exc:
            parse_skill_markdown(path)
        assert "opening" in str(exc.value)
        assert "path" in exc.value.context

    def test_missing_closing_delimiter(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "SKILL.md", "---\nname: x\ndescription: y\n\n# Body, no close\n")
        with pytest.raises(SkillManifestError) as exc:
            parse_skill_markdown(path)
        assert "closing" in str(exc.value)
        assert "path" in exc.value.context

    def test_malformed_yaml(self, tmp_path: Path) -> None:
        text = "---\nname: x\ndescription: y\ntools_required: [unterminated\n---\n\nBody.\n"
        path = _write(tmp_path, "SKILL.md", text)
        with pytest.raises(SkillManifestError) as exc:
            parse_skill_markdown(path)
        assert "malformed YAML" in str(exc.value)
        # ``reason`` carries a truncated YAMLError detail.
        assert "reason" in exc.value.context
        assert len(exc.value.context["reason"]) <= 200

    def test_non_mapping_front_matter_list(self, tmp_path: Path) -> None:
        # Top-level YAML list — not a dict.
        text = "---\n- a\n- b\n---\n\nBody.\n"
        path = _write(tmp_path, "SKILL.md", text)
        with pytest.raises(SkillManifestError) as exc:
            parse_skill_markdown(path)
        assert "mapping" in str(exc.value)

    def test_non_mapping_front_matter_scalar(self, tmp_path: Path) -> None:
        text = "---\njust a string\n---\n\nBody.\n"
        path = _write(tmp_path, "SKILL.md", text)
        with pytest.raises(SkillManifestError):
            parse_skill_markdown(path)

    def test_empty_file(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "SKILL.md", "")
        with pytest.raises(SkillManifestError) as exc:
            parse_skill_markdown(path)
        # Empty doesn't start with ``---\n`` → opening-delimiter failure.
        assert "opening" in str(exc.value)

    def test_body_only_no_dashes(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "SKILL.md", "Just plain body, no delimiters.")
        with pytest.raises(SkillManifestError) as exc:
            parse_skill_markdown(path)
        assert "opening" in str(exc.value)


class TestContextShape:
    def test_path_always_present(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "SKILL.md", "no front matter")
        with pytest.raises(SkillManifestError) as exc:
            parse_skill_markdown(path)
        assert exc.value.context["path"] == str(path)

    def test_reason_present_only_on_yaml_parse_error(self, tmp_path: Path) -> None:
        # Opening-delimiter failure: no reason key.
        p1 = _write(tmp_path, "SKILL1.md", "bare text")
        with pytest.raises(SkillManifestError) as exc1:
            parse_skill_markdown(p1)
        assert "reason" not in exc1.value.context

        # Malformed YAML: reason key present.
        p2 = _write(tmp_path, "SKILL2.md", "---\nfoo: [unterminated\n---\n")
        with pytest.raises(SkillManifestError) as exc2:
            parse_skill_markdown(p2)
        assert "reason" in exc2.value.context


class TestFileSystemBehaviour:
    def test_nonexistent_path_raises_oserror_not_manifest_error(
        self,
        tmp_path: Path,
    ) -> None:
        # OSError propagates; the scanner's envelope (T04) catches it. The
        # parser doesn't pre-check existence — let the OS error speak.
        with pytest.raises(FileNotFoundError):
            parse_skill_markdown(tmp_path / "does_not_exist.md")
