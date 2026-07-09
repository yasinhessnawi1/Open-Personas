"""RuntimeFactory wires ``render_diagram`` when a workspace persister exists.

Spec 28 B3. ``render_diagram`` is runtime-wired (needs the WorkspacePersister to
store the diagram source for client-side SVG rendering). Since R5-D-4 the
persister writes through the ``FileStorage`` seam: it is composed in
``_build_toolbox`` only when a ``file_storage`` backend is injected (the app
composition root builds one from ``workspace_root`` via ``build_file_storage``);
the persona allow-list is the final advertisement gate.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from persona.schema.persona import Persona, PersonaIdentity
from persona_api.services.runtime_factory import RuntimeFactory
from persona_api.storage import LocalFileStorage

if TYPE_CHECKING:
    from pathlib import Path

pytestmark = pytest.mark.integration


def _make_persona(*, tools: list[str]) -> Persona:
    return Persona(
        persona_id="persona_diagwire_test",
        identity=PersonaIdentity(
            name="Astrid", role="assistant", background="diagram-wiring test."
        ),
        tools=tools,
    )


def _make_factory(*, audit_root: Path, workspace_root: Path | None) -> RuntimeFactory:
    # Mirror the app composition root (R5-D-4): a configured workspace_root
    # comes with a FileStorage backend (``build_file_storage`` → local default);
    # no workspace ⇒ no storage ⇒ no persister.
    return RuntimeFactory(
        rls_engine=None,  # type: ignore[arg-type]
        embedder=None,  # type: ignore[arg-type]
        tier_registry=None,  # type: ignore[arg-type]
        turn_log_writer=None,  # type: ignore[arg-type]
        audit_root=audit_root,
        file_storage=LocalFileStorage(workspace_root) if workspace_root is not None else None,
        sandbox_pool=None,
        workspace_root=workspace_root,
        image_backend=None,
    )


@pytest.mark.asyncio
async def test_render_diagram_registered_when_workspace_present(tmp_path: Path) -> None:
    factory = _make_factory(audit_root=tmp_path, workspace_root=tmp_path / "ws")
    persona = _make_persona(tools=["render_diagram"])
    toolbox = await factory._build_toolbox(persona, scanned_skills=[])
    assert "render_diagram" in toolbox.names()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_render_diagram_absent_when_no_workspace(tmp_path: Path) -> None:
    # No workspace_root → no persister → render_diagram not composed.
    factory = _make_factory(audit_root=tmp_path, workspace_root=None)
    persona = _make_persona(tools=["render_diagram"])
    toolbox = await factory._build_toolbox(persona, scanned_skills=[])
    assert "render_diagram" not in toolbox.names()  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_allow_list_gates_render_diagram(tmp_path: Path) -> None:
    factory = _make_factory(audit_root=tmp_path, workspace_root=tmp_path / "ws")
    persona = _make_persona(tools=["web_search"])  # no render_diagram declared
    toolbox = await factory._build_toolbox(persona, scanned_skills=[])
    assert "render_diagram" not in toolbox.names()  # type: ignore[attr-defined]
    assert "web_search" in toolbox.names()  # type: ignore[attr-defined]
