"""Integration test for the bundled built-in skill packs (T08; relocated at Spec 24).

Round-trips ``web_research``, ``document_generation``, and ``data_analysis``
through the real scanner → injector pipeline using bundled SKILL.md files from
``packages/core/src/persona/skills/builtin/``.

Spec 24 (D-24-1/D-24-9) folded the five document-format packs
(``document_drafting`` / ``docx_generation`` / ``pptx_generation`` /
``xlsx_generation`` / ``pdf_generation``) into the single
``document_generation`` skill. Per the close-out rule, coverage is **relocated,
never deleted**: the under-budget verbatim-inject path (was ``document_drafting``)
and the four format packs' discovery/invariant guards now assert against
``document_generation``; the deprecated names are covered by alias-resolution
tests; the "all six formats work" capability is asserted via the registry.

Verifies spec §9 #8 (built-in skills are discoverable) and §9 #10 (injector
branches end-to-end with real files). Regression guard on token counts:
``web_research`` MUST stay > 2000 tokens and ``document_generation`` MUST stay
< 2000 tokens so both injector branches stay exercised end-to-end.
"""

# ruff: noqa: ANN401, ARG001, ARG002

from __future__ import annotations

from pathlib import Path

import pytest
from persona.skills import (
    SkillInjector,
    SkillScanner,
    count_tokens,
    make_use_skill_tool,
    render_skill_index,
)
from persona.skills.document_generation import supported_formats
from persona.skills.injector import MARKER

pytestmark = pytest.mark.integration

# The bundled built-in skills live alongside the package source.
BUILTIN_ROOT = (
    Path(__file__).parent.parent.parent / "src" / "persona" / "skills" / "builtin"
).resolve()

# Spec 24 deprecated skill names (deleted dirs) → fold into document_generation.
_DEPRECATED_DOCUMENT_SKILLS = [
    "document_drafting",
    "docx_generation",
    "pptx_generation",
    "xlsx_generation",
    "pdf_generation",
]


@pytest.fixture
def scanner() -> SkillScanner:
    return SkillScanner([BUILTIN_ROOT])


@pytest.fixture
def web_research_spec(scanner: SkillScanner):  # noqa: ANN201
    [spec] = scanner.scan(["web_research"])
    return spec


@pytest.fixture
def document_generation_spec(scanner: SkillScanner):  # noqa: ANN201
    [spec] = scanner.scan(["document_generation"])
    return spec


@pytest.fixture
def data_analysis_spec(scanner: SkillScanner):  # noqa: ANN201
    [spec] = scanner.scan(["data_analysis"])
    return spec


class TestBuiltinDiscovery:
    """Spec §9 #8 — built-in skills are discoverable."""

    def test_all_builtins_scan(self, scanner: SkillScanner) -> None:
        out = scanner.scan(["web_research", "document_generation", "data_analysis"])
        assert len(out) == 3
        names = [s.name for s in out]
        assert "web_research" in names
        assert "document_generation" in names
        assert "data_analysis" in names

    def test_scan_preserves_declared_order(self, scanner: SkillScanner) -> None:
        out = scanner.scan(["document_generation", "web_research"])
        assert [s.name for s in out] == ["document_generation", "web_research"]

    def test_web_research_has_expected_tools_required(
        self,
        web_research_spec,  # noqa: ANN001
    ) -> None:
        # The spec lists web_search, web_fetch, file_write as required.
        assert "web_search" in web_research_spec.tools_required
        assert "web_fetch" in web_research_spec.tools_required
        assert "file_write" in web_research_spec.tools_required

    def test_document_generation_has_expected_tools_required(
        self,
        document_generation_spec,  # noqa: ANN001
    ) -> None:
        # D-24-1: the unified skill writes documents via the code sandbox.
        assert document_generation_spec.tools_required == ["code_execution"]

    def test_data_analysis_has_expected_tools_required(
        self,
        data_analysis_spec,  # noqa: ANN001
    ) -> None:
        assert data_analysis_spec.tools_required == ["code_execution"]

    def test_all_have_when_to_use_populated(
        self,
        web_research_spec,  # noqa: ANN001
        document_generation_spec,  # noqa: ANN001
        data_analysis_spec,  # noqa: ANN001
    ) -> None:
        assert web_research_spec.when_to_use
        assert document_generation_spec.when_to_use
        assert data_analysis_spec.when_to_use


class TestDeprecatedSkillAliasResolution:
    """D-24-3/D-24-9 — the 5 deleted document-format skill names keep working
    by resolving, via the alias shim in the scanner, to ``document_generation``
    (backward-compat for persona YAMLs declared before Spec 24)."""

    @pytest.mark.parametrize("old_name", _DEPRECATED_DOCUMENT_SKILLS)
    def test_old_name_resolves_to_document_generation(
        self,
        scanner: SkillScanner,
        old_name: str,
    ) -> None:
        [spec] = scanner.scan([old_name])
        assert spec.name == "document_generation"

    def test_multiple_old_names_dedup_to_one(self, scanner: SkillScanner) -> None:
        out = scanner.scan(["document_drafting", "docx_generation", "pdf_generation"])
        assert [s.name for s in out] == ["document_generation"]


class TestTokenCountRegressionGuards:
    """Pin the size relationship to the budget. If these fail, the bundled
    SKILL.md prose drifted and the injector test coverage is no longer
    end-to-end."""

    def test_web_research_over_budget(self, web_research_spec) -> None:  # noqa: ANN001
        assert web_research_spec.content_token_count > 2000, (
            f"web_research dropped to {web_research_spec.content_token_count} tokens; "
            "must stay > 2000 so the over-budget injector branches are exercised "
            "end-to-end."
        )

    def test_document_generation_under_budget(
        self,
        document_generation_spec,  # noqa: ANN001
    ) -> None:
        # The injection contract (what the model actually receives): the RAW
        # body carries BOTH fidelity variants (P5's fence) and is never
        # injected — each RESOLVED variant must fit the skill's declared
        # D-24-5 budget so the injector's verbatim branch is exercised
        # (over-budget content is summarised/TRUNCATED — the real failure).
        from persona.skills.document_generation import apply_docgen_fidelity

        budget = document_generation_spec.token_budget
        assert budget is not None
        for full in (True, False):
            resolved = apply_docgen_fidelity(document_generation_spec, full_fidelity=full)
            n = count_tokens(resolved.content)
            assert n <= budget, (
                f"document_generation ({'full' if full else 'degrade'} variant) "
                f"resolved to {n} tokens > the declared token_budget {budget}; "
                "the injector would truncate it. Raise the D-24-5 frontmatter "
                "budget deliberately or move detail into supplements/."
            )

    def test_data_analysis_under_budget(
        self,
        data_analysis_spec,  # noqa: ANN001
    ) -> None:
        assert data_analysis_spec.content_token_count < 2000, (
            f"data_analysis grew to {data_analysis_spec.content_token_count} tokens; "
            "must stay < 2000 so the verbatim pass-through path is exercised."
        )
        assert data_analysis_spec.content_token_count < 1800, (
            f"data_analysis at {data_analysis_spec.content_token_count} tokens "
            "drifted past R-17-2's 1,800-token target ceiling; "
            "trim back or supplement-extract before the cushion erodes."
        )

    def test_count_matches_recomputed(
        self,
        web_research_spec,  # noqa: ANN001
        document_generation_spec,  # noqa: ANN001
        data_analysis_spec,  # noqa: ANN001
    ) -> None:
        assert web_research_spec.content_token_count == count_tokens(
            web_research_spec.content,
        )
        assert document_generation_spec.content_token_count == count_tokens(
            document_generation_spec.content,
        )
        assert data_analysis_spec.content_token_count == count_tokens(
            data_analysis_spec.content,
        )


class TestInjectorEndToEnd:
    """Spec §9 #10 — both injector branches exercised against real files."""

    @pytest.mark.asyncio
    async def test_document_generation_verbatim_pass_through(
        self,
        document_generation_spec,  # noqa: ANN001
    ) -> None:
        injector = SkillInjector()
        out = await injector.inject(document_generation_spec)
        # Under budget → verbatim.
        assert out == document_generation_spec.content
        assert MARKER not in out

    @pytest.mark.asyncio
    async def test_web_research_truncated_without_summariser(
        self,
        web_research_spec,  # noqa: ANN001
    ) -> None:
        injector = SkillInjector()
        out = await injector.inject(web_research_spec)
        # Over budget + no summariser → truncated.
        assert out.endswith(MARKER)
        assert count_tokens(out) <= SkillInjector.TOKEN_BUDGET
        prefix = out[: -len(MARKER)]
        assert web_research_spec.content.startswith(prefix)

    @pytest.mark.asyncio
    async def test_web_research_summarised_with_summariser(
        self,
        web_research_spec,  # noqa: ANN001
    ) -> None:
        captured: list[str] = []

        async def fake_summariser(content: str) -> str:
            captured.append(content)
            return "Brief summary of the web_research skill body."

        injector = SkillInjector(summariser=fake_summariser)
        out = await injector.inject(web_research_spec)
        assert out == "Brief summary of the web_research skill body."
        assert len(captured) == 1
        assert captured[0] == web_research_spec.content


class TestIndexRendering:
    """Spec §9 #2 — the index is rendered correctly from the scanned specs."""

    def test_index_contains_both_skill_names_and_descriptions(
        self,
        scanner: SkillScanner,
    ) -> None:
        specs = scanner.scan(["web_research", "document_generation"])
        out = render_skill_index(specs)
        assert "**web_research**" in out
        assert "**document_generation**" in out
        assert "Research a topic" in out
        assert "Produce a downloadable document" in out

    def test_index_contains_use_when_lines(self, scanner: SkillScanner) -> None:
        specs = scanner.scan(["web_research", "document_generation"])
        out = render_skill_index(specs)
        use_when_count = out.count("Use when:")
        assert use_when_count == 2

    def test_index_is_compact(self, scanner: SkillScanner) -> None:
        specs = scanner.scan(["web_research", "document_generation"])
        out = render_skill_index(specs)
        n = count_tokens(out)
        assert n < 500, f"index grew to {n} tokens; expected < 500"


class TestUseSkillToolIntegration:
    """Spec §9 #3 (activation path) — use_skill exposes the built-ins."""

    @pytest.mark.asyncio
    async def test_factory_works_with_real_specs(
        self,
        scanner: SkillScanner,
    ) -> None:
        specs = scanner.scan(["web_research", "document_generation"])
        t = make_use_skill_tool(specs)
        r = await t.execute(skill_name="web_research")
        assert r.is_error is False
        assert r.data == {"skill_name": "web_research"}

    @pytest.mark.asyncio
    async def test_factory_rejects_unknown_when_real_specs_loaded(
        self,
        scanner: SkillScanner,
    ) -> None:
        specs = scanner.scan(["web_research", "document_generation"])
        t = make_use_skill_tool(specs)
        r = await t.execute(skill_name="not_a_skill")
        assert r.is_error is True
        assert "document_generation" in r.content
        assert "web_research" in r.content


class TestPublicSurface:
    """`persona.skills` is importable as the documented public surface."""

    def test_all_public_names_importable(self) -> None:
        from persona.skills import (  # noqa: F401
            SkillInjector,
            SkillManifestError,
            SkillScanner,
            SkillSpec,
            count_tokens,
            make_use_skill_tool,
            render_skill_index,
        )

    def test_underscore_prefixed_not_re_exported(self) -> None:
        import persona.skills as skills_pkg

        assert "_tokens" not in skills_pkg.__all__
        assert "_frontmatter" not in skills_pkg.__all__


# ---------------------------------------------------------------------------
# Spec 24 — the unified document_generation skill (relocated from the four
# deleted *_generation packs' discovery/invariant guards, D-24-1).
# ---------------------------------------------------------------------------


class TestUnifiedDocumentGenerationSkill:
    """The discovery + invariant guards that the four deleted ``*_generation``
    packs carried (Spec 16) are relocated onto the single
    ``document_generation`` skill: code_execution-only tools, under the 2000
    token ceiling, when_to_use populated, a supplements/ directory, the
    SKILL.md teaches the staged path, and the file is packaged + readable."""

    def test_declares_code_execution_tool_required(
        self,
        document_generation_spec,  # noqa: ANN001
    ) -> None:
        assert document_generation_spec.tools_required == ["code_execution"]

    def test_under_token_budget(self, document_generation_spec) -> None:  # noqa: ANN001
        n = count_tokens(document_generation_spec.content)
        # Growth guard on the RAW body (2026-07-03): the raw body carries BOTH
        # P5 fidelity variants inline (the fence) and is never injected as-is —
        # the injection contract (each resolved variant ≤ the declared D-24-5
        # token_budget) is asserted in test_document_generation_under_budget.
        # This guard only catches unbounded body growth.
        assert n <= 2500, (
            f"document_generation RAW body grew to {n} tokens (> 2500). Move "
            "per-format detail into supplements/ instead of growing the body, "
            "and check the resolved variants against the declared token_budget."
        )

    def test_when_to_use_populated(self, document_generation_spec) -> None:  # noqa: ANN001
        assert document_generation_spec.when_to_use
        assert document_generation_spec.when_to_use.strip()

    def test_has_supplements_directory(
        self,
        document_generation_spec,  # noqa: ANN001
    ) -> None:
        supplements_dir = document_generation_spec.path / "supplements"
        assert supplements_dir.is_dir(), "document_generation has no supplements/ directory"
        md_files = list(supplements_dir.glob("*.md"))
        assert len(md_files) >= 1, "document_generation/supplements/ contains no .md files"

    def test_path_taught_equals_path_staged(
        self,
        document_generation_spec,  # noqa: ANN001
    ) -> None:
        # D-16-2-path: the SKILL.md body must reference the sandbox-internal
        # path the runtime stages supplements at, or the model can't read them.
        prefix = "/workspace/in/.skills/document_generation/supplements/"
        assert prefix in document_generation_spec.content, (
            f"document_generation/SKILL.md does NOT reference {prefix!r}; the model "
            "cannot reach the staged supplements (D-16-2-path)."
        )

    def test_skill_md_packaged_and_readable(
        self,
        document_generation_spec,  # noqa: ANN001
    ) -> None:
        skill_md = document_generation_spec.path / "SKILL.md"
        assert skill_md.is_file(), f"document_generation/SKILL.md not on disk at {skill_md}"
        assert skill_md.stat().st_size > 200, (
            f"document_generation/SKILL.md is suspiciously small ({skill_md.stat().st_size} bytes)"
        )

    def test_supports_all_six_formats(self) -> None:
        # Capability relocation: the four deleted packs (docx/pptx/xlsx/pdf)
        # plus the two new common formats (md/txt) are all supported by the
        # unified skill's registry — "all six formats work" at the new entry.
        assert set(supported_formats()) == {"docx", "pdf", "pptx", "xlsx", "md", "txt"}

    def test_skill_md_does_not_instruct_a_network_install(
        self,
        document_generation_spec,  # noqa: ANN001
    ) -> None:
        # The production sandbox runs with internet egress DISABLED by design
        # (``NetworkPolicy()`` default = egress off; hosted enforces
        # ``allow_internet_access=False``, D-12-4). A ``pip install`` therefore
        # ALWAYS fails (DNS error / setup-timeout). The SKILL.md MUST teach
        # pre-installed libraries only and must NOT instruct any runtime
        # install. Verified offline against real E2B: docx/xlsx/md/txt/pdf
        # (matplotlib PdfPages) produce valid files; pptx degrades honestly.
        body = document_generation_spec.content.lower()
        forbidden = [
            "pip install",
            "pip3 install",
            "uv pip install",
            "-m pip",
            "subprocess.run([sys.executable",
            "find_spec",
            "importlib.util",
            "apt-get install",
            "apt install",
        ]
        offenders = [needle for needle in forbidden if needle in body]
        assert not offenders, (
            "document_generation/SKILL.md instructs a runtime install "
            f"({offenders}); the sandbox has egress disabled (D-12-4) so any "
            "install hangs until the setup-timeout. Route every format to a "
            "pre-installed library or degrade honestly."
        )

    def test_skill_md_routes_pdf_and_pptx_to_offline_safe_strategies(
        self,
        document_generation_spec,  # noqa: ANN001
    ) -> None:
        # reportlab + python-pptx are ABSENT from the default sandbox template
        # and unobtainable offline. The SKILL.md must route pdf to matplotlib
        # (PdfPages, pre-installed) and degrade pptx honestly — never name the
        # absent libraries as the route to take.
        body = document_generation_spec.content
        assert "PdfPages" in body, (
            "SKILL.md must teach matplotlib's PdfPages for offline PDF "
            "(reportlab is absent from the sandbox and cannot be installed)."
        )
        # pptx must be flagged as unavailable / degraded, not produced via
        # an absent library import.
        assert "python-pptx" in body, "SKILL.md must name python-pptx in the pptx degrade guidance."
        assert "NOT installed" in body, (
            "SKILL.md must state python-pptx is NOT installed and degrade the "
            "pptx format honestly (offer docx/md instead)."
        )
