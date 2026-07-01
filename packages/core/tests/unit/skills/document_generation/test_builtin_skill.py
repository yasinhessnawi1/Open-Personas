"""A3: the unified ``document_generation`` builtin skill ↔ registry consistency.

Guards against drift between the registry (code) and the bundled skill data
(SKILL.md front matter + supplements/ + templates/).
"""

from __future__ import annotations

from persona.skills import BUILTIN_ROOT, SkillScanner
from persona.skills._frontmatter import parse_skill_markdown
from persona.skills.document_generation import (
    FORMAT_HANDLERS,
    supported_formats,
    supported_templates,
)

_SKILL_DIR = BUILTIN_ROOT / "document_generation"


def test_scanner_discovers_document_generation() -> None:
    specs = SkillScanner([BUILTIN_ROOT]).scan(["document_generation"])
    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "document_generation"
    assert spec.description
    assert spec.when_to_use
    assert spec.tools_required == ["code_execution"]
    assert spec.content  # body is non-empty
    assert spec.content_token_count > 0


def test_skill_md_format_enum_matches_registry() -> None:
    meta, _ = parse_skill_markdown(_SKILL_DIR / "SKILL.md")
    enum = meta["metadata"]["parameters"]["properties"]["format"]["enum"]
    assert sorted(enum) == list(supported_formats())


def test_skill_md_template_enum_matches_registry() -> None:
    meta, _ = parse_skill_markdown(_SKILL_DIR / "SKILL.md")
    enum = meta["metadata"]["parameters"]["properties"]["template"]["enum"]
    assert sorted(enum) == list(supported_templates())


def test_every_declared_supplement_topic_has_a_bundled_file() -> None:
    supplements = _SKILL_DIR / "supplements"
    for fmt, handler in FORMAT_HANDLERS.items():
        for topic in handler.supplement_topics:
            path = supplements / f"{fmt}-{topic}.md"
            assert path.is_file(), f"missing supplement {path.name}"


def test_every_registered_template_has_a_bundled_file() -> None:
    templates = _SKILL_DIR / "templates"
    for template_id in supported_templates():
        path = templates / f"{template_id}.md"
        assert path.is_file(), f"missing template {path.name}"
        assert "{{" in path.read_text(), f"template {path.name} has no placeholders"


def test_text_formats_bundle_no_supplements() -> None:
    # md / txt declare no topics; nothing prefixed md-/txt- should exist.
    supplements = _SKILL_DIR / "supplements"
    stray = [p.name for p in supplements.glob("md-*.md")]
    stray += [p.name for p in supplements.glob("txt-*.md")]
    assert stray == []


# -- Spec P5 A3a: pdf try-import fidelity (template-agnostic) ----------------


def test_pdf_section_teaches_reportlab_with_matplotlib_fallback() -> None:
    """The pdf section must teach BOTH branches (P5-D-4): prefer reportlab, fall
    back to matplotlib on ModuleNotFoundError — a static try-import that produces
    a real .pdf whether or not the custom template baked reportlab in."""
    body = (_SKILL_DIR / "SKILL.md").read_text()
    pdf_section = body.split("### `pdf`", 1)[1].split("### `pptx`", 1)[0]
    # full-fidelity branch (reportlab platypus)
    assert "reportlab" in pdf_section
    assert "SimpleDocTemplate" in pdf_section
    # the fallback branch, reached only on a template without reportlab
    assert "except ModuleNotFoundError" in pdf_section
    assert "PdfPages" in pdf_section


def test_pdf_descriptor_documents_both_libraries() -> None:
    from persona.skills.document_generation import FORMAT_HANDLERS

    lib = FORMAT_HANDLERS["pdf"].library
    assert "reportlab" in lib
    assert "matplotlib" in lib


def test_skill_never_instructs_a_runtime_install() -> None:
    """Content-level egress/no-pip guard (D-12-4): the model-facing instructions
    must never teach a runtime install; every mention of installing is a *negative*
    ('never install'). Complements the structural sandbox invariant (A3b)."""
    body = (_SKILL_DIR / "SKILL.md").read_text().lower()
    # No imperative install lines. Any occurrence of "pip install" / "apt" must sit
    # in a forbidding clause, so assert the affirmative-install command shapes are absent.
    for forbidden in ("pip install", "!pip", "subprocess.run(['pip'", "os.system('pip"):
        assert forbidden not in body, f"SKILL.md must not teach `{forbidden}`"
    # And the never-install rule is present + explicit.
    assert "never" in body
    assert "install" in body
