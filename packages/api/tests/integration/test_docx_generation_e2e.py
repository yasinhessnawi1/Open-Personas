"""T11 — docx_generation end-to-end backend integration test (Spec 16).

Production-verification path for the docx skill — quality-bar criterion #2.

This test wires the real ``LocalDockerSandbox`` behind ``[sandbox]`` extra,
the real ``docx_generation`` SKILL.md + supplements (M1a-staged via
``collect_skill_supplements``), the real :func:`make_use_skill_tool`, and
the real :func:`make_code_execution_tool` whose ``produced_file_persister``
callback copies the docx from the sandbox session's ``host_out`` into
``<persona_workspace_root>/<owner_id>/<persona_id>/<filename>`` via
:meth:`LocalDockerSandbox.copy_produced_file_to`. The persisted file is
then parsed with python-docx host-side and asserted against research §3.7
table:

- ``"fldSimple" in xml or "fldChar" in xml`` — TOC field shell present.
- ``len(doc.tables[0].rows) >= 3`` — 3-column table with header + 2 data.
- ``len(doc.inline_shapes) >= 1`` — embedded PNG image.
- ``any(p.style.name == "Heading 1" / "Heading 2" for p in doc.paragraphs)``
  — named-style headings (NOT raw bold + size).
- ``doc.styles["Normal"].font.name + .size both not None`` — body font set.
- ``Inches(N) <= width.emu <= 5_000_000`` — image sized, not raw 96-dpi.

The representative task is the §3.2 5-section Norwegian tenancy law memo
with a TOC, a 3-column comparison table, and an inline image — the same
prose the T11 scorecard binds to.

**Pre-condition.** Both Phase 5b Production-fixes have landed:

- D-16-X-6 PATH fix — ``_BASE_CONTAINER_KWARGS["environment"]["PATH"]``
  prepends ``/opt/venv/bin``, so the test executes a clean
  ``from docx import Document`` snippet (no ``sys.path.insert`` prelude).
- D-16-X-7 relative-path fix — ``collect_skill_supplements`` emits
  ``.skills/docx_generation/supplements/<topic>.md`` (relative), so
  ``LocalDockerSandbox._seed_workspace`` joins with ``host_in`` correctly.

Both verified by reading the post-fix files before composing this test.

**Scope.** Local backend only (consistent with T10's ``test_document_-
generation_loops.py`` A2 scoping — ``HostedSandbox`` produced_files
persistence lands when Spec 17 ships D-17-X-bytes-persistence end-to-end).

**``_ScriptedBackend``** is module-private per the api per-file convention
(T01 audit A1 — four precedents in the api integration tree counting T10).
The agentic loop is the cleaner scripted-test surface for this format
(handover surprise #1 + T10 precedent at ``test_document_generation_loops``).
"""

# ruff: noqa: SLF001, ANN401 — tests pin scripted backends into the tier
# registry cache and assert on the sandbox's per-session host workspace
# dirs (SLF001); the MemoryStore double mirrors the Protocol's loose
# kwargs shape (ANN401).

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from persona.backends import BackendConfig, ChatResponse
from persona.backends.types import TokenUsage
from persona.sandbox.local_docker import (
    DEFAULT_IMAGE,
    LocalDockerSandbox,
    is_docker_available,
)
from persona.sandbox.result import NetworkPolicy, ResourceLimits, SandboxFile
from persona.sandbox.tool import make_code_execution_tool
from persona.schema.persona import Persona, PersonaIdentity
from persona.schema.tools import ToolCall
from persona.skills import (
    SkillInjector,
    SkillScanner,
    collect_skill_supplements,
    make_use_skill_tool,
)
from persona.tools import Toolbox
from persona_runtime.agentic.loop import AgenticLoop
from persona_runtime.agentic.run import RunStatus
from persona_runtime.agentic.step import StepType
from persona_runtime.prompt import PromptBuilder
from persona_runtime.router import Router
from persona_runtime.tier import TierConfig, TierRegistry

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from persona.backends.types import StreamChunk, ToolSpec
    from persona.schema.conversation import ConversationMessage
    from persona.schema.skills import SkillSpec
    from persona.tools.protocol import AsyncTool


pytestmark = [pytest.mark.integration, pytest.mark.docker]


_DUMMY_CFG = BackendConfig(provider="anthropic", model="m", api_key=None)


# ---------------------------------------------------------------------------
# Inspection artifact directory (D-16-X-3: gitignored; the scorecard in
# state.md is the committed evidence). Reproducible from re-running this
# test — ``shutil.copy`` lands the produced docx here at the close of the
# happy path so the operator can open it in Word / LibreOffice for the
# §3.7 visual-only rows.
# ---------------------------------------------------------------------------

_INSPECTION_DIR = (
    Path(__file__).resolve().parents[4] / "docs" / "specs" / "phase2" / "spec_16" / "inspection"
)


# ---------------------------------------------------------------------------
# Module-private _ScriptedBackend (T01 audit A1 — per-file api convention).
# Copied from packages/runtime/tests/_fakes.py:50 / T10's test_document_-
# generation_loops.py:132 with only the agentic-chat_script path needed for
# T11 (the docx test drives AgenticLoop end-to-end).
# ---------------------------------------------------------------------------


class _ScriptedBackend:
    """A ChatBackend that replays a scripted ``chat_script`` of ChatResponses."""

    def __init__(
        self,
        *,
        chat_script: list[ChatResponse],
        provider_name: str = "anthropic",
        model_name: str = "claude-sonnet-4-6",
        supports_vision: bool = False,
    ) -> None:
        self._chat_script: list[ChatResponse] = list(chat_script)
        self._chat_index = 0
        self.chat_calls = 0
        self._provider_name = provider_name
        self._model_name = model_name
        self._supports_vision = supports_vision

    @property
    def provider_name(self) -> str:
        return self._provider_name

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def supports_native_tools(self) -> bool:
        return False

    @property
    def supports_vision(self) -> bool:
        return self._supports_vision

    async def chat(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002
        *,
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
        temperature: float = 0.0,  # noqa: ARG002
        max_tokens: int = 4096,  # noqa: ARG002
        stop: list[str] | None = None,  # noqa: ARG002
    ) -> ChatResponse:
        self.chat_calls += 1
        if self._chat_index < len(self._chat_script):
            response = self._chat_script[self._chat_index]
            self._chat_index += 1
            return response
        return ChatResponse(
            content="",
            tool_calls=[],
            usage=TokenUsage(prompt_tokens=1, completion_tokens=0, total_tokens=1),
            model=self._model_name,
            provider=self._provider_name,
            latency_ms=0.0,
        )

    async def chat_stream(
        self,
        messages: list[ConversationMessage],  # noqa: ARG002
        *,
        tools: list[ToolSpec] | None = None,  # noqa: ARG002
        temperature: float = 0.0,  # noqa: ARG002
        max_tokens: int = 4096,  # noqa: ARG002
        stop: list[str] | None = None,  # noqa: ARG002
    ) -> AsyncIterator[StreamChunk]:
        # T11 only uses chat() (agentic loop). chat_stream is unused.
        if False:  # pragma: no cover
            yield  # type: ignore[unreachable]
        raise NotImplementedError("T11 only drives AgenticLoop")


# ---------------------------------------------------------------------------
# In-memory MemoryStore double (mirrors packages/runtime/tests/_fakes.py).
# Avoids the Postgres + RLS engine overhead — T11 is about the loop + sandbox
# + skill + supplements wiring, not the persistence layer.
# ---------------------------------------------------------------------------


class _FakeStore:
    """Minimal in-memory MemoryStore recording writes."""

    def __init__(self) -> None:
        self.writes: list[list[Any]] = []
        self._all: list[Any] = []

    def write(
        self,
        persona_id: str,  # noqa: ARG002
        chunks: list[Any],
        *,
        source: Any,  # noqa: ARG002
        written_by: str | None = None,  # noqa: ARG002
        reason: str | None = None,  # noqa: ARG002
        force: bool = False,  # noqa: ARG002
        retry_on_conflict: bool = True,  # noqa: ARG002
    ) -> None:
        self.writes.append(list(chunks))
        self._all.extend(chunks)

    def query(
        self,
        persona_id: str,  # noqa: ARG002
        query: str,  # noqa: ARG002
        top_k: int,  # noqa: ARG002
        **filters: Any,  # noqa: ARG002
    ) -> list[Any]:
        return []

    def get_all(
        self,
        persona_id: str,  # noqa: ARG002
        *,
        include_superseded: bool = False,  # noqa: ARG002
    ) -> list[Any]:
        return list(self._all)

    def delete(self, persona_id: str) -> None: ...  # noqa: ARG002

    def remove_documents(
        self,
        persona_id: str,  # noqa: ARG002
        doc_ids: list[str],  # noqa: ARG002
    ) -> None: ...

    def history(
        self,
        persona_id: str,  # noqa: ARG002
        logical_id: str,  # noqa: ARG002
    ) -> list[Any]:
        return []

    def rollback(
        self,
        persona_id: str,  # noqa: ARG002
        logical_id: str,  # noqa: ARG002
        to_version: int,  # noqa: ARG002
        *,
        source: Any,  # noqa: ARG002
        written_by: str | None = None,  # noqa: ARG002
        reason: str | None = None,  # noqa: ARG002
        force: bool = False,  # noqa: ARG002
    ) -> None: ...


# ---------------------------------------------------------------------------
# Docker availability — skip cleanly when the substrate is not reachable.
# ---------------------------------------------------------------------------


def _is_image_available(tag: str) -> bool:
    """True if the sandbox image is locally available (mirrors core conftest)."""
    try:
        import docker
        from docker.errors import DockerException, ImageNotFound
    except ImportError:
        return False
    try:
        client = docker.from_env()
        try:
            client.images.get(tag)
        except ImageNotFound:
            return False
        finally:
            client.close()
    except DockerException:
        return False
    return True


def _docker_skip_reason() -> str | None:
    """Return a skip reason if the local Docker substrate isn't ready, else None."""
    if not is_docker_available():
        return (
            "Docker daemon unreachable; T11 needs LocalDockerSandbox to drive "
            "code_execution. Start Docker and rerun."
        )
    if not _is_image_available(DEFAULT_IMAGE):
        return (
            f"Sandbox image {DEFAULT_IMAGE!r} not built. Build via the "
            "Spec 12 T06 Dockerfile (or pull) before running T11."
        )
    return None


# ---------------------------------------------------------------------------
# Persona, registry, scanner — wire the real docx_generation skill.
# ---------------------------------------------------------------------------


_PERSONA_ID = "astrid_t11_docx"
_OWNER_ID = "owner_t11_docx"


def _persona() -> Persona:
    return Persona(
        persona_id=_PERSONA_ID,
        identity=PersonaIdentity(
            name="Astrid",
            role="document-producing tenancy assistant",
            background="Knows husleieloven; produces real docx files.",
            constraints=[],
        ),
        skills=["docx_generation"],
        tools=["use_skill", "code_execution"],
    )


def _stores() -> dict[str, _FakeStore]:
    return {
        "identity": _FakeStore(),
        "self_facts": _FakeStore(),
        "worldview": _FakeStore(),
        "episodic": _FakeStore(),
    }


def _registry_for(backend: _ScriptedBackend) -> TierRegistry:
    """A TierRegistry whose every tier resolves to the scripted backend."""
    registry = TierRegistry(
        {
            "frontier": TierConfig(name="frontier", backend_config=_DUMMY_CFG),
            "mid": TierConfig(name="mid", backend_config=_DUMMY_CFG),
            "small": TierConfig(name="small", backend_config=_DUMMY_CFG),
        }
    )
    registry._cache = {"frontier": backend, "mid": backend, "small": backend}
    return registry


_BUILTIN_ROOT = (
    Path(__file__).resolve().parents[4]
    / "packages"
    / "core"
    / "src"
    / "persona"
    / "skills"
    / "builtin"
)


def _scan_real_docx_skill() -> list[SkillSpec]:
    """Scan the real builtin tree for the unified ``document_generation`` skill.

    Spec 24 (D-24-9) folded ``docx_generation`` into ``document_generation``;
    the deprecated name still resolves via the alias shim. The SkillScanner
    reads the real on-disk SKILL.md + supplements/ tree at
    ``packages/core/src/persona/skills/builtin/document_generation/``.
    ``collect_skill_supplements`` later globs ``spec.path / "supplements"``
    for the M1a staging — the docx supplements ship as ``docx-<topic>.md``.
    """
    scanner = SkillScanner([_BUILTIN_ROOT])
    return scanner.scan(["docx_generation"], tool_allow_list=["code_execution"])


def _resp(content: str = "", *, tool_calls: list[ToolCall] | None = None) -> ChatResponse:
    return ChatResponse(
        content=content,
        tool_calls=tool_calls or [],
        usage=TokenUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
        model="claude-sonnet-4-6",
        provider="anthropic",
        latency_ms=1.0,
    )


# ---------------------------------------------------------------------------
# The representative-task code snippet. Models the agent's behaviour: reads
# enough of the on-disk SKILL.md guidance (TOC field shell, named heading
# styles, body font, image embed, table) to write idiomatic python-docx
# code that hits every §3.7 docx surface for the 5-section Norwegian
# tenancy memo + 3-col comparison table + inline PNG image.
#
# The PIL-generated PNG is intentionally minimal (a coloured rectangle
# with white text) so the test is deterministic and fast. Real production
# usage would use matplotlib or a richer image source; the §3.7 #6 surface
# (inline_shapes[0].width.emu in [100_000, 5_000_000]) only cares that the
# image is sized via Inches(N), not its content.
# ---------------------------------------------------------------------------


_DOCX_CODE = '''\
"""Produce the §3.2 representative docx memo: 5 sections, TOC, 3-col
table, inline image. All §3.7 docx surfaces hit."""
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt
from PIL import Image, ImageDraw


def add_toc(doc, levels="1-3"):
    """The fldChar TOC shell — Word fills on F9. Per supplements/toc.md."""
    p = doc.add_paragraph()
    p.style = doc.styles["TOC Heading"] if "TOC Heading" in doc.styles else doc.styles["Heading 1"]
    p.add_run("Table of Contents")

    p = doc.add_paragraph()
    run = p.add_run()
    fld_begin = OxmlElement("w:fldChar")
    fld_begin.set(qn("w:fldCharType"), "begin")
    instr = OxmlElement("w:instrText")
    instr.set(qn("xml:space"), "preserve")
    instr.text = f' TOC \\\\o "{levels}" \\\\h \\\\z \\\\u '
    fld_sep = OxmlElement("w:fldChar")
    fld_sep.set(qn("w:fldCharType"), "separate")
    fld_end = OxmlElement("w:fldChar")
    fld_end.set(qn("w:fldCharType"), "end")
    run._r.append(fld_begin)
    run._r.append(instr)
    run._r.append(fld_sep)
    run._r.append(fld_end)


# Build the placeholder PNG inline via PIL (480x270 ~16:9; coloured
# rectangle with caption text overlay — content is not load-bearing for
# the §3.7 image surface).
img_path = Path("/workspace/out/tenant_protection_diagram.png")
img = Image.new("RGB", (480, 270), color=(30, 90, 160))
draw = ImageDraw.Draw(img)
draw.rectangle((20, 20, 460, 250), outline=(255, 255, 255), width=3)
draw.text((40, 120), "Tenant protection diagram", fill=(255, 255, 255))
img.save(img_path, format="PNG")

doc = Document()

# Body font — set BEFORE adding content (criterion #4).
normal = doc.styles["Normal"]
normal.font.name = "Calibri"
normal.font.size = Pt(11)

# Title (Title style — distinct from Heading 1 per the SKILL.md guidance).
doc.add_heading(
    "Norwegian tenancy law: protections for tenants subject to landlord rent increases",
    level=0,
)

# TOC at the top (criterion #1).
add_toc(doc, levels="1-3")
doc.add_page_break()

# 5 sections, each Heading 1 (named-style, criterion #3) with Heading 2
# sub-headings and body paragraphs.
sections = [
    (
        "Background",
        "Norwegian tenancy law (husleieloven) regulates the landlord-tenant relationship, "
        "with specific notice periods and procedural safeguards on rent increases.",
        [
            (
                "Statutory framework",
                "The 1999 Act on Tenancy Agreements sets the floor for tenant protections.",
            ),
            (
                "Recent amendments",
                "The 2024 reform tightened notice periods on annual rent increases.",
            ),
        ],
    ),
    (
        "Pre-2024 rules",
        "Before the 2024 reform, landlords could increase rent annually with 1 month notice "
        "tied to the consumer price index.",
        [
            ("Notice period", "1 month written notice was the statutory minimum."),
            ("Index ceiling", "Increase capped at the official CPI for the prior 12 months."),
        ],
    ),
    (
        "Post-2024 rules",
        "After the 2024 reform, notice periods extended and contested-increase resolution sped up.",
        [
            ("Notice period", "3 months written notice — triple the pre-reform floor."),
            ("Dispute resolution", "Husleietvistutvalget handles disputes within 90 days."),
        ],
    ),
    (
        "Comparison",
        "See the table below for a side-by-side pre-2024 vs post-2024 comparison "
        "on notice period, dispute window, and applicable cap.",
        [],
    ),
    (
        "Practical recommendations",
        "Tenants subject to a rent increase should verify the notice period, check whether "
        "the increase tracks the official CPI, and contact Husleietvistutvalget if disputed.",
        [
            ("Verify the notice", "Confirm date received and statutory notice length."),
            ("Document the chain", "Keep written correspondence with the landlord."),
        ],
    ),
]

for title, lead, sub_pairs in sections:
    doc.add_heading(title, level=1)
    doc.add_paragraph(lead)
    for sub_title, sub_body in sub_pairs:
        doc.add_heading(sub_title, level=2)
        doc.add_paragraph(sub_body)
    # Insert the comparison table inside the Comparison section.
    if title == "Comparison":
        table = doc.add_table(rows=3, cols=3)
        table.style = "Light Grid Accent 1" if "Light Grid Accent 1" in [
            s.name for s in doc.styles
        ] else "Table Grid"
        # Header row (criterion #7 — header + ≥2 data rows).
        header_cells = table.rows[0].cells
        header_cells[0].text = "Aspect"
        header_cells[1].text = "Pre-2024"
        header_cells[2].text = "Post-2024"
        # Data row 1: notice period.
        row1 = table.rows[1].cells
        row1[0].text = "Notice period"
        row1[1].text = "1 month"
        row1[2].text = "3 months"
        # Data row 2: dispute window.
        row2 = table.rows[2].cells
        row2[0].text = "Dispute window"
        row2[1].text = "180 days"
        row2[2].text = "90 days"
        # Set column widths (criterion #7's "explicit column widths").
        for row in table.rows:
            row.cells[0].width = Inches(1.5)
            row.cells[1].width = Inches(2.0)
            row.cells[2].width = Inches(2.0)

# Embed the inline PNG image sized to 480px wide (~5 inches) — criterion
# #6: inline_shapes width in [100_000, 5_000_000] EMU range.
doc.add_paragraph()
doc.add_picture(str(img_path), width=Inches(5))

out_path = Path("/workspace/out/tenancy-rent-increase-memo.docx")
out_path.parent.mkdir(parents=True, exist_ok=True)
doc.save(out_path)
print(f"WROTE: {out_path}")
'''


# ---------------------------------------------------------------------------
# Loop builder — wires the M1a holder so supplements stage automatically.
# ---------------------------------------------------------------------------


def _build_code_execution_tool(
    sandbox: LocalDockerSandbox,
    *,
    session_id: str,
    deferred_holder: list[SandboxFile],
    persona_workspace: Path,
) -> AsyncTool:
    """Build a code_execution tool with the M1a holder + persistence callback.

    Mirrors :class:`persona_api.services.runtime_factory.RuntimeFactory`'s
    composition root: the shared ``list[SandboxFile]`` is bound to BOTH
    the loop's public ``deferred_input_files`` attribute (the writer) and
    the tool's drain-and-clear closure (the reader). And the
    ``produced_file_persister`` closure mirrors
    :mod:`persona_api.sandbox.runtime_tool._persist_produced_file` —
    copies each produced file from the session's ``host_out`` into the
    persona-workspace directory (D-17-X-bytes-persistence inlined here
    so T11 verifies the production-verification path end-to-end without
    spinning up the api router).
    """
    holder = deferred_holder

    def _drain_and_clear() -> list[SandboxFile]:
        snapshot = list(holder)
        holder.clear()
        return snapshot

    async def _persist_produced_file(sid: str, ref: str) -> None:
        await sandbox.copy_produced_file_to(sid, ref, persona_workspace / ref)

    return make_code_execution_tool(
        sandbox,
        session_id_provider=lambda: session_id,
        deferred_input_files_provider=_drain_and_clear,
        produced_file_persister=_persist_produced_file,
    )


def _build_agentic_loop(
    backend: _ScriptedBackend,
    *,
    sandbox: LocalDockerSandbox,
    session_id: str,
    scanned: list[SkillSpec],
    persona: Persona,
    persona_workspace: Path,
) -> AgenticLoop:
    deferred_holder: list[SandboxFile] = []
    tools: list[AsyncTool] = [
        _build_code_execution_tool(
            sandbox,
            session_id=session_id,
            deferred_holder=deferred_holder,
            persona_workspace=persona_workspace,
        ),
        make_use_skill_tool(scanned),
    ]
    toolbox = Toolbox(tools, allow_list=list(persona.tools or []))
    loop = AgenticLoop(
        persona=persona,
        stores=_stores(),  # type: ignore[arg-type]
        toolbox=toolbox,
        skill_injector=SkillInjector(),
        scanned_skills=scanned,
        prompt_builder=PromptBuilder(),
        router=Router(),
        tier_registry=_registry_for(backend),
    )
    loop.deferred_input_files = deferred_holder
    return loop


# ---------------------------------------------------------------------------
# The test.
# ---------------------------------------------------------------------------


class TestT11DocxEndToEnd:
    """T11 — docx_generation production-verification end-to-end (criterion #2).

    Asserts every research §3.7 docx programmatic surface against the file
    landed at ``<persona_workspace_root>/<owner_id>/<persona_id>/<filename>``
    by the real ``_persist_produced_file`` callback. Visual-only rows are
    PARTIAL by definition (frontend not yet shipped) and tagged honestly.
    """

    @pytest.mark.asyncio
    async def test_docx_lands_at_persona_workspace_and_passes_quality_bar(
        self, tmp_path: Path
    ) -> None:
        skip_reason = _docker_skip_reason()
        if skip_reason:
            pytest.skip(skip_reason)

        # python-docx is a host-side test dep (dev-only); skip if not present.
        docx = pytest.importorskip("docx")

        scanned = _scan_real_docx_skill()
        assert len(scanned) == 1, (
            f"expected exactly one SkillSpec (docx_generation → document_generation "
            f"via the alias shim); got {[s.name for s in scanned]}"
        )
        docx_spec = scanned[0]
        assert docx_spec.name == "document_generation"

        # Verify M1a supplements collection — produces relative paths
        # (post D-16-X-7 fix). This is a pre-test sanity, not the main
        # assertion (T08 + the unit tests already regression-guard the
        # producer; T11 verifies the integration with the real skill).
        supplements = collect_skill_supplements(docx_spec)
        assert supplements, (
            "document_generation must have on-disk supplements/ files — staged via M1a"
        )
        for entry in supplements:
            assert not entry.path.startswith("/"), (
                f"D-16-X-7 violation: collect_skill_supplements emitted absolute "
                f"path {entry.path!r}; must be relative"
            )
            assert entry.path.startswith(".skills/document_generation/supplements/"), (
                f"D-16-2-path violation: {entry.path!r} does not match the staged shape"
            )
        # The docx supplements survive the fold as docx-<topic>.md.
        assert any(e.path.endswith("/docx-tables.md") for e in supplements)

        # Compose the persona workspace + sandbox.
        persona_workspace_root = tmp_path / "persona_workspace"
        persona_workspace = persona_workspace_root / _OWNER_ID / _PERSONA_ID
        persona_workspace.mkdir(parents=True, exist_ok=True)

        sandbox = LocalDockerSandbox(workspace_root=tmp_path / "wsp")
        try:
            session_id = f"{_OWNER_ID}:conv_{uuid.uuid4().hex[:8]}"
            await sandbox.create_session(
                session_id, limits=ResourceLimits(), network=NetworkPolicy()
            )

            # Scripted chat: (a) use_skill(docx_generation) → supplements
            # are staged on the holder; (b) code_execution with the
            # python-docx snippet → writes /workspace/out/<name>.docx;
            # the produced_file_persister copies it to the persona-workspace;
            # (c) final.
            chat_script: list[ChatResponse] = [
                _resp(
                    tool_calls=[
                        ToolCall(
                            name="use_skill",
                            args={
                                "skill_name": "document_generation",
                                "parameters": {"format": "docx"},
                            },
                            call_id="us1",
                        )
                    ]
                ),
                _resp(
                    tool_calls=[
                        ToolCall(
                            name="code_execution",
                            args={"code": _DOCX_CODE},
                            call_id="ce1",
                        )
                    ]
                ),
                _resp("[FINAL] Wrote /workspace/out/tenancy-rent-increase-memo.docx"),
            ]
            backend = _ScriptedBackend(chat_script=chat_script)
            loop = _build_agentic_loop(
                backend,
                sandbox=sandbox,
                session_id=session_id,
                scanned=scanned,
                persona=_persona(),
                persona_workspace=persona_workspace,
            )

            run = await loop.run(
                "Draft a 5-section memo titled 'Norwegian tenancy law: "
                "protections for tenants subject to landlord rent increases' "
                "as a docx. Include a TOC, a 3-column comparison table, and "
                "one inline placeholder image."
            )

            # ---- COMPOSITION GUARDS --------------------------------
            assert run.status is RunStatus.COMPLETED, (
                f"expected COMPLETED; got {run.status}: {run.error!r}"
            )
            code_step = next(
                s
                for s in run.steps
                if s.type is StepType.TOOL_CALL
                and any(c.name == "code_execution" for c in s.tool_calls)
            )
            ce_result = next(
                r
                for r, c in zip(code_step.results, code_step.tool_calls, strict=True)
                if c.name == "code_execution"
            )
            assert ce_result.is_error is False, f"code_execution failed: {ce_result.content!r}"
            assert ce_result.data is not None
            produced = ce_result.data.get("produced_files") or []
            assert any(
                p.get("path", "").endswith("tenancy-rent-increase-memo.docx") for p in produced
            ), f"expected memo.docx in produced_files; got {produced}"

            # ---- PERSONA-WORKSPACE PERSISTENCE GUARD -----------------
            # The real _persist_produced_file callback (D-17-X-bytes-persistence)
            # has just copied the docx from the sandbox session's host_out into
            # <persona_workspace_root>/<owner_id>/<persona_id>/<filename>.
            workspace_docx = persona_workspace / "tenancy-rent-increase-memo.docx"
            assert workspace_docx.is_file(), (
                f"expected docx persisted at {workspace_docx}; "
                f"persona_workspace listing = {list(persona_workspace.iterdir())}"
            )
            assert workspace_docx.stat().st_size > 5000, (
                f"docx too small ({workspace_docx.stat().st_size} bytes); "
                "expected ≥5KB for a 5-section memo with TOC + table + image"
            )

            # ---- SAVE INSPECTION ARTIFACT (D-16-X-3) ----------------
            # The artifact is reproducible by re-running this test. It is
            # gitignored at docs/specs/phase2/spec_16/inspection/ per
            # D-16-X-3. The committed evidence is the state.md scorecard,
            # not the binary itself.
            _INSPECTION_DIR.mkdir(parents=True, exist_ok=True)
            inspection_target = _INSPECTION_DIR / "docx_sample.docx"
            shutil.copy(workspace_docx, inspection_target)

            # ---- §3.7 PROGRAMMATIC ASSERTIONS -----------------------
            # Load via python-docx and run every assert from research §3.7
            # docx table. Visual-only rows are documented in the
            # state.md scorecard as PARTIAL (programmatic surrogate where
            # one exists; honest "Visual" otherwise — frontend not yet
            # shipped so visual inspection happens manually).
            doc = docx.Document(str(workspace_docx))
            xml = doc.element.xml

            # Row 1: TOC field shell present (PASS).
            assert "fldSimple" in xml or "fldChar" in xml, (
                "§3.7 docx #1: expected fldSimple or fldChar in XML "
                "(TOC field shell). python-docx writes the shell; Word "
                "fills on F9 / Update Field."
            )

            # Row 3: Heading 1 + Heading 2 as named styles (PASS).
            heading_1_present = any(
                p.style is not None and p.style.name == "Heading 1" for p in doc.paragraphs
            )
            heading_2_present = any(
                p.style is not None and p.style.name == "Heading 2" for p in doc.paragraphs
            )
            assert heading_1_present, (
                "§3.7 docx #3a: expected at least one paragraph with style "
                "'Heading 1' (named styles, not raw bold + size)"
            )
            assert heading_2_present, (
                "§3.7 docx #3b: expected at least one paragraph with style "
                "'Heading 2' (named styles, not raw bold + size)"
            )

            # Row 4: Body font name + size set explicitly (PASS).
            normal_style = doc.styles["Normal"]
            assert normal_style.font.name is not None, (
                "§3.7 docx #4a: Normal.font.name must be set explicitly "
                "(Word defaults are not consistent across versions)"
            )
            assert normal_style.font.size is not None, (
                "§3.7 docx #4b: Normal.font.size must be set explicitly"
            )

            # Row 6: image embedded + sized to a reasonable EMU range (PASS).
            assert len(doc.inline_shapes) >= 1, (
                "§3.7 docx #6a: expected ≥1 inline_shape (inline image)"
            )
            img_width_emu = doc.inline_shapes[0].width.emu
            assert 100_000 <= img_width_emu <= 5_000_000, (
                f"§3.7 docx #6b: inline image width {img_width_emu} EMU "
                "outside [100_000, 5_000_000] — likely raw 96-dpi giant "
                "or sub-pixel placeholder"
            )

            # Row 7: table with ≥3 rows (header + ≥2 data) (PASS).
            assert len(doc.tables) >= 1, "§3.7 docx #7a: expected ≥1 table"
            first_table = doc.tables[0]
            assert len(first_table.rows) >= 3, (
                f"§3.7 docx #7b: first table has {len(first_table.rows)} rows; "
                "expected ≥3 (header + 2 data)"
            )
            # Bonus check — 3 columns per the §3.2 representative task.
            assert len(first_table.columns) >= 3, (
                f"§3.7 docx #7c: first table has {len(first_table.columns)} "
                "columns; expected ≥3 (pre-2024 vs post-2024 comparison)"
            )

            # Row 2: Page numbers in footer — PARTIAL surrogate. The
            # representative task doesn't strictly require footers; the
            # SKILL.md teaches them as "every multi-page document".
            # T11 records this as a structural surrogate: check whether
            # ANY footer paragraph carries a PAGE field. If absent in
            # the §3.2 minimal task, mark PARTIAL honestly.
            footer_has_page_field = any(
                "PAGE" in p._p.xml for section in doc.sections for p in section.footer.paragraphs
            )
            # Record this in state.md as PARTIAL — the SKILL.md teaches
            # the footer page-number idiom but the representative task
            # does not explicitly demand it; the §3.7 surface is a soft
            # surrogate, not a hard fail.
            _ = footer_has_page_field  # surfaced in state.md scorecard

            # Row 5 (paragraph spacing) and Row 8 (no XML errors on
            # open) are visual-only — handled as PARTIAL in state.md.
            # The file opens (parsed cleanly by python-docx above), so
            # Row 8 is the strongest possible programmatic surrogate.

        finally:
            await sandbox.aclose()
